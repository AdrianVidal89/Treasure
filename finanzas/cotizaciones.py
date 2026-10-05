"""Cotizaciones de mercado: precio actual e histórico (Yahoo Finance).

Tres usos:

* `actualizar_precios`: refresca `ValorActualInversion` de una lista de
  activos. Lo usan el botón "Actualizar precios", la apertura de la pestaña
  Inversiones (si los precios tienen más de unos minutos) y el comando
  programado `manage.py actualizar_precios`, que corre en el servidor cada hora.
* `asegurar_historico`: descarga y guarda en `PrecioHistorico` los cierres
  diarios de un ticker que aún no estén en la base.
* `serie_evolucion`: reconstruye día a día el valor de mercado y el capital
  invertido de la cartera a partir de los movimientos (qué unidades había
  cada día) y de los cierres históricos (a qué precio cotizaban).
"""
import bisect
import datetime
import json
import logging
import math
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from django.db.models import Max, Min

from .models import (
    HistorialValorInversion,
    MovimientoInversion,
    PrecioHistorico,
    ValorActualInversion,
    posicion_desde_movimientos,
)

logger = logging.getLogger(__name__)

YAHOO_CHART = 'https://query2.finance.yahoo.com/v8/finance/chart/'
FUENTE = 'Yahoo Finance'

# Último intento de descarga del histórico por ticker (en este proceso). Evita
# volver a pedir a Yahoo en cada carga de la gráfica cuando falta un día que
# Yahoo no va a tener (fin de semana, festivo, valor que cotiza desde después).
_ULTIMO_INTENTO_HISTORICO = {}
REINTENTO_HISTORICO_SEG = 3600


def _normalizar(ticker):
    return (ticker or '').strip().upper()


def _get_json(url, timeout):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


# ─── Precio actual ───────────────────────────────────────────────────────────

def precio_actual(ticker, timeout=8):
    """Último precio de mercado del ticker, o None si Yahoo no lo da."""
    url = f'{YAHOO_CHART}{urllib.parse.quote(ticker)}?interval=1d&range=1d'
    meta = _get_json(url, timeout)['chart']['result'][0]['meta']
    precio = meta.get('regularMarketPrice') or meta.get('previousClose')
    return Decimal(str(precio)) if precio else None


def actualizar_precios(inversiones, timeout=8):
    """Refresca el precio actual de los activos actualizables con ticker.

    Cada ticker se pide UNA vez aunque esté en varias posiciones, y las
    peticiones van en paralelo para que abrir la pestaña no espere a Yahoo
    activo por activo. Devuelve (actualizados, errores) como listas de texto.
    """
    por_ticker = {}
    for inv in inversiones:
        ticker = _normalizar(inv.ticker)
        if not ticker or not inv.actualizable or inv.tipo == 'DEPOSITO':
            continue
        por_ticker.setdefault(ticker, []).append(inv)
    if not por_ticker:
        return [], []

    def pedir(ticker):
        try:
            return ticker, precio_actual(ticker, timeout), None
        except Exception as e:  # red, ticker inexistente, respuesta rara...
            return ticker, None, e

    with ThreadPoolExecutor(max_workers=min(8, len(por_ticker))) as pool:
        resultados = list(pool.map(pedir, por_ticker))

    actualizados, errores = [], []
    # Las escrituras, en el hilo principal (la conexión a la base es por hilo).
    for ticker, precio, error in resultados:
        if error is not None:
            errores.append(f"{ticker}: {error}")
            continue
        if not precio:
            errores.append(f"{ticker}: sin precio")
            continue
        for inv in por_ticker[ticker]:
            ValorActualInversion.objects.update_or_create(
                inversion=inv,
                defaults={'valor_unitario': precio, 'fuente': FUENTE},
            )
        actualizados.append(f"{ticker}: {precio}")
    return actualizados, errores


def precios_obsoletos(inversiones, max_edad):
    """¿Hay algún activo actualizable cuyo precio tenga más de `max_edad`?"""
    from django.utils import timezone
    limite = timezone.now() - max_edad
    for inv in inversiones:
        if not _normalizar(inv.ticker) or not inv.actualizable or inv.tipo == 'DEPOSITO':
            continue
        try:
            fecha = inv.valor_actual.fecha_actualizacion
        except ValorActualInversion.DoesNotExist:
            return True
        if fecha is None or fecha < limite:
            return True
    return False


