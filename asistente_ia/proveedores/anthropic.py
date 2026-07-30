"""Proveedor Anthropic (Claude) — implementación completa: tool_use nativo y
`cache_control: ephemeral` en el bloque de system que contiene el briefing.

El caching es la optimización de coste/latencia más importante del diseño:
el briefing es idéntico turno a turno dentro de una conversación, así que
marcarlo como cacheable evita que Anthropic tenga que re-procesarlo (y
re-cobrarlo a precio completo) en cada mensaje.
"""
import requests

from .base import ProveedorLLM, RespuestaLLM, envolver_errores_red, error_chat, error_clave
from .bloques import bloque_texto, bloque_tool_use

API_BASE = 'https://api.anthropic.com/v1'
MAX_TOKENS_RESPUESTA = 4096


class AnthropicProvider(ProveedorLLM):
    id = 'anthropic'
    nombre_mostrado = 'Claude (Anthropic)'

    def _headers(self, api_key):
        return {
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01',
            # Habilita cache_control en cuentas donde el caching de prompts
            # todavía requiere el flag beta; en el resto se ignora sin efecto.
            'anthropic-beta': 'prompt-caching-2024-07-31',
            'content-type': 'application/json',
        }

    def _traducir_mensajes(self, mensajes):
        salida = []
        for msg in mensajes:
            contenido = []
            for bloque in msg['blocks']:
                if bloque['tipo'] == 'text':
                    if bloque['texto']:
                        contenido.append({'type': 'text', 'text': bloque['texto']})
                elif bloque['tipo'] == 'tool_use':
                    contenido.append({
                        'type': 'tool_use', 'id': bloque['id'],
                        'name': bloque['nombre'], 'input': bloque['argumentos'],
                    })
                elif bloque['tipo'] == 'tool_result':
                    contenido.append({
                        'type': 'tool_result', 'tool_use_id': bloque['tool_use_id'],
                        'content': bloque['contenido'], 'is_error': bloque.get('es_error', False),
                    })
            if contenido:
                salida.append({'role': msg['role'], 'content': contenido})
        return salida

    def _traducir_system(self, system_blocks):
        bloques = []
        for b in system_blocks or []:
            if not b.get('texto'):
                continue
            bloque = {'type': 'text', 'text': b['texto']}
            if b.get('cacheable'):
                bloque['cache_control'] = {'type': 'ephemeral'}
            bloques.append(bloque)
        return bloques

    def _traducir_tools(self, tools):
        if not tools:
            return None
        return [
            {'name': t['name'], 'description': t['description'], 'input_schema': t['parametros']}
            for t in tools
        ]

    def _parsear_respuesta(self, data):
        blocks = []
        for b in data.get('content', []):
            if b.get('type') == 'text':
                blocks.append(bloque_texto(b.get('text', '')))
            elif b.get('type') == 'tool_use':
                blocks.append(bloque_tool_use(b.get('id'), b.get('name'), b.get('input') or {}))
        uso = data.get('usage', {})
        return RespuestaLLM(
            blocks=blocks,
            parada=data.get('stop_reason', ''),
            uso={
                'entrada': uso.get('input_tokens', 0),
                'salida': uso.get('output_tokens', 0),
                'cache_creado': uso.get('cache_creation_input_tokens', 0),
                'cache_leido': uso.get('cache_read_input_tokens', 0),
            },
        )

    def enviar(self, mensajes, system_blocks, tools, api_key, modelo, timeout=30):
        cuerpo = {
            'model': modelo,
            'max_tokens': MAX_TOKENS_RESPUESTA,
            'system': self._traducir_system(system_blocks),
            'messages': self._traducir_mensajes(mensajes),
        }
        tools_traducidas = self._traducir_tools(tools)
        if tools_traducidas:
            cuerpo['tools'] = tools_traducidas

        try:
            resp = requests.post(
                f'{API_BASE}/messages', headers=self._headers(api_key), json=cuerpo, timeout=timeout,
            )
        except requests.Timeout:
            return RespuestaLLM(error='Claude ha tardado demasiado en responder. Inténtalo de nuevo.')
        except requests.RequestException:
            return RespuestaLLM(error='No se pudo contactar con Claude. Comprueba tu conexión.')

        if resp.status_code != 200:
            return RespuestaLLM(error=error_chat('Claude', resp.status_code, modelo))
        try:
            return self._parsear_respuesta(resp.json())
        except (ValueError, KeyError) as e:
            return RespuestaLLM(error=f'Claude devolvió una respuesta inesperada: {e}')

    def listar_modelos(self, api_key, timeout=30):
        def _hacer():
            resp = requests.get(
                f'{API_BASE}/models',
                headers={'x-api-key': api_key, 'anthropic-version': '2023-06-01'},
                params={'limit': 100}, timeout=timeout,
            )
            if resp.status_code != 200:
                raise error_clave('Claude', resp.status_code)
            modelos = [
                {'id': m['id'], 'nombre': m.get('display_name') or m['id']}
                for m in resp.json().get('data', []) if m.get('id')
            ]
            return sorted(modelos, key=lambda m: m['id'], reverse=True)
        return envolver_errores_red('Claude', _hacer)
