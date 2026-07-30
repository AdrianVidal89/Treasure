"""Fábrica de proveedores de LLM + API pública del paquete.

Un formato neutro de mensajes (`bloques.py`) y una clase por proveedor
(`ProveedorLLM`) que lo traduce al suyo. Los proveedores disponibles se
listan aquí; añadir uno nuevo es registrar su clase en `PROVEEDORES`.
"""
from .anthropic import AnthropicProvider
from .base import ErrorProveedorIA, ProveedorLLM, RespuestaLLM, TIMEOUT_POR_DEFECTO
from .gemini import GeminiProvider
from .openai import OpenAIProvider

PROVEEDORES = {
    'anthropic': AnthropicProvider(),
    'openai': OpenAIProvider(),
    'gemini': GeminiProvider(),
}


def obtener_proveedor(id_proveedor):
    proveedor = PROVEEDORES.get(id_proveedor)
    if not proveedor:
        raise ErrorProveedorIA(f'Proveedor de IA desconocido: {id_proveedor}')
    return proveedor


def listar_modelos(id_proveedor, api_key, timeout=TIMEOUT_POR_DEFECTO):
    """Consulta al proveedor qué modelos hay disponibles para esta clave.
    Lanza ErrorProveedorIA si la clave no es válida o el proveedor falla —
    los llamantes (vistas de configuración) ya lo capturan explícitamente."""
    return obtener_proveedor(id_proveedor).listar_modelos(api_key, timeout=timeout)


__all__ = [
    'ErrorProveedorIA', 'ProveedorLLM', 'RespuestaLLM', 'PROVEEDORES',
    'obtener_proveedor', 'listar_modelos',
]