# ─── Histórico de cierres ────────────────────────────────────────────────────

def descargar_historico(ticker, desde, hasta, timeout=15):
    """Cierres diarios del ticker entre dos fechas: {fecha: Decimal}."""
    inicio = int(datetime.datetime.combine(desde, datetime.time()).replace(
        tzinfo=datetime.timezone.utc).timestamp())
    fin = int(datetime.datetime.combine(hasta + datetime.timedelta(days=1), datetime.time()).replace(
        tzinfo=datetime.timezone.utc).timestamp())
    url = (f'{YAHOO_CHART}{urllib.parse.quote(ticker)}'
           f'?interval=1d&period1={inicio}&period2={fin}&events=split')
    resultado = _get_json(url, timeout)['chart']['result'][0]
    # Las marcas de tiempo son UTC; se pasan a la hora del mercado para que
    # el cierre de un día no caiga en el anterior.
    offset = resultado.get('meta', {}).get('gmtoffset') or 0
    marcas = resultado.get('timestamp') or []
    cierres = (resultado.get('indicators', {}).get('quote') or [{}])[0].get('close') or []
    datos = {}
    for ts, cierre in zip(marcas, cierres):
        if cierre is None:
            continue
        fecha = datetime.datetime.fromtimestamp(ts + offset, datetime.timezone.utc).date()
        datos[fecha] = Decimal(str(round(cierre, 8)))
    return datos


def asegurar_historico(ticker, desde, forzar=False):
    """Descarga los cierres que falten entre `desde` y ayer.

    Hoy no se guarda: mientras el mercado está abierto el "cierre" de hoy es
    el precio del momento, y ese ya lo da `ValorActualInversion`.
    Devuelve el número de cierres nuevos guardados.
    """
    ticker = _normalizar(ticker)
    if not ticker:
        return 0
    ahora = time.monotonic()
    ultimo = _ULTIMO_INTENTO_HISTORICO.get(ticker)
    if not forzar and ultimo is not None and ahora - ultimo < REINTENTO_HISTORICO_SEG:
        return 0

    ayer = datetime.date.today() - datetime.timedelta(days=1)
    rango = PrecioHistorico.objects.filter(ticker=ticker).aggregate(min=Min('fecha'), max=Max('fecha'))
    tramos = []
    if rango['min'] is None:
        tramos.append((desde, ayer))
    else:
        # Margen de unos días: el primer día con movimiento puede ser festivo.
        if desde < rango['min'] - datetime.timedelta(days=5):
            tramos.append((desde, rango['min']))
        if rango['max'] < ayer:
            tramos.append((rango['max'] + datetime.timedelta(days=1), ayer))
    tramos = [(a, b) for a, b in tramos if a <= b]
    if not tramos:
        return 0

    _ULTIMO_INTENTO_HISTORICO[ticker] = ahora
    nuevos = 0
    for a, b in tramos:
        try:
            datos = descargar_historico(ticker, a, b)
        except Exception as e:
            logger.warning("No se pudo descargar el histórico de %s: %s", ticker, e)
            continue
        filas = [PrecioHistorico(ticker=ticker, fecha=f, cierre=c)
                 for f, c in datos.items() if f <= ayer]
        PrecioHistorico.objects.bulk_create(filas, ignore_conflicts=True)
        nuevos += len(filas)
    return nuevos


# ─── Serie de evolución de la cartera ────────────────────────────────────────

def _pasos_posicion(movimientos, cartera_id):
    """[(fecha, unidades, capital)] tras cada día con movimientos.

    Sin cartera: la posición completa del activo, con el mismo criterio que
    la tabla (unidades netas y coste base). Con cartera: solo sus compras,
    con el mismo criterio que el panel de la cartera (unidades compradas y
    lo aportado, comisiones incluidas; no descuenta ventas).
    """
    pasos = []
    if cartera_id is None:
        for i, m in enumerate(movimientos):
            if i + 1 < len(movimientos) and movimientos[i + 1].fecha == m.fecha:
                continue
            pos = posicion_desde_movimientos(movimientos[:i + 1])
            pasos.append((m.fecha, pos['total_activos'], pos['coste_base_actual']))
    else:
        unidades = capital = Decimal('0')
        compras = [m for m in movimientos
                   if m.tipo == MovimientoInversion.COMPRA and m.grupo_id == cartera_id]
        for i, m in enumerate(compras):
            unidades += m.cantidad
            capital += m.cantidad * m.precio_unitario + (m.comision or Decimal('0'))
            if i + 1 < len(compras) and compras[i + 1].fecha == m.fecha:
                continue
            pasos.append((m.fecha, unidades, capital))
    return pasos


