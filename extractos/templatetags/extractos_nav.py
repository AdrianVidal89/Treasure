"""Barra de navegación del módulo de Extractos.

Vive aquí y no en el contexto de cada vista porque son seis pantallas que la
comparten (movimientos, sin categorizar, conciliación, reglas, categorías y la
importación) y el contador de pendientes tiene que estar al día en todas.
"""

from django import template

from ..models import MovimientoBancario, ReglaCategorizacion

register = template.Library()


@register.inclusion_tag('extractos/_nav.html', takes_context=True)
def extractos_nav(context, activo=''):
    request = context.get('request')
    hogar = None
    perfil = getattr(getattr(request, 'user', None), 'userprofile', None)
    if perfil:
        hogar = perfil.hogar

    sin_categorizar = 0
    num_reglas = 0
    if hogar:
        sin_categorizar = MovimientoBancario.objects.filter(
            hogar=hogar, categoria__isnull=True, es_traspaso=False,
        ).count()
        num_reglas = ReglaCategorizacion.objects.filter(hogar=hogar, activo=True).count()

    return {
        'activo': activo,
        'sin_categorizar': sin_categorizar,
        'num_reglas': num_reglas,
    }
