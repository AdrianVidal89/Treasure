"""Interfaz común a los proveedores de LLM + utilidades de error compartidas.

Contrato importante: `ProveedorLLM.enviar()` NUNCA lanza una excepción sin
capturar. Cualquier fallo del proveedor (clave inválida, rate limit, timeout,
conexión, respuesta inesperada) se devuelve como `RespuestaLLM(error=...)`
— así el loop del agente nunca puede convertir un fallo del proveedor en un
500 de Django.

`listar_modelos()` es la excepción: se usa solo desde la página de
configuración (para poblar el desplegable y validar la clave que se está
guardando), donde el llamante ya envuelve la llamada en un try/except
explícito — ahí sí tiene sentido seguir lanzando `ErrorProveedorIA`.
"""
from dataclasses import dataclass, field

import requests

TIMEOUT_POR_DEFECTO = 30


class ErrorProveedorIA(Exception):
    """Error amigable, seguro para mostrar directamente en el chat."""


@dataclass
class RespuestaLLM:
    blocks: list = field(default_factory=list)
    error: str = ''
    uso: dict = field(default_factory=dict)
    parada: str = ''  # 'texto' | 'tool_use' | 'max_tokens' | ...

    @property
    def texto(self):
        return ''.join(b['texto'] for b in self.blocks if b.get('tipo') == 'text')

    @property
    def tool_uses(self):
        return [b for b in self.blocks if b.get('tipo') == 'tool_use']


class ProveedorLLM:
    """Una implementación por proveedor traduce el formato neutro de
    `proveedores.bloques` al suyo propio."""

    id = None
    nombre_mostrado = ''

    def enviar(self, mensajes, system_blocks, tools, api_key, modelo, timeout=TIMEOUT_POR_DEFECTO):
        """
        mensajes: lista de {'role','blocks':[...]} (ver bloques.py).
        system_blocks: lista de {'texto': str, 'cacheable': bool}.
        tools: lista de {'name','description','parametros': json-schema} o None.

        Devuelve SIEMPRE un RespuestaLLM; nunca lanza.
        """
        raise NotImplementedError

    def listar_modelos(self, api_key, timeout=TIMEOUT_POR_DEFECTO):
        """Lista de {'id','nombre'} disponibles para esta clave. Lanza
        ErrorProveedorIA si la clave no es válida o el proveedor falla."""
        raise NotImplementedError


def envolver_errores_red(nombre_proveedor, fn, *args, **kwargs):
    """Ejecuta `fn` capturando fallos de red/timeout y devolviéndolos como
    ErrorProveedorIA, para que las implementaciones no tengan que repetir el
    mismo try/except en cada método."""
    try:
        return fn(*args, **kwargs)
    except requests.Timeout:
        raise ErrorProveedorIA(f'{nombre_proveedor} ha tardado demasiado en responder. Inténtalo de nuevo.')
    except requests.RequestException:
        raise ErrorProveedorIA(f'No se pudo contactar con {nombre_proveedor}. Comprueba tu conexión.')


def error_clave(nombre_proveedor, status_code):
    if status_code in (401, 403):
        return ErrorProveedorIA(f'{nombre_proveedor} ha rechazado la clave API (error {status_code}). Revísala.')
    return ErrorProveedorIA(f'{nombre_proveedor} devolvió un error ({status_code}) al listar los modelos.')


def error_chat(nombre_proveedor, status_code, modelo):
    """Mensaje de error del chat, distinguiendo clave inválida de modelo inexistente."""
    if status_code in (401, 403):
        return (
            f'{nombre_proveedor} ha rechazado la clave API (error {status_code}). '
            'Revísala en la configuración del Asistente IA.'
        )
    if status_code == 404:
        return (
            f'{nombre_proveedor} no reconoce el modelo "{modelo}" (error 404). '
            'Ve a la configuración del Asistente IA y elige un modelo de la lista.'
        )
    if status_code == 429:
        return f'{nombre_proveedor} ha limitado el ritmo de peticiones (error 429). Inténtalo en unos segundos.'
    return f'{nombre_proveedor} devolvió un error ({status_code}).'