def _valor_en(fechas, valores, dia):
    """Último valor conocido en o antes de `dia` (None si no hay)."""
    i = bisect.bisect_right(fechas, dia) - 1
    return valores[i] if i >= 0 else None


def serie_evolucion(inversiones, cartera_id=None, desde=None, hasta=None,
                    max_puntos=400, descargar=True):
    """Valor de mercado y capital invertido, día a día, de los activos dados.

    El precio de cada día es el cierre de Yahoo; si no lo hay (activo sin
    ticker, o Yahoo no lo tiene) se usa lo último conocido: el precio de las
    compras/ventas y los precios guardados al actualizar. Los fines de semana
    y festivos arrastran el último cierre.
    """
    hoy = datetime.date.today()
    hasta = min(hasta or hoy, hoy)

    activos = []
    for inv in inversiones:
        if inv.tipo == 'DEPOSITO':
            continue
        movimientos = sorted(inv.movimientos.all(), key=lambda m: (m.fecha, m.id))
        pasos = _pasos_posicion(movimientos, cartera_id)
        if not pasos:
            continue
        activos.append((inv, movimientos, pasos))
    fechas_pasos = {inv.pk: [p[0] for p in pasos] for inv, _, pasos in activos}

    vacia = {'fechas': [], 'valor': [], 'invertido': [], 'inicio': None, 'sin_historico': []}
    if not activos:
        return vacia

    inicio = min(p[0][0] for _, _, p in activos)
    sin_historico = []
    precios = {}
    for inv, movimientos, pasos in activos:
        ticker = _normalizar(inv.ticker)
        conocidos = {}
        # De menos a más fiable: precio de las operaciones, precios guardados
        # al actualizar, cierre oficial y, para hoy, el precio actual.
        for m in movimientos:
            if m.tipo in (MovimientoInversion.COMPRA, MovimientoInversion.VENTA) and m.precio_unitario:
                conocidos[m.fecha] = m.precio_unitario
        for h in HistorialValorInversion.objects.filter(inversion=inv).only('fecha', 'valor_unitario'):
            conocidos[h.fecha] = h.valor_unitario
        if ticker:
            if descargar and inv.actualizable:
                asegurar_historico(ticker, pasos[0][0])
            cierres = PrecioHistorico.objects.filter(
                ticker=ticker, fecha__gte=pasos[0][0] - datetime.timedelta(days=7),
            ).values_list('fecha', 'cierre')
            hay = False
            for f, c in cierres:
                conocidos[f] = c
                hay = True
            if not hay and ticker not in sin_historico:
                sin_historico.append(ticker)
        try:
            conocidos[hoy] = inv.valor_actual.valor_unitario
        except ValorActualInversion.DoesNotExist:
            pass
        fechas = sorted(conocidos)
        precios[inv.pk] = (fechas, [conocidos[f] for f in fechas])

    desde = max(desde or inicio, inicio)
    if desde > hasta:
        return dict(vacia, inicio=inicio.isoformat(), sin_historico=sin_historico)

    dias = (hasta - desde).days
    paso = max(1, math.ceil(dias / max_puntos)) if max_puntos else 1
    muestras = [desde + datetime.timedelta(days=d) for d in range(0, dias + 1, paso)]
    if muestras[-1] != hasta:
        muestras.append(hasta)

    serie_valor, serie_invertido = [], []
    for dia in muestras:
        valor = invertido = Decimal('0')
        for inv, _, pasos in activos:
            i = bisect.bisect_right(fechas_pasos[inv.pk], dia) - 1
            if i < 0:
                continue
            _, unidades, capital = pasos[i]
            invertido += capital
            fechas, valores = precios[inv.pk]
            precio = _valor_en(fechas, valores, dia)
            if precio is not None:
                valor += unidades * precio
        serie_valor.append(round(float(valor), 2))
        serie_invertido.append(round(float(invertido), 2))

    return {
        'fechas': [d.isoformat() for d in muestras],
        'valor': serie_valor,
        'invertido': serie_invertido,
        'inicio': inicio.isoformat(),
        'sin_historico': sin_historico,
    }
