"""El presupuesto declarado, listo para comparar con lo observado.

Vive aparte porque lo consultan tres pantallas (el panel de movimientos, el
análisis del mes y la conciliación) y todas necesitan exactamente lo mismo:
cuánto hay declarado al mes para cada categoría y para cada bloque.

Los gastos NO mensuales se prorratean (`importe_mensual` ya lo hace): un IBI de
520 € al año son 43 €/mes de límite, que es contra lo que se compara un mes.
"""

from collections import defaultdict
from decimal import Decimal


def _partidas(hogar, solo_mensuales=False):
    """Las partidas activas del hogar.

    `solo_mensuales` deja fuera los gastos que se pagan de golpe (IBI, seguro,
    revisión del coche). Se usa al mirar UN mes: ese gasto se provisiona todo el
    año y se paga en uno, así que compararlo contra el mes en el que cae dice
    que te has arruinado, y contra los otros once, que eres un ahorrador. En esa
    vista se sacan sus pagos del gasto Y su provisión del límite, o la
    comparación queda coja por un lado. Sobre doce meses ambos lados se
    promedian bien y no hace falta.
    """
    from .models import PartidaGasto

    qs = PartidaGasto.objects.filter(hogar=hogar, activo=True).select_related('categoria')
    if solo_mensuales:
        qs = qs.filter(periodicidad='mensual')
    return qs


def _declarado(partida, anual):
    """Lo declarado en una partida, al mes o al año.

    El anual NO es el mensual por doce: `importe_mensual` viene redondeado a
    céntimos, y multiplicarlo convierte un IBI de 520 € en uno de 519,96. Sobre
    un año esa diferencia se ve, y el bloque de los anuales se juzga justo
    contra ese número.
    """
    return partida.importe_anual if anual else partida.importe_mensual


def por_categoria(hogar, solo_mensuales=False, anual=False):
    """{categoria_id: importe declarado}, al mes o —con `anual`— al año.

    Las partidas declaradas para el bloque entero no aportan a ninguna
    categoría: precisamente existen porque el usuario NO sabe en qué categorías
    se va a repartir ese dinero.
    """
    totales = defaultdict(lambda: Decimal('0'))
    for p in _partidas(hogar, solo_mensuales):
        if p.categoria_id:
            totales[p.categoria_id] += _declarado(p, anual)
    return dict(totales)


def por_bloque(hogar, solo_mensuales=False, anual=False):
    """{tipo de bloque: importe declarado}, al mes o —con `anual`— al año.

    Cuando el bloque tiene un presupuesto propio declarado, ESE es su techo y no
    se le suma el de sus categorías: «tengo 1.500 € para caprichos, de los
    cuales 200 para restaurantes» son 1.500, no 1.700. Las categorías que sí
    tengan límite siguen teniéndolo como desglose informativo dentro del techo.

    Sin presupuesto de bloque declarado se comporta como siempre: el techo es la
    suma de lo declarado en sus categorías.
    """
    de_bloque = defaultdict(lambda: Decimal('0'))
    de_categorias = defaultdict(lambda: Decimal('0'))
    for p in _partidas(hogar, solo_mensuales):
        tipo = p.tipo_bloque
        if not tipo:
            continue
        if p.es_del_bloque:
            de_bloque[tipo] += _declarado(p, anual)
        else:
            de_categorias[tipo] += _declarado(p, anual)

    return {
        tipo: de_bloque.get(tipo) or de_categorias[tipo]
        for tipo in set(de_bloque) | set(de_categorias)
    }


def techo_de_bloque(hogar):
    """Solo los bloques con un techo propio declarado, para poder decir en la
    pantalla que ese límite es del bloque y no la suma de sus categorías."""
    totales = defaultdict(lambda: Decimal('0'))
    for p in _partidas(hogar):
        if p.es_del_bloque and p.tipo_bloque:
            totales[p.tipo_bloque] += p.importe_mensual
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
