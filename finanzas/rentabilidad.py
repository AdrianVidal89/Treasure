"""Cuánto renta una propiedad: la real si está alquilada y la simulada si no.

No hay una sola rentabilidad: cada cifra contesta una pregunta distinta.

* BRUTA = alquiler del año / lo que costó (precio + gastos de compra).
  La que se usa para comparar anuncios. Exagera: no descuenta nada.
* NETA = (alquiler − gastos del piso) / lo que costó. La principal: lo que
  renta el piso en sí, sin contar cómo se pagó. Los gastos son IBI,
  comunidad, seguro, reparaciones… La hipoteca NO: es financiación, no un
  gasto del piso, y metiéndola dos pisos iguales rentarían distinto solo por
  cómo se compraron.
* NETA TRAS IRPF = lo anterior menos el impuesto del alquiler, con el mismo
  cálculo que la declaración (`alquiler.impuesto_del_alquiler`).
* LA HIPOTECA se lee de su cuadro si está declarada (las doce cuotas
  siguientes) y se parte en INTERESES —un gasto, que deduce en el IRPF— y
  CAPITAL —ahorro: sale del bolsillo pero se queda en el piso—.
  TE QUEDA (caja) = alquiler − gastos − IRPF − cuota entera.
  GANANCIA REAL = caja + capital = alquiler − gastos − IRPF − intereses.
* SOBRE TU CAPITAL = ganancia real / capital liberable (lo que sacarías si
  vendieras hoy: valor − deuda − gastos de venta − plusvalía). Es lo que de
  verdad te rinde el dinero que tienes metido en el piso.
* MANTENER FRENTE A VENDER = ganancia real − capital liberable × tipo de
  referencia, en €/año: lo que ganas (o pierdes) por no vender y poner ese
  dinero a rendir fuera.
  Sin la hipoteca declarada se mantiene la cuenta de antes (SOBRE TU DINERO
  = caja / lo que pusiste al comprar) y se avisa.
* SOBRE EL VALOR ACTUAL = neto / lo que vale hoy. No dice si la compra fue
  buena, sino si hoy compensa tener ese dinero en el piso.

La REVALORIZACIÓN va aparte y no se suma: es real, pero no se cobra hasta
vender.

La real se mide en los ÚLTIMOS 12 MESES CERRADOS (no el año natural: en
octubre el año natural son nueve meses), o desde que se alquila si es menos,
llevado a un año. Los recibos que se pagan una vez al año (IBI, seguro)
cuentan una vez, no multiplicados.
"""
import datetime
from datetime import date
from decimal import Decimal, InvalidOperation

from . import costes_activo
from .alquiler import (CERO, _es_hipoteca, _es_reparacion, amortizacion_anual, impuesto_del_alquiler,
                       rendimiento_neto)

CIEN = Decimal('100')
TIPO_REFERENCIA = Decimal('3')

# Lo que se propone en el simulador cuando no se ha guardado nada.
SIMULACION_POR_DEFECTO = {
    'renta_mensual': '',
    'meses_ocupados': '11',
    'mantenimiento_pct': '5',
    'seguro_impago_pct': '4',
    'gestion_pct': '0',
}
CAMPOS_SIMULACION = (
    'renta_mensual', 'meses_ocupados', 'gastos_fijos_anuales', 'cuota_hipoteca_anual',
    'mantenimiento_pct', 'seguro_impago_pct', 'gestion_pct', 'reduccion_pct',
)


def _pct(parte, total):
    if not total or total <= 0:
        return None
    return round(float(parte / total * CIEN), 2)


