"""Las hipotecas declaradas de una propiedad, leídas desde su cuadro.

El cálculo vive en `amortizacion.py` (puro, sin Django). Aquí está lo que
une ese cuadro con el resto de la app:

* la DEUDA de hoy —y la de cada mes en Evolución— sale del cuadro, no de un
  número puesto a mano que envejece cada mes;
* lo que CUESTA la propiedad se separa en coste real (gastos + intereses) y
  ahorro en capital: la parte de la cuota que devuelve préstamo no es un
  gasto, es dinero que se queda en la casa;
* la CONCILIACIÓN compara el cuadro con lo que de verdad cobró el banco.

Una propiedad sin hipoteca declarada sigue como estaba: todas las funciones
devuelven None y quien las llama usa los campos de siempre.
"""
import datetime
from calendar import monthrange
from decimal import Decimal

from . import amortizacion

CERO = Decimal('0')


def _d(x):
    return Decimal(str(round(x, 2)))


def activas(propiedad):
    """Las hipotecas activas de la propiedad (una consulta por propiedad y
    petición: se guardan en la instancia)."""
    cache = propiedad.__dict__.get('_hipotecas_activas')
    if cache is None:
        cache = list(propiedad.hipotecas.filter(activa=True)
                     .prefetch_related('revisiones', 'amortizaciones'))
        propiedad.__dict__['_hipotecas_activas'] = cache
    return cache


def olvidar(propiedad):
    """Tras cambiar una hipoteca, que la próxima lectura vuelva a la base."""
    propiedad.__dict__.pop('_hipotecas_activas', None)


HISTORIA_MESES = 24


def cuadro_con_historia(hipoteca):
    """El cuadro y, si empieza en un punto de partida conocido, las cuotas de
    los dos años anteriores estimadas hacia atrás (`estimada: True`): las
    necesitan la declaración del año y la rentabilidad de los últimos doce
    meses."""
    cache = hipoteca.__dict__.setdefault('_cuadros', {})
    if 'historia' not in cache:
        filas = hipoteca.cuadro()
        cache['historia'] = amortizacion.historia_previa(
            hipoteca.como_prestamo(), filas, HISTORIA_MESES) + filas
    return cache['historia']


def filas(propiedad, con_historia=False):
    """Las filas de todos los cuadros de la propiedad, juntas. Sin historia
    estimada salvo que se pida: para conciliar con el banco solo vale el
    cuadro de verdad."""
    return [f for h in activas(propiedad)
            for f in (cuadro_con_historia(h) if con_historia else h.cuadro())]


def _sumar(filas_):
    total = {'cuotas': 0, 'pagado': CERO, 'intereses': CERO, 'capital': CERO,
             'extra': CERO, 'comision': CERO, 'estimadas': 0}
    meses = set()
    for f in filas_:
        meses.add((f['fecha'].year, f['fecha'].month))
        total['pagado'] += _d(f['cuota'])
        total['intereses'] += _d(f['intereses'])
        total['capital'] += _d(f['capital'])
        total['extra'] += _d(f['extra'])
        total['comision'] += _d(f['comision'])
        total['estimadas'] += 1 if f.get('estimada') else 0
    total['cuotas'] = len(meses)
    return total


def entre(propiedad, desde, hasta):
    """Intereses, capital, cuotas… de las cuotas cobradas entre dos fechas
    (incluidas), sumando sus hipotecas e incluida la historia estimada.
    None sin hipoteca declarada."""
    if not activas(propiedad):
        return None
    return _sumar([f for f in filas(propiedad, True) if desde <= f['fecha'] <= hasta])


def proximos_12(propiedad, hoy=None):
    """Las doce cuotas siguientes a hoy: el AÑO TIPO de la hipoteca para la
    rentabilidad. Es lo que va a pasar, no lo que pasó con tipos de antes."""
    hs = activas(propiedad)
    if not hs:
        return None
    hoy = hoy or datetime.date.today()
    return _sumar([f for h in hs for f in [x for x in h.cuadro() if x['fecha'] > hoy][:12]])


def deuda(propiedad, dia=None):
    """Capital pendiente de todas sus hipotecas un día (hoy si no se dice).
    None si no tiene ninguna declarada."""
    hs = activas(propiedad)
    if not hs:
        return None
    dia = dia or datetime.date.today()
    return sum((h.saldo_a(dia) for h in hs), CERO).quantize(Decimal('0.01'))


def deuda_fin_de_mes(propiedad, anio, mes):
    if not (1 <= mes <= 12 and 1900 < anio < 3000):
        return None
    ultimo = datetime.date(anio, mes, monthrange(anio, mes)[1])
    return deuda(propiedad, ultimo)


def capital_prestado(propiedad):
    hs = activas(propiedad)
    if not hs:
        return None
    return sum((h.capital_inicial for h in hs), CERO)


def del_anio(propiedad, anio):
    """Intereses, capital, cuotas… del año natural, sumando sus hipotecas
    (con la historia estimada si el cuadro empieza a mitad de año)."""
    return entre(propiedad, datetime.date(anio, 1, 1), datetime.date(anio, 12, 31))


def _de_los_meses(propiedad, anio, hasta_mes):
    """Intereses y capital de las cuotas de `anio` hasta `hasta_mes`, y en
    cuántos meses hubo cuota."""
    intereses = capital = 0.0
    meses = set()
    for f in filas(propiedad, con_historia=True):
        if f['fecha'].year == anio and f['fecha'].month <= hasta_mes:
            intereses += f['intereses']
            capital += f['capital']
            meses.add(f['fecha'].month)
    return _d(intereses), _d(capital), len(meses)


