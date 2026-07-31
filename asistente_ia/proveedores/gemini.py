"""Proveedor Gemini (Google) — soporte best-effort de function calling.

La API de Gemini identifica las llamadas a función por NOMBRE, no por un id
independiente como Anthropic/OpenAI. Para encajar en el formato neutro (que
correlaciona tool_use↔tool_result por id) se usa el propio nombre de la
función como id sintético — funciona salvo que el modelo pida la misma tool
dos veces en el mismo turno, un caso raro que aquí se trata como best-effort:
cualquier fallo de traducción/parseo se devuelve como error, nunca como
excepción sin capturar.
"""
import requests

from .base import ProveedorLLM, RespuestaLLM, envolver_errores_red, error_chat, error_clave
from .bloques import bloque_texto, bloque_tool_use

API_BASE = 'https://generativelanguage.googleapis.com/v1beta'


class GeminiProvider(ProveedorLLM):
    id = 'gemini'
    nombre_mostrado = 'Gemini (Google)'

    def _traducir_system(self, system_blocks):
        texto = '\n\n'.join(b['texto'] for b in (system_blocks or []) if b.get('texto'))
        return {'parts': [{'text': texto}]} if texto else None

    def _traducir_mensajes(self, mensajes):
        salida = []
        for msg in mensajes:
            parts = []
            for bloque in msg['blocks']:
                if bloque['tipo'] == 'text':
                    if bloque['texto']:
                        parts.append({'text': bloque['texto']})
                elif bloque['tipo'] == 'tool_use':
                    parts.append({'functionCall': {'name': bloque['nombre'], 'args': bloque['argumentos']}})
                elif bloque['tipo'] == 'tool_result':
                    contenido = bloque['contenido']
                    parts.append({'functionResponse': {
                        'name': bloque['tool_use_id'],
                        'response': {'resultado': contenido} if not bloque.get('es_error') else {'error': contenido},
                    }})
            if parts:
                rol = 'model' if msg['role'] == 'assistant' else 'user'
                salida.append({'role': rol, 'parts': parts})
        return salida

    def _traducir_tools(self, tools):
        if not tools:
            return None
        return [{'functionDeclarations': [
            {'name': t['name'], 'description': t['description'], 'parameters': t['parametros']}
            for t in tools
        ]}]

    def _parsear_respuesta(self, data):
        candidatos = data.get('candidates') or []
        if not candidatos:
            return RespuestaLLM(error='Gemini no devolvió ninguna respuesta (puede haber bloqueado el contenido).')
        candidato = candidatos[0]
        blocks = []
        for part in candidato.get('content', {}).get('parts', []):
            if 'text' in part:
                blocks.append(bloque_texto(part['text']))
            elif 'functionCall' in part:
                fc = part['functionCall']
                nombre = fc.get('name', '')
                blocks.append(bloque_tool_use(nombre, nombre, fc.get('args') or {}))
        uso = data.get('usageMetadata', {})
        return RespuestaLLM(
            blocks=blocks,
            parada=candidato.get('finishReason', ''),
            uso={
                'entrada': uso.get('promptTokenCount', 0),
                'salida': uso.get('candidatesTokenCount', 0),
                'cache_leido': uso.get('cachedContentTokenCount', 0),
            },
        )

    def enviar(self, mensajes, system_blocks, tools, api_key, modelo, timeout=120):
        try:
            cuerpo = {'contents': self._traducir_mensajes(mensajes)}
            system_instruction = self._traducir_system(system_blocks)
            if system_instruction:
                cuerpo['system_instruction'] = system_instruction
            tools_traducidas = self._traducir_tools(tools)
            if tools_traducidas:
                cuerpo['tools'] = tools_traducidas
        except Exception as e:  # noqa: BLE001 — best-effort: nunca romper el loop
            return RespuestaLLM(error=f'No se pudo construir la petición para Gemini: {e}')

        url = f'{API_BASE}/models/{modelo}:generateContent'
        try:
            resp = requests.post(url, params={'key': api_key}, json=cuerpo, timeout=timeout)
        except requests.Timeout:
            return RespuestaLLM(error='Gemini ha tardado demasiado en responder. Inténtalo de nuevo.')
        except requests.RequestException:
            return RespuestaLLM(error='No se pudo contactar con Gemini. Comprueba tu conexión.')

        if resp.status_code != 200:
            return RespuestaLLM(error=error_chat('Gemini', resp.status_code, modelo))
        try:
            return self._parsear_respuesta(resp.json())
        except (ValueError, KeyError, IndexError) as e:
            return RespuestaLLM(error=f'Gemini devolvió una respuesta inesperada: {e}')

    def listar_modelos(self, api_key, timeout=30):
        def _hacer():
            resp = requests.get(
                f'{API_BASE}/models', params={'key': api_key, 'pageSize': 200}, timeout=timeout,
            )
            if resp.status_code != 200:
                raise error_clave('Gemini', resp.status_code)
            modelos = []
            for m in resp.json().get('models', []):
                if 'generateContent' not in m.get('supportedGenerationMethods', []):
                    continue
                identificador = m.get('name', '').removeprefix('models/')
                if identificador:
                    modelos.append({'id': identificador, 'nombre': m.get('displayName') or identificador})
            return sorted(modelos, key=lambda m: m['id'], reverse=True)
        return envolver_errores_red('Gemini', _hacer)