def _sumar_meses(dia, meses):
    total = dia.year * 12 + dia.month - 1 + meses
    return date(total // 12, total % 12 + 1, 1)


def _ultimo_mes_cerrado(hoy):
    return hoy.replace(day=1) - datetime.timedelta(days=1)


def _gastos_por_tipo(propiedad, movimientos):
    """(corrientes, recibos anuales, hipoteca, reparaciones) de unos
    movimientos de gasto. Las reparaciones van también dentro de corrientes
    o recibos: se dicen aparte porque tienen tope en el IRPF.

    El IRPF del alquiler pagado como gasto del piso no entra: el impuesto se
    calcula aparte y contarlo además sería restarlo dos veces."""
    corrientes = provisiones = hipoteca = reparaciones = CERO
    for m in movimientos:
        if not m.cuenta_como_gasto:
            continue
        if propiedad.partida_irpf_id and m.partida_conciliada_id == propiedad.partida_irpf_id:
            continue
        importe = -m.importe_neto
        if _es_hipoteca(m):
            hipoteca += importe
            continue
        if _es_reparacion(m):
            reparaciones += importe
        if m.es_pago_provision:
            provisiones += importe
        else:
            corrientes += importe
    return corrientes, provisiones, hipoteca, reparaciones


def tipo_referencia(propiedad):
    return propiedad.tipo_referencia_pct if propiedad.tipo_referencia_pct is not None else TIPO_REFERENCIA


def _cascada(pasos):
    """Cada paso es un tramo [bajo, alto] sobre la misma escala; puede bajar
    de cero. `pasos`: [(clave, nombre, importe, encadenado)]: los
    encadenados suben o bajan desde donde quedó el anterior; los demás
    (el resultado) van de cero a su importe."""
    tramos, nivel = [], CERO
    for clave, nombre, importe, encadenado in pasos:
        if encadenado:
            antes, nivel = nivel, nivel + importe
            tramos.append((clave, nombre, importe, min(antes, nivel), max(antes, nivel)))
        else:
            tramos.append((clave, nombre, importe, min(CERO, importe), max(CERO, importe)))
    minimo = min(t[3] for t in tramos)
    maximo = max(max(t[4] for t in tramos), Decimal('1'))
    escala = maximo - minimo
    return [{
        'clave': clave, 'nombre': nombre, 'importe': importe,
        'izq': round(float((bajo - minimo) / escala * CIEN), 2),
        'ancho': max(round(float((alto - bajo) / escala * CIEN), 2), 0.4),
    } for clave, nombre, importe, bajo, alto in tramos
        if importe or clave in ('renta', 'flujo', 'ganancia')]


def _metricas(propiedad, renta, gastos, hipoteca, pct_reduccion, anio, hoy, reparaciones=CERO):
    """Todas las cifras a partir del año tipo: alquiler, gastos del piso (con
    las reparaciones dentro) y cuota de hipoteca anuales. Lo comparten la
    real y la simulada.

    Con la hipoteca DECLARADA, la cuota sale de su cuadro (las doce cuotas
    siguientes) y se parte en intereses —gasto, deducen en el IRPF— y
    capital —ahorro, se queda en el piso—. Sin hipoteca ninguna, igual con
    ceros. Con una hipoteca sin declarar (hay cuota en los movimientos o
    deuda puesta a mano) se mantiene la cuenta de antes y se avisa."""
    from . import hipoteca_propiedad

    coste = propiedad.coste_base
    cuadro = hipoteca_propiedad.proximos_12(propiedad, hoy)
    hipoteca_banco = hipoteca
    if cuadro is not None:
        modo = 'cuadro'
        intereses, capital = cuadro['intereses'], cuadro['capital']
        hipoteca = intereses + capital
    elif not hipoteca and not propiedad.deuda_hipotecaria:
        modo = 'sin_hipoteca'
        intereses = capital = CERO
    else:
        modo = 'sin_declarar'
        intereses = propiedad.intereses_hipoteca_anuales or CERO
        capital = max(hipoteca - intereses, CERO) if hipoteca and intereses else None

    amortizacion, base_amortizacion = amortizacion_anual(propiedad)
    rn = rendimiento_neto(renta, gastos, reparaciones, intereses, amortizacion)
    rendimiento = rn['rendimiento']
    reduccion = (rendimiento * pct_reduccion / CIEN).quantize(Decimal('0.01')) if rendimiento > 0 else CERO
    _, titulares, impuesto = impuesto_del_alquiler(propiedad, rendimiento - reduccion, anio)

    neto = renta - gastos
    neto_irpf = neto - impuesto
    flujo = neto_irpf - hipoteca            # la caja: lo que queda tras pagar la cuota entera
    ganancia = flujo + capital if capital is not None else None   # caja + lo que amortizas

    # Lo que sacarías si vendieras hoy: contra eso se mide lo que te rinde.
    venta = propiedad.calcular_neto_venta()
    liberable = Decimal(str(venta['liberable']))
    referencia = tipo_referencia(propiedad)
    sobre_capital = _pct(ganancia, liberable) if modo != 'sin_declarar' and ganancia is not None else None
    mantener = (
        (ganancia - liberable * referencia / CIEN).quantize(Decimal('1'))
        if sobre_capital is not None else None
    )

    # Lo de antes, solo sin declarar la hipoteca: sobre lo que pusiste al
    # comprar, con la cuota entera restada.
    prestamo = propiedad.hipoteca_inicial
    falta_prestamo = prestamo is None and (propiedad.deuda_hipotecaria > 0 or hipoteca > 0)
    capital_propio = None if falta_prestamo else coste - (prestamo or CERO)
    if capital_propio is not None and capital_propio <= 0:
        capital_propio = None
    sobre_tu_dinero = _pct(flujo, capital_propio) if capital_propio and modo == 'sin_declarar' else None
    sobre_tu_dinero_con_capital = (
        _pct(flujo + capital, capital_propio)
        if sobre_tu_dinero is not None and capital is not None else None
    )

    pasos = [('renta', 'Alquiler', renta, True), ('gastos', 'Gastos del piso', -gastos, True),
             ('irpf', 'IRPF', -impuesto, True)]
    if modo == 'sin_declarar':
        pasos.append(('hipoteca', 'Hipoteca', -hipoteca, True))
    else:
        pasos += [('intereses', 'Intereses', -intereses, True),
                  ('capital', 'Capital (ahorro)', -capital, True)]
    pasos.append(('flujo', 'Te queda (caja)', flujo, False))
    if ganancia is not None and capital:
        pasos.append(('ganancia', 'Ganancia real', ganancia, False))

    return {
        'modo': modo,
        'renta': renta,
        'gastos': gastos,
        'reparaciones': reparaciones,
        'hipoteca': hipoteca,
        'hipoteca_banco': hipoteca_banco,
        'intereses': intereses,
        'capital_amortizado': capital,
        'cuadro_estimado': bool(cuadro and cuadro['estimadas']),
        'amortizacion': amortizacion,
        'base_amortizacion': base_amortizacion,
        'rendimiento': rendimiento,
        'exceso_arrastrable': rn['exceso_arrastrable'],
        'pct_reduccion': pct_reduccion,
        'reduccion': reduccion,
        'impuesto': impuesto,
        'titulares': titulares,
        'neto': neto,
        'neto_irpf': neto_irpf,
        'flujo': flujo,
        'flujo_mes': (flujo / 12).quantize(Decimal('1')),
        'ganancia': ganancia,
        'ganancia_mes': (ganancia / 12).quantize(Decimal('1')) if ganancia is not None else None,
        'coste': coste,
        'liberable': liberable,
        'venta': venta,
        'sobre_capital': sobre_capital,
        'tipo_referencia': referencia,
        'mantener_vs_vender': mantener,
        'capital_propio': capital_propio,
        'falta_prestamo': falta_prestamo,
        'bruta': _pct(renta, coste),
        'neta': _pct(neto, coste),
        'neta_irpf': _pct(neto_irpf, coste),
        'sobre_valor': _pct(neto, propiedad.valor_actual),
        'sobre_tu_dinero': sobre_tu_dinero,
        'sobre_tu_dinero_con_capital': sobre_tu_dinero_con_capital,
        'cascada': _cascada(pasos),
        'revalorizacion': revalorizacion(propiedad, hoy),
    }


def revalorizacion(propiedad, hoy=None):
    """Lo que ha ganado (o perdido) de valor desde la compra. Aparte: no se
    cobra hasta vender."""
    hoy = hoy or date.today()
    coste = propiedad.coste_base
    total = propiedad.valor_actual - coste
    anios = (hoy - propiedad.fecha_compra).days / 365.25 if propiedad.fecha_compra else 0
    anual = None
    if anios >= 1 and coste > 0 and propiedad.valor_actual > 0:
        anual = round(((float(propiedad.valor_actual) / float(coste)) ** (1 / anios) - 1) * 100, 2)
    return {
        'total': total,
        'pct': _pct(total, coste),
        'anual': anual,
        'anios': round(anios, 1),
    }


# ─── La real ─────────────────────────────────────────────────────────────────

def rentabilidad_real(propiedad, hoy=None):
    """Rentabilidad de una propiedad alquilada en los últimos 12 meses
    cerrados (o desde que se alquila), llevada a un año. None si aún no hay
    ni un mes cerrado con el piso alquilado."""
    hoy = hoy or date.today()
    todos = list(costes_activo._movimientos(propiedad))
    ingresos = [m for m in todos if m.cuenta_como_ingreso]

    fin = _ultimo_mes_cerrado(hoy)
    inicio = _sumar_meses(fin.replace(day=1), -11)
    if propiedad.alquilada_desde:
        desde_alquiler = propiedad.alquilada_desde.replace(day=1)
    elif ingresos:
        desde_alquiler = min(m.fecha for m in ingresos).replace(day=1)
    else:
        return None
    inicio = max(inicio, desde_alquiler)
    if inicio > fin:
        return None
    meses = (fin.year - inicio.year) * 12 + fin.month - inicio.month + 1

    en_ventana = [m for m in todos if inicio <= m.fecha <= fin]
    cobrado = sum((m.importe for m in en_ventana if m.cuenta_como_ingreso), CERO)
    corrientes, provisiones, hipoteca, reparaciones = _gastos_por_tipo(propiedad, en_ventana)

    factor = Decimal(12) / meses
    renta = (cobrado * factor).quantize(Decimal('0.01'))
    # Los recibos anuales cuentan una vez: llevarlos a un año multiplicando
    # haría que el IBI pagado en el único trimestre alquilado contara cuatro.
    gastos = (corrientes * factor + provisiones).quantize(Decimal('0.01'))
    hipoteca = (hipoteca * factor).quantize(Decimal('0.01'))

    datos = _metricas(
        propiedad, renta, gastos, hipoteca,
        Decimal(propiedad.reduccion_alquiler_pct or 0), fin.year, hoy,
        reparaciones=(reparaciones * factor).quantize(Decimal('0.01')),
    )
    datos.update({
        'inicio': inicio,
        'fin': fin,
        'meses': meses,
        'anualizado': meses < 12,
        'cobrado': cobrado,
    })
    return datos


# ─── La simulada ─────────────────────────────────────────────────────────────

def _decimal(valor, defecto=None):
    if valor in (None, ''):
        return defecto
    try:
        return Decimal(str(valor).replace(',', '.'))
    except InvalidOperation:
        return defecto


def lo_que_ya_cuesta(propiedad, hoy=None):
    """Gastos fijos y cuota de hipoteca de los últimos 12 meses cerrados: lo
    que ya pagas por la propiedad, alquilada o no. Si no hay movimientos
    imputados, lo presupuestado en sus partidas."""
    hoy = hoy or date.today()
    fin = _ultimo_mes_cerrado(hoy)
    inicio = _sumar_meses(fin.replace(day=1), -11)
    movs = [m for m in costes_activo._movimientos(propiedad) if inicio <= m.fecha <= fin]
    corrientes, provisiones, hipoteca, _ = _gastos_por_tipo(propiedad, movs)
    fijos = corrientes + provisiones
    origen = 'movimientos'
    if not fijos and not hipoteca:
        partidas = [p for p in costes_activo._partidas(propiedad)
                    if p.id != propiedad.partida_irpf_id]
        fijos = sum((p.importe_anual for p in partidas
                     if not any(k in (p.categoria.nombre if p.categoria else p.nombre).lower()
                                for k in ('hipoteca', 'prestamo', 'préstamo'))), CERO)
        hipoteca = sum((p.importe_anual for p in partidas), CERO) - fijos
        origen = 'partidas' if fijos or hipoteca else None
    return {
        'gastos_fijos_anuales': fijos.quantize(Decimal('0.01')),
        'cuota_hipoteca_anual': hipoteca.quantize(Decimal('0.01')),
        'origen': origen,
    }


def _texto(numero):
    """1130.00 → «1130»; 12.50 → «12.5». Para rellenar los campos."""
    texto = f'{numero:.2f}'
    return texto.rstrip('0').rstrip('.') if '.' in texto else texto


def escenario(propiedad, hoy=None):
    """Los valores del simulador: lo guardado o, si no, lo propuesto."""
    ya = lo_que_ya_cuesta(propiedad, hoy)
    valores = dict(SIMULACION_POR_DEFECTO)
    valores['gastos_fijos_anuales'] = _texto(ya['gastos_fijos_anuales'])
    valores['cuota_hipoteca_anual'] = _texto(ya['cuota_hipoteca_anual'])
    valores['reduccion_pct'] = str(propiedad.reduccion_alquiler_pct or 0)
    guardado = propiedad.simulacion_alquiler or {}
    valores.update({k: v for k, v in guardado.items() if k in CAMPOS_SIMULACION})
    try:
        fecha = date.fromisoformat(guardado['guardado'])
    except (KeyError, TypeError, ValueError):
        fecha = None
    return valores, ya, fecha


def leer_escenario(post):
    """Los valores del formulario del simulador, validados. Lanza ValueError."""
    valores = {}
    for campo in CAMPOS_SIMULACION:
        texto = (post.get(campo) or '').strip()
        numero = _decimal(texto)
        if texto and numero is None:
            raise ValueError(f'«{texto}» no es un número.')
        if numero is not None and numero < 0:
            raise ValueError('Los importes no pueden ser negativos.')
        valores[campo] = _texto(numero) if numero is not None else ''
    if not valores['renta_mensual']:
        raise ValueError('Indica la renta mensual que esperas cobrar.')
    meses = _decimal(valores['meses_ocupados'], Decimal('12'))
    if meses > 12:
        raise ValueError('Un año tiene 12 meses como mucho.')
    return valores


def simular(propiedad, valores, hoy=None):
    """La rentabilidad si se alquilara con el escenario dado. None sin renta."""
    hoy = hoy or date.today()
    renta_mensual = _decimal(valores.get('renta_mensual'))
    if not renta_mensual:
        return None
    meses = _decimal(valores.get('meses_ocupados'), Decimal('12'))
    fijos = _decimal(valores.get('gastos_fijos_anuales'), CERO)
    hipoteca = _decimal(valores.get('cuota_hipoteca_anual'), CERO)
    pct_extra = sum((_decimal(valores.get(k), CERO)
                     for k in ('mantenimiento_pct', 'seguro_impago_pct', 'gestion_pct')), CERO)

    renta = (renta_mensual * meses).quantize(Decimal('0.01'))
    extra = (renta * pct_extra / CIEN).quantize(Decimal('0.01'))
    mantenimiento = (renta * _decimal(valores.get('mantenimiento_pct'), CERO) / CIEN).quantize(Decimal('0.01'))
    gastos = fijos + extra
    datos = _metricas(
        propiedad, renta, gastos, hipoteca,
        _decimal(valores.get('reduccion_pct'), CERO), hoy.year, hoy,
        reparaciones=mantenimiento,
    )
    # Frente a tenerla vacía: los gastos fijos y la hipoteca se pagan igual,
    # así que lo que cambia al alquilar es la renta menos lo que el alquiler
    # añade (mantenimiento, impago, gestión) y menos su IRPF. La hipoteca, la
    # del cuadro si está declarada.
    vacia = -(fijos + datos['hipoteca'])
    datos.update({
        'renta_mensual': renta_mensual,
        'meses_ocupados': meses,
        'gastos_fijos': fijos,
        'gastos_extra': extra,
        'pct_extra': pct_extra,
        'vacia': vacia,
        'frente_a_vacia': datos['flujo'] - vacia,
        'frente_a_vacia_mes': ((datos['flujo'] - vacia) / 12).quantize(Decimal('1')),
    })
    return datos
