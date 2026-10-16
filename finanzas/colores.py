"""Normaliza los colores que llegan de los formularios.

Los campos `color` de los modelos guardan un hex de 7 caracteres (#rrggbb).
Los selectores de color de la interfaz usan los tokens del tema; si alguno
llega como `var(--token)` se traduce a su hex en vez de romper el guardado.
"""
import re

_HEX = re.compile(r'^#[0-9a-fA-F]{6}$')

# Valores del tema claro (static/css/main.css).
TOKENS_HEX = {
    'var(--success)': '#2d6a4f',
    'var(--info)': '#2c5f7a',
    'var(--primary-color)': '#1b4332',
    'var(--danger)': '#b4442e',
    'var(--warning)': '#b7791f',
}


def color_valido(valor, defecto='#2d6a4f'):
    """Devuelve un hex #rrggbb válido; si no se puede, `defecto`."""
    valor = (valor or '').strip()
    if _HEX.match(valor):
        return valor.lower()
    return TOKENS_HEX.get(valor.replace(' ', ''), defecto)
