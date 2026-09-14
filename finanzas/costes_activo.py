"""Lo que cuesta mantener un activo: lo declarado frente a lo que pasó por el banco.

Vale igual para un vehículo y para una propiedad porque la pregunta es la
misma —«¿cuánto me cuesta tener esto?»— y la respuesta se construye igual:

* TEÓRICO: lo que el hogar ha presupuestado para ese activo (las PartidaGasto
  imputadas), prorrateado a mes y a año. Es la previsión.
* REAL: lo que de verdad ha salido de la cuenta (los MovimientoBancario
  imputados). Es el hecho.

Comparar ambos es lo que convierte una lista de gastos en una respuesta: el
coche «cuesta 120 €/mes» solo si de verdad se van 120 €/mes.

La clave del activo (`vehiculo:3`, `propiedad:1`) es lo que permite que haya un
único selector en toda la interfaz en vez de uno por tipo.
"""

from collections import defaultdict
from decimal import Decimal

MESES_ES = [
    '', 'Ene', 'Feb', 'Mar', 'Abr', 'May', 'Jun',
    'Jul', 'Ago', 'Sep', 'Oct', 'Nov', 'Dic',
]

# Nombre del campo que apunta al activo, según su tipo.
CAMPO_POR_TIPO = {'vehiculo': 'vehiculo', 'propiedad': 'propiedad'}


def clave(activo):
    """«vehiculo:3» / «propiedad:1» para un objeto activo."""
    from .models import Vehiculo

    tipo = 'vehiculo' if isinstance(activo, Vehiculo) else 'propiedad'
    return f'{tipo}:{activo.pk}'


def resolver(hogar, clave_texto):
    """El activo del hogar que corresponde a una clave, o None.

    Devuelve None ante cualquier cosa rara (clave inventada, activo de otro
    hogar): la imputación es un dato del usuario, no una ruta de confianza.
    """
    from .models import Propiedad, Vehiculo

    if not clave_texto or ':' not in clave_texto:
        return None
    tipo, _, ident = clave_texto.partition(':')
    if not ident.isdigit():
        return None
    modelo = {'vehiculo': Vehiculo, 'propiedad': Propiedad}.get(tipo)
    if modelo is None:
        return None
    return modelo.objects.filter(hogar=hogar, pk=int(ident)).first()


def opciones(hogar):
    """Los activos del hogar agrupados para un selector, en un solo sitio para
    que el desplegable sea idéntico en gastos y en extractos."""
    from .models import Propiedad, Vehiculo

    return [
        {
            'etiqueta': 'Vehículos',
            'opciones': [
                {'clave': v.clave_activo, 'nombre': v.nombre}
                for v in Vehiculo.objects.filter(hogar=hogar, activo=True)
            ],
        },
        {
            'etiqueta': 'Propiedades',
            'opciones': [
                {'clave': f'propiedad:{p.pk}', 'nombre': p.nombre}
                for p in Propiedad.objects.filter(hogar=hogar, activo=True)
            ],
        },
    ]


def asignar(obj, activo):
    """Imputa el objeto al activo dado (o lo desimputa con None).

    Se limpian SIEMPRE los dos campos: sin esto, reasignar del coche a la casa
    dejaría el gasto contado en los dos sitios."""
    from .models import Vehiculo

    obj.vehiculo = activo if isinstance(activo, Vehiculo) else None
    obj.propiedad = activo if (activo is not None and not isinstance(activo, Vehiculo)) else None
    return obj


def _partidas(activo):
    from .models import PartidaGasto

    campo = 'vehiculo' if clave(activo).startswith('vehiculo') else 'propiedad'
    return (
        PartidaGasto.objects.filter(activo=True, **{campo: activo})
        .select_related('categoria')
    )


def _movimientos(activo):
    from extractos.models import MovimientoBancario

    campo = 'vehiculo' if clave(activo).startswith('vehiculo') else 'propiedad'
    return (
        MovimientoBancario.objects.filter(**{campo: activo})
        .select_related('categoria')
    )


