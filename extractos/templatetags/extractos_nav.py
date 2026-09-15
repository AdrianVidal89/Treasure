"""Barra de navegación del módulo de Extractos.

Vive aquí y no en el contexto de cada vista porque son varias pantallas que la
comparten (movimientos, conciliación, reglas, categorías y la importación).
"""

from django import template

from ..models import ReglaCategorizacion

register = template.Library()


@register.inclusion_tag('extractos/_nav.html', takes_context=True)
def extractos_nav(context, activo=''):
    request = context.get('request')
    hogar = None
    perfil = getattr(getattr(request, 'user', None), 'userprofile', None)
    if perfil:
        hogar = perfil.hogar

    # El periodo viaja con las pestañas. Si no, filtrar agosto en Movimientos,
    # saltar a Análisis y volver te devuelve a «todo el histórico»: pierdes el
    # contexto y la vuelta carga todos los movimientos del hogar.
    periodo = _periodo(request)

    # Lo que queda sin clasificar se ve en el propio listado (un chip que
    # filtra), así que aquí ya no hay contador que mantener.
    num_reglas = 0
    if hogar:
        num_reglas = ReglaCategorizacion.objects.filter(hogar=hogar, activo=True).count()

    return {
        'activo': activo,
        'num_reglas': num_reglas,
        'periodo': periodo,
    }


def _periodo(request):
    """«?anio=2026&mes=8» si hay un periodo elegido, o cadena vacía.

    Solo se propagan el año y el mes: el resto de filtros (categoría, comercio,
    etiqueta) son propios de la pantalla donde se pusieron y arrastrarlos a las
    demás daría vistas vacías sin explicación.
    """
    if request is None:
        return ''
    partes = []
    for clave in ('anio', 'mes'):
        valor = (request.GET.get(clave) or '').strip()
        if valor and valor != 'all':
            partes.append(f'{clave}={valor}')
    return ('?' + '&'.join(partes)) if partes else ''
