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


def listar_modelos(proveedor, api_key, timeout=TIMEOUT_POR_DEFECTO):
    """Consulta al proveedor qué modelos hay disponibles para esta clave.

    Devuelve una lista de {'id': str, 'nombre': str} ordenada, para poblar el
    desplegable de la página de configuración. Sirve además como verificación
    de que la clave es válida: si la clave no sirve, lanza ErrorProveedorIA.
    """
    try:
        if proveedor == 'anthropic':
            return _modelos_anthropic(api_key, timeout)
        if proveedor == 'openai':
            return _modelos_openai(api_key, timeout)
        if proveedor == 'gemini':
            return _modelos_gemini(api_key, timeout)
        raise ErrorProveedorIA(f'Proveedor de IA desconocido: {proveedor}')
    except requests.Timeout:
        raise ErrorProveedorIA('El proveedor de IA ha tardado demasiado en responder. Inténtalo de nuevo.')
    except requests.RequestException:
        raise ErrorProveedorIA('No se pudo contactar con el proveedor de IA. Comprueba tu conexión.')


def _error_clave(nombre_proveedor, status_code):
    if status_code in (401, 403):
        return ErrorProveedorIA(f'{nombre_proveedor} ha rechazado la clave API (error {status_code}). Revísala.')
    return ErrorProveedorIA(f'{nombre_proveedor} devolvió un error ({status_code}) al listar los modelos.')


def _error_chat(nombre_proveedor, status_code, modelo):
    """Mensaje de error del chat, distinguiendo clave inválida de modelo inexistente."""
    if status_code in (401, 403):
        return ErrorProveedorIA(
            f'{nombre_proveedor} ha rechazado la clave API (error {status_code}). '
            'Revísala en la configuración del Asistente IA.'
        )
    if status_code == 404:
        return ErrorProveedorIA(
            f'{nombre_proveedor} no reconoce el modelo "{modelo}" (error 404). '
            'Ve a la configuración del Asistente IA y elige un modelo de la lista.'
        )
    if status_code == 429:
        return ErrorProveedorIA(f'{nombre_proveedor} ha limitado el ritmo de peticiones (error 429). Inténtalo en unos segundos.')
    return ErrorProveedorIA(f'{nombre_proveedor} devolvió un error ({status_code}).')


def _modelos_anthropic(api_key, timeout):
    resp = requests.get(
        'https://api.anthropic.com/v1/models',
        headers={'x-api-key': api_key, 'anthropic-version': '2023-06-01'},
        params={'limit': 100},
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise _error_clave('Claude', resp.status_code)
    modelos = [
        {'id': m['id'], 'nombre': m.get('display_name') or m['id']}
        for m in resp.json().get('data', []) if m.get('id')
    ]
    return sorted(modelos, key=lambda m: m['id'], reverse=True)


def _modelos_openai(api_key, timeout):
    resp = requests.get(
        'https://api.openai.com/v1/models',
        headers={'Authorization': f'Bearer {api_key}'},
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise _error_clave('ChatGPT', resp.status_code)
    # La lista incluye modelos de embeddings, audio, imagen… nos quedamos con
    # los de chat, que son los únicos utilizables por el asistente.
    prefijos_chat = ('gpt-', 'o1', 'o3', 'o4', 'chatgpt-')
    excluir = ('-audio', '-realtime', '-transcribe', '-tts', '-image', '-search', 'instruct')
    modelos = [
        {'id': m['id'], 'nombre': m['id']}
        for m in resp.json().get('data', [])
        if m.get('id', '').startswith(prefijos_chat)
        and not any(x in m['id'] for x in excluir)
    ]
    return sorted(modelos, key=lambda m: m['id'], reverse=True)


def _modelos_gemini(api_key, timeout):
    resp = requests.get(
        'https://generativelanguage.googleapis.com/v1beta/models',
        params={'key': api_key, 'pageSize': 200},
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise _error_clave('Gemini', resp.status_code)
    modelos = []
    for m in resp.json().get('models', []):
        # Solo los que soportan generación de contenido (excluye embeddings…)
        if 'generateContent' not in m.get('supportedGenerationMethods', []):
            continue
        identificador = m.get('name', '').removeprefix('models/')
        if identificador:
            modelos.append({'id': identificador, 'nombre': m.get('displayName') or identificador})
    return sorted(modelos, key=lambda m: m['id'], reverse=True)


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
        raise _error_chat('Claude', resp.status_code, modelo)
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
        raise _error_chat('ChatGPT', resp.status_code, modelo)
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
        raise _error_chat('Gemini', resp.status_code, modelo)
    data = resp.json()
    try:
        return data['candidates'][0]['content']['parts'][0]['text']
    except (KeyError, IndexError):
        raise ErrorProveedorIA('Gemini devolvió una respuesta inesperada.')