def costes(activo, anio):
    """Coste declarado y coste real del activo en un año.

    `pct_ejecucion` es la barra que se va llenando: cuánto del presupuesto anual
    llevas gastado. Puede pasar de 100 (y entonces interesa verlo).
    """
    partidas = list(_partidas(activo))
    movimientos = [m for m in _movimientos(activo) if m.cuenta_como_gasto]
    del_anio = [m for m in movimientos if m.fecha.year == anio]

    teorico_mensual = sum((p.importe_mensual for p in partidas), Decimal('0'))
    teorico_anual = sum((p.importe_anual for p in partidas), Decimal('0'))
    real_anual = sum((-m.importe for m in del_anio), Decimal('0'))

    # Meses del año con algún movimiento del activo: la media mensual real se
    # calcula sobre ellos, no sobre doce, o un activo estrenado en noviembre
    # parecería baratísimo.
    meses_con_datos = {m.fecha.month for m in del_anio}
    real_mensual = (
        real_anual / len(meses_con_datos) if meses_con_datos else Decimal('0')
    )

    return {
        'activo': activo,
        'clave': clave(activo),
        'anio': anio,
        'partidas': partidas,
        'num_partidas': len(partidas),
        'num_movimientos': len(del_anio),
        'teorico_mensual': teorico_mensual,
        'teorico_anual': teorico_anual,
        'real_anual': real_anual,
        'real_mensual': real_mensual,
        'meses_con_datos': len(meses_con_datos),
        'diferencia_anual': real_anual - teorico_anual,
        'pct_ejecucion': _pct(real_anual, teorico_anual),
        'pct_transcurrido': _pct_transcurrido(anio),
        'por_mes': _por_mes(del_anio, teorico_mensual),
        'por_categoria': _por_categoria(partidas, del_anio),
        'movimientos': sorted(del_anio, key=lambda m: m.fecha, reverse=True),
        'anios_con_datos': sorted({m.fecha.year for m in movimientos}, reverse=True),
    }


def _pct_transcurrido(anio):
    """Qué parte del año va consumida.

    Sin esto, un 76% del presupuesto no dice nada: en diciembre es ir sobrado y
    en marzo es ir camino de duplicarlo."""
    from datetime import date

    hoy = date.today()
    if anio < hoy.year:
        return 100
    if anio > hoy.year:
        return 0
    return int(hoy.month / 12 * 100)


def _pct(real, teorico):
    if teorico <= 0:
        return None
    return int(min(real / teorico * 100, 999))


def _por_mes(movimientos, teorico_mensual):
    """Serie de doce meses: lo real de cada uno contra la previsión mensual."""
    totales = defaultdict(lambda: Decimal('0'))
    for m in movimientos:
        totales[m.fecha.month] += -m.importe

    tope = max(list(totales.values()) + [teorico_mensual, Decimal('0')])
    return [
        {
            'mes': n,
            'etiqueta': MESES_ES[n],
            'real': totales[n],
            'pct': float(totales[n] / tope * 100) if tope else 0,
            'pct_teorico': float(teorico_mensual / tope * 100) if tope else 0,
        }
        for n in range(1, 13)
    ]


def _por_categoria(partidas, movimientos):
    """Declarado y real por categoría: dónde se desvía el coste del activo."""
    filas = {}

    def _fila(categoria):
        nombre = categoria.nombre if categoria else 'Sin categorizar'
        return filas.setdefault(nombre, {
            'categoria': nombre,
            'tipo': categoria.tipo if categoria else 'sin',
            'declarado_anual': Decimal('0'),
            'real_anual': Decimal('0'),
        })

    for p in partidas:
        _fila(p.categoria)['declarado_anual'] += p.importe_anual
    for m in movimientos:
        _fila(m.categoria)['real_anual'] += -m.importe

    orden = sorted(filas.values(), key=lambda f: f['real_anual'], reverse=True)
    for f in orden:
        f['diferencia'] = f['real_anual'] - f['declarado_anual']
        f['pct'] = _pct(f['real_anual'], f['declarado_anual'])
    return orden


def resumen(activos, anio):
    """Los totales de una lista de activos, para la pantalla de listado."""
    fichas = [costes(a, anio) for a in activos]
    return {
        'fichas': fichas,
        'teorico_mensual': sum((f['teorico_mensual'] for f in fichas), Decimal('0')),
        'teorico_anual': sum((f['teorico_anual'] for f in fichas), Decimal('0')),
        'real_anual': sum((f['real_anual'] for f in fichas), Decimal('0')),
    }
