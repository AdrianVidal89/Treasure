"""El presupuesto declarado, listo para comparar con lo observado.

Vive aparte porque lo consultan tres pantallas (el panel de movimientos, el
análisis del mes y la conciliación) y todas necesitan exactamente lo mismo:
cuánto hay declarado al mes para cada categoría y para cada bloque.

Los gastos NO mensuales se prorratean (`importe_mensual` ya lo hace): un IBI de
520 € al año son 43 €/mes de límite, que es contra lo que se compara un mes.
"""

from collections import defaultdict
from decimal import Decimal


def por_categoria(hogar):
    """{categoria_id: importe mensual declarado}."""
    from .models import PartidaGasto

    totales = defaultdict(lambda: Decimal('0'))
    for p in PartidaGasto.objects.filter(hogar=hogar, activo=True).select_related('categoria'):
        totales[p.categoria_id] += p.importe_mensual
    return dict(totales)


def por_bloque(hogar):
    """{tipo de bloque: importe mensual declarado}."""
    from .models import PartidaGasto

    totales = defaultdict(lambda: Decimal('0'))
    for p in PartidaGasto.objects.filter(hogar=hogar, activo=True).select_related('categoria'):
        if p.categoria:
            totales[p.categoria.tipo] += p.importe_mensual
    return dict(totales)


def estado(real, limite):
    """Cómo va lo gastado frente a su límite.

    Devuelve `None` en `dentro` cuando no hay límite declarado: no es que se
    cumpla ni que se incumpla, es que no hay con qué comparar, y pintarlo verde
    sería decir que todo va bien sin saberlo.
    """
    # Las claves llevan el sufijo `_gastado` a propósito: este dict se mezcla
    # con el de cada bloque o categoría, que ya trae su propio `pct` (el peso
    # sobre el total del gasto), y un `pct` suelto lo pisaba en silencio.
    if not limite or limite <= 0:
        return {
            'limite': Decimal('0'), 'dentro': None, 'exceso': Decimal('0'),
            'pct_gastado': 0, 'pct_barra': 100 if real > 0 else 0,
        }
    pct = float(real / limite * 100)
    return {
        'limite': limite,
        'dentro': real <= limite,
        'exceso': max(real - limite, Decimal('0')),
        'pct_gastado': round(pct, 1),
        # La barra se llena hasta el límite; pasarse la deja llena y en rojo.
        'pct_barra': min(pct, 100),
    }
