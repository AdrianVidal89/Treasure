"""Proveedor OpenAI (ChatGPT) — tool_use nativo vía `tool_calls`.

OpenAI no expone un control de caching equivalente a `cache_control` de
Anthropic (aplica caching automático de prompts largos en servidor), así que
aquí `system_blocks` simplemente se concatena en un único mensaje de system.
"""
import json

import requests

from .base import ProveedorLLM, RespuestaLLM, envolver_errores_red, error_chat, error_clave
from .bloques import bloque_texto, bloque_tool_use

API_BASE = 'https://api.openai.com/v1'


class OpenAIProvider(ProveedorLLM):
    id = 'openai'
    nombre_mostrado = 'ChatGPT (OpenAI)'

    def _headers(self, api_key):
        return {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}

    def _traducir_system(self, system_blocks):
        texto = '\n\n'.join(b['texto'] for b in (system_blocks or []) if b.get('texto'))
        return [{'role': 'system', 'content': texto}] if texto else []

    def _traducir_mensajes(self, mensajes):
        salida = []
        for msg in mensajes:
            textos = [b['texto'] for b in msg['blocks'] if b['tipo'] == 'text' and b['texto']]
            tool_uses = [b for b in msg['blocks'] if b['tipo'] == 'tool_use']
            tool_results = [b for b in msg['blocks'] if b['tipo'] == 'tool_result']

            if msg['role'] == 'assistant' and tool_uses:
                salida.append({
                    'role': 'assistant',
                    'content': '\n'.join(textos) or None,
                    'tool_calls': [
                        {
                            'id': tu['id'], 'type': 'function',
                            'function': {'name': tu['nombre'], 'arguments': json.dumps(tu['argumentos'], ensure_ascii=False)},
                        }
                        for tu in tool_uses
                    ],
                })
                continue

            if tool_results:
                for tr in tool_results:
                    salida.append({
                        'role': 'tool', 'tool_call_id': tr['tool_use_id'],
                        'content': tr['contenido'],
                    })
                continue

            if textos:
                salida.append({'role': msg['role'], 'content': '\n'.join(textos)})
        return salida

    def _traducir_tools(self, tools):
        if not tools:
            return None
        return [
            {'type': 'function', 'function': {'name': t['name'], 'description': t['description'], 'parameters': t['parametros']}}
            for t in tools
        ]

    def _parsear_respuesta(self, data):
        mensaje = data['choices'][0]['message']
        blocks = []
        if mensaje.get('content'):
            blocks.append(bloque_texto(mensaje['content']))
        for tc in mensaje.get('tool_calls') or []:
            try:
                argumentos = json.loads(tc['function']['arguments'] or '{}')
            except (ValueError, KeyError):
                argumentos = {}
            blocks.append(bloque_tool_use(tc['id'], tc['function']['name'], argumentos))
        uso = data.get('usage', {})
        detalles_cache = uso.get('prompt_tokens_details') or {}
        return RespuestaLLM(
            blocks=blocks,
            parada=data['choices'][0].get('finish_reason', ''),
            uso={
                'entrada': uso.get('prompt_tokens', 0),
                'salida': uso.get('completion_tokens', 0),
                'cache_leido': detalles_cache.get('cached_tokens', 0),
            },
        )

    def enviar(self, mensajes, system_blocks, tools, api_key, modelo, timeout=120):
        cuerpo = {
            'model': modelo,
            'messages': self._traducir_system(system_blocks) + self._traducir_mensajes(mensajes),
        }
        tools_traducidas = self._traducir_tools(tools)
        if tools_traducidas:
            cuerpo['tools'] = tools_traducidas

        try:
            resp = requests.post(f'{API_BASE}/chat/completions', headers=self._headers(api_key), json=cuerpo, timeout=timeout)
        except requests.Timeout:
            return RespuestaLLM(error='ChatGPT ha tardado demasiado en responder. Inténtalo de nuevo.')
        except requests.RequestException:
            return RespuestaLLM(error='No se pudo contactar con ChatGPT. Comprueba tu conexión.')

        if resp.status_code != 200:
            return RespuestaLLM(error=error_chat('ChatGPT', resp.status_code, modelo))
        try:
            return self._parsear_respuesta(resp.json())
        except (ValueError, KeyError, IndexError) as e:
            return RespuestaLLM(error=f'ChatGPT devolvió una respuesta inesperada: {e}')

    def listar_modelos(self, api_key, timeout=30):
        def _hacer():
            resp = requests.get(f'{API_BASE}/models', headers={'Authorization': f'Bearer {api_key}'}, timeout=timeout)
            if resp.status_code != 200:
                raise error_clave('ChatGPT', resp.status_code)
            prefijos_chat = ('gpt-', 'o1', 'o3', 'o4', 'chatgpt-')
            excluir = ('-audio', '-realtime', '-transcribe', '-tts', '-image', '-search', 'instruct')
            modelos = [
                {'id': m['id'], 'nombre': m['id']}
                for m in resp.json().get('data', [])
                if m.get('id', '').startswith(prefijos_chat) and not any(x in m['id'] for x in excluir)
            ]
            return sorted(modelos, key=lambda m: m['id'], reverse=True)
        return envolver_errores_red('ChatGPT', _hacer)