def _es_pago_hipoteca(propiedad, m):
    from .alquiler import _es_hipoteca
    partidas = {h.partida_id for h in activas(propiedad)} - {None}
    return _es_hipoteca(m) or (m.partida_conciliada_id in partidas)


def coste_mensual(propiedad, costes):
    """Lo que cuesta al mes, separando la cuota en intereses y capital.

    `costes` es lo que devuelve `costes_activo.costes` para el año: su
    `ritmo_mensual` lleva la cuota entera si se paga desde movimientos
    imputados a la casa. Se quita esa cuota y se pone en su sitio lo que dice
    el cuadro para los mismos meses cerrados:

        coste real       = gastos de la casa + intereses
        ahorro en capital = la parte de la cuota que devuelve préstamo

    Si la cuota no pasa por la casa (se paga desde otra partida), el ritmo no
    la llevaba y aquí se añaden igual sus intereses: son coste de tenerla.
    None si no hay hipoteca declarada."""
    if not activas(propiedad):
        return None
    anio = costes['anio']
    meses = costes['meses_cerrados']
    divisor = Decimal(meses or 1)
    cargada = sum(
        (-m.importe_neto for m in costes['movimientos']
         if not m.es_pago_provision and m.fecha.month <= meses and _es_pago_hipoteca(propiedad, m)),
        CERO,
    )
    hipoteca_en_ritmo = (cargada / divisor).quantize(Decimal('0.01')) if meses else CERO
    intereses, capital, con_cuota = _de_los_meses(propiedad, anio, meses) if meses else (CERO, CERO, 0)
    if con_cuota:
        # Por mes con cuota, no por mes cerrado: una hipoteca que empieza (o
        # se declara desde un punto de partida) a mitad de año no cuesta
        # menos al mes por haber cobrado menos meses.
        intereses_mes = (intereses / con_cuota).quantize(Decimal('0.01'))
        capital_mes = (capital / con_cuota).quantize(Decimal('0.01'))
    else:
        # Sin meses cerrados (enero, o un año futuro): lo que dice el cuadro
        # para el año entero.
        a = del_anio(propiedad, anio)
        n = Decimal(a['cuotas'] or 1)
        intereses_mes = (a['intereses'] / n).quantize(Decimal('0.01'))
        capital_mes = (a['capital'] / n).quantize(Decimal('0.01'))
    gastos = max(costes['ritmo_mensual'] - hipoteca_en_ritmo, CERO)
    return {
        'gastos': gastos,
        'intereses': intereses_mes,
        'capital': capital_mes,
        'cuota': intereses_mes + capital_mes,
        'coste_real': gastos + intereses_mes,
        'sale_del_bolsillo': gastos + intereses_mes + capital_mes,
        'hipoteca_en_movimientos': hipoteca_en_ritmo,
    }


def pagos_reales(propiedad, desde=None, hasta=None):
    """[(fecha, importe)] de lo que el banco cobró de hipoteca: movimientos
    imputados a la casa con categoría de hipoteca o préstamo, y los
    conciliados con la partida de alguna de sus hipotecas."""
    from extractos.models import MovimientoBancario
    from . import costes_activo

    vistos = {}
    for m in costes_activo._movimientos(propiedad):
        if m.cuenta_como_gasto and _es_pago_hipoteca(propiedad, m):
            vistos[m.pk] = m
    partidas = [h.partida_id for h in activas(propiedad) if h.partida_id]
    if partidas:
        for m in (MovimientoBancario.objects.filter(partida_conciliada_id__in=partidas)
                  .select_related('categoria', 'partida_conciliada', 'dividido_de')
                  .prefetch_related('partes', 'reembolsos')):
            if m.cuenta_como_gasto:
                vistos.setdefault(m.pk, m)
    pagos = [(m.fecha, -m.importe_neto) for m in vistos.values()]
    return [(f, i) for f, i in pagos if (not desde or f >= desde) and (not hasta or f <= hasta)]


def conciliacion(propiedad, hoy=None, meses=12):
    """Los meses de los últimos `meses` cerrados en que lo cobrado no cuadra
    con el cuadro. None si no hay hipoteca o no hay pagos que comparar."""
    if not activas(propiedad):
        return None
    hoy = hoy or datetime.date.today()
    fin = hoy.replace(day=1) - datetime.timedelta(days=1)
    inicio = amortizacion.sumar_meses(fin.replace(day=1), -(meses - 1))
    pagos = pagos_reales(propiedad, inicio, fin)
    if not pagos:
        return {'pagos': 0, 'avisos': [], 'inicio': inicio, 'fin': fin}
    avisos = amortizacion.conciliar(filas(propiedad), [(f, float(i)) for f, i in pagos])
    return {'pagos': len(pagos), 'avisos': avisos, 'inicio': inicio, 'fin': fin}


def sincronizar(propiedad, hoy=None):
    """Deja `deuda_hipotecaria` (y la foto del mes en curso, si la hay) con
    lo que dice el cuadro. Es una copia: la usan Evolución, el asistente y
    quien no sepa de hipotecas. No hace nada sin hipoteca declarada."""
    from .models import HistorialPropiedad

    olvidar(propiedad)
    hoy = hoy or datetime.date.today()
    valor = deuda(propiedad, hoy)
    if valor is None:
        return
    if propiedad.deuda_hipotecaria != valor:
        propiedad.deuda_hipotecaria = valor
        propiedad.save(update_fields=['deuda_hipotecaria'])
    HistorialPropiedad.objects.filter(
        propiedad=propiedad, año=hoy.year, mes=hoy.month,
    ).exclude(deuda_hipotecaria=valor).update(deuda_hipotecaria=valor)
