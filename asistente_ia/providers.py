"""Llamadas directas (vía REST) a los proveedores de IA soportados.

Se usa `requests` en lugar de instalar los tres SDKs oficiales, para mantener
el mínimo de dependencias del proyecto.
"""
import requests

TIMEOUT_POR_DEFECTO = 30


class ErrorProveedorIA(Exception):
    """Error amigable, seguro para mostrar directamente en el chat."""


def enviar_mensaje(proveedor, api_key, modelo, historial_mensajes, system_prompt,
                    timeout=TIMEOUT_POR_DEFECTO):
    """
    historial_mensajes: lista de {'rol': 'user'|'assistant', 'contenido': str},
    en orden cronológico (sin mensajes de rol 'system').

    Devuelve el texto de la respuesta del asistente.
    Lanza ErrorProveedorIA en cualquier fallo (timeout, HTTP != 2xx, respuesta
    inesperada) con un mensaje apto para mostrar al usuario.
    """
    try:
        if proveedor == 'anthropic':
            return _enviar_anthropic(api_key, modelo, historial_mensajes, system_prompt, timeout)
        if proveedor == 'openai':
            return _enviar_openai(api_key, modelo, historial_mensajes, system_prompt, timeout)
        if proveedor == 'gemini':
            return _enviar_gemini(api_key, modelo, historial_mensajes, system_prompt, timeout)
        raise ErrorProveedorIA(f'Proveedor de IA desconocido: {proveedor}')
    except requests.Timeout:
        raise ErrorProveedorIA('El proveedor de IA ha tardado demasiado en responder. Inténtalo de nuevo.')
    except requests.RequestException:
        raise ErrorProveedorIA('No se pudo contactar con el proveedor de IA. Comprueba tu conexión.')


def _enviar_anthropic(api_key, modelo, historial, system_prompt, timeout):
    resp = requests.post(
        'https://api.anthropic.com/v1/messages',
        headers={
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json',
        },
        json={
            'model': modelo,
            'max_tokens': 2048,
            'system': system_prompt,
            'messages': [{'role': m['rol'], 'content': m['contenido']} for m in historial],
        },
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise ErrorProveedorIA(f'Claude devolvió un error ({resp.status_code}). Revisa la clave API y el modelo configurados.')
    data = resp.json()
    bloques_texto = [b.get('text', '') for b in data.get('content', []) if b.get('type') == 'text']
    return ''.join(bloques_texto) or '(respuesta vacía)'


def _enviar_openai(api_key, modelo, historial, system_prompt, timeout):
    mensajes = [{'role': 'system', 'content': system_prompt}] + [
        {'role': m['rol'], 'content': m['contenido']} for m in historial
    ]
    resp = requests.post(
        'https://api.openai.com/v1/chat/completions',
        headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
        json={'model': modelo, 'messages': mensajes},
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise ErrorProveedorIA(f'ChatGPT devolvió un error ({resp.status_code}). Revisa la clave API y el modelo configurados.')
    data = resp.json()
    try:
        return data['choices'][0]['message']['content']
    except (KeyError, IndexError):
        raise ErrorProveedorIA('ChatGPT devolvió una respuesta inesperada.')


def _enviar_gemini(api_key, modelo, historial, system_prompt, timeout):
    url = f'https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent'
    contents = [
        {'role': 'model' if m['rol'] == 'assistant' else 'user', 'parts': [{'text': m['contenido']}]}
        for m in historial
    ]
    resp = requests.post(
        url,
        params={'key': api_key},
        json={
            'system_instruction': {'parts': [{'text': system_prompt}]},
            'contents': contents,
        },
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise ErrorProveedorIA(f'Gemini devolvió un error ({resp.status_code}). Revisa la clave API y el modelo configurados.')
    data = resp.json()
    try:
        return data['candidates'][0]['content']['parts'][0]['text']
    except (KeyError, IndexError):
        raise ErrorProveedorIA('Gemini devolvió una respuesta inesperada.')
