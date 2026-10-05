"""Motor de amortización de préstamos (sistema francés), mes a mes.

Es el ÚNICO sitio donde se calcula cómo se paga una hipoteca: el cuadro, las
revisiones del tipo, las amortizaciones anticipadas y su comisión. Lo usan
Propiedades (deuda, intereses del año, conciliación), la rentabilidad del
alquiler y el simulador de pronto pago. `static/js/amortizacion.js` es la
misma lógica para lo que tiene que recalcularse en el navegador a cada
movimiento de un slider; los dos pasan los mismos casos de
`finanzas/amortizacion_casos.json`, así que no hay dos verdades.

Todo son funciones puras: entra un diccionario (JSON-able, el mismo que recibe
el JS) y salen listas de diccionarios. Nada de Django aquí.

El préstamo:

    {
      "capital": 323400,            # lo que se firmó
      "inicio": "2024-01-15",       # fecha de firma
      "plazo_meses": 360,
      "dia_cobro": null,            # vacío: el día de la firma
      "modalidad": "fijo",          # fijo | variable | mixto
      "tipo_inicial": 2.0,          # % fijo, o el del tramo fijo inicial
      "meses_tramo_fijo": null,     # mixto (o variable con un primer tramo fijo)
      "diferencial": null,          # variable / mixto: índice + diferencial
      "revision_meses": 12,         # cada cuánto se revisa el tipo
      "indice_futuro": null,        # supuesto del índice para proyectar
      "revisiones": [{"fecha": "2025-01-15", "tipo": 3.1}],
      "amortizaciones": [{"fecha": "2026-03-01", "importe": 10000,
                          "modo": "plazo", "comision": null}],
      "ancla": {"fecha": "2026-01-01", "saldo": 113341,
                "cuota": 472.93, "tipo": null},
      "comision": {"pct_inicial": 2, "meses_iniciales": 120, "pct_despues": 1.5}
    }

Cómo se cuenta, que es como lo hace un banco:

* La cuota k se cobra k meses después de la firma, el día de cobro. Sus
  intereses son el pendiente por el tipo del periodo / 12, redondeados al
  céntimo; la cuota también va al céntimo y la última ajusta lo que quede.
* El tipo de un periodo es el vigente el día en que empieza (ver `tipo_en`).
  Cuando cambia, la cuota se recalcula sobre lo pendiente y las cuotas que
  quedan.
* Una amortización anticipada se aplica justo después de la última cuota
  cobrada en o antes de su fecha. En modo «cuota» se recalcula la cuota sobre
  el plazo que queda; en modo «plazo» la cuota se mantiene y el préstamo
  termina antes.
* El ANCLA es un punto conocido —un recibo, el certificado del banco—: saldo
  a una fecha (y, si se sabe, la cuota). Si está, el cuadro empieza ahí: es lo
  que hay que usar cuando no se conoce la historia del préstamo o las
  revisiones pasadas de un variable.
"""
import math
from calendar import monthrange
from datetime import date

MAX_MESES = 1200

# Comisión por amortización anticipada que permite la Ley 5/2019 (art. 23).
# Son los máximos; el contrato puede fijar menos, por eso se pueden cambiar.
#   fijo:     2 % los diez primeros años, 1,5 % después.
#   variable: 0,25 % los tres primeros años, o 0,15 % los cinco primeros
#             (lo elige el contrato); después, nada.
# Un mixto se trata como fijo durante su tramo fijo y como variable después.
COMISION_FIJO = (2.0, 120, 1.5)
COMISION_VARIABLE = (0.25, 36, 0.0)
COMISION_VARIABLE_5 = (0.15, 60, 0.0)


# ── Utilidades ───────────────────────────────────────────────────────────────

def r2(x):
    """Redondeo al céntimo, mitad hacia arriba (igual en el JS: el `round`
    de Python redondea al par y daría céntimos distintos)."""
    signo = -1 if x < 0 else 1
    return signo * math.floor(abs(x) * 100 + 0.5 + 1e-9) / 100


def fecha(valor):
    if valor is None or isinstance(valor, date):
        return valor
    return date.fromisoformat(str(valor)[:10])


def sumar_meses(dia, meses, dia_fijo=None):
    total = dia.year * 12 + dia.month - 1 + meses
    anio, mes = total // 12, total % 12 + 1
    return date(anio, mes, min(dia_fijo or dia.day, monthrange(anio, mes)[1]))


def meses_entre(desde, hasta):
    """Meses completos de `desde` a `hasta`."""
    n = (hasta.year - desde.year) * 12 + hasta.month - desde.month
    if hasta.day < desde.day:
        n -= 1
    return n


def cuota(capital, tipo_anual, meses):
    """Cuota de un préstamo francés (sin redondear)."""
    if capital <= 0 or meses <= 0:
        return 0.0
    if not tipo_anual:
        return capital / meses
    r = tipo_anual / 1200
    return capital * r / (1 - (1 + r) ** -meses)


def capital_para_cuota(cuota_mensual, tipo_anual, meses):
    """Cuánto capital se paga con una cuota dada: la inversa de `cuota`."""
    if cuota_mensual <= 0 or meses <= 0:
        return 0.0
    if not tipo_anual:
        return cuota_mensual * meses
    r = tipo_anual / 1200
    return cuota_mensual * (1 - (1 + r) ** -meses) / r


def cuotas_restantes(capital, tipo_anual, cuota_mensual):
    """Cuántas cuotas hacen falta para devolver `capital` con esa cuota.
    None si la cuota no llega ni a cubrir los intereses."""
    if capital <= 0:
        return 0
    if cuota_mensual <= 0:
        return None
    r = (tipo_anual or 0) / 1200
    if r == 0:
        return math.ceil(capital / cuota_mensual - 1e-9)
    if cuota_mensual <= capital * r:
        return None
    n = -math.log(1 - capital * r / cuota_mensual) / math.log(1 + r)
    return max(1, math.ceil(n - 1e-6))


# ── Tipo y comisión ──────────────────────────────────────────────────────────

def _revisiones(p):
    revs = [(fecha(r['fecha']), float(r['tipo'])) for r in p.get('revisiones') or []]
    ancla = p.get('ancla') or {}
    if ancla.get('tipo') not in (None, ''):
        revs.append((fecha(ancla['fecha']), float(ancla['tipo'])))
    return sorted(revs)


def tipo_en(p, inicio_periodo, meses_desde_firma, revisiones=None):
    """El tipo de un periodo que empieza en `inicio_periodo`.

    Manda la última revisión conocida. Pasado su periodo de revisión, si hay
    un supuesto del índice (`indice_futuro`), un variable pasa a índice +
    diferencial: es la palanca para proyectar subidas o bajadas. Sin
    revisiones, el tipo inicial durante el tramo fijo y luego índice +
    diferencial si se conoce el índice; si no, el último tipo conocido."""
    revisiones = _revisiones(p) if revisiones is None else revisiones
    modalidad = p.get('modalidad') or 'fijo'
    tipo_inicial = float(p.get('tipo_inicial') or 0)
    tramo_fijo = int(p.get('meses_tramo_fijo') or 0)
    indice = p.get('indice_futuro')
    diferencial = p.get('diferencial')
    proyectable = (modalidad != 'fijo' and indice not in (None, '') and diferencial not in (None, '')
                   and meses_desde_firma >= tramo_fijo)

    vigente = None
    for f, t in revisiones:
        if f <= inicio_periodo:
            vigente = (f, t)
    if vigente:
        f, t = vigente
        caduca = sumar_meses(f, int(p.get('revision_meses') or 12))
        if not proyectable or inicio_periodo < caduca:
            return t
    if modalidad == 'fijo' or meses_desde_firma < tramo_fijo:
        return tipo_inicial
    if proyectable:
        return float(indice) + float(diferencial)
    return vigente[1] if vigente else tipo_inicial


def reglas_comision(p):
    """`(pct_inicial, meses_iniciales, pct_despues)` aplicables, o None si es
    un mixto sin reglas propias (se decide mes a mes, ver `pct_comision`)."""
    c = p.get('comision') or {}
    if any(c.get(k) not in (None, '') for k in ('pct_inicial', 'meses_iniciales', 'pct_despues')):
        defecto = COMISION_FIJO if (p.get('modalidad') or 'fijo') == 'fijo' else COMISION_VARIABLE
        return (
            float(c['pct_inicial']) if c.get('pct_inicial') not in (None, '') else defecto[0],
            int(c['meses_iniciales']) if c.get('meses_iniciales') not in (None, '') else defecto[1],
            float(c['pct_despues']) if c.get('pct_despues') not in (None, '') else defecto[2],
        )
    modalidad = p.get('modalidad') or 'fijo'
    if modalidad == 'fijo':
        return COMISION_FIJO
    if modalidad == 'variable':
        return COMISION_VARIABLE
    return None


def pct_comision(p, meses_desde_firma):
    reglas = reglas_comision(p)
    if reglas is None:  # mixto por defecto
        reglas = COMISION_FIJO if meses_desde_firma < int(p.get('meses_tramo_fijo') or 0) else COMISION_VARIABLE
    pct_inicial, meses_iniciales, pct_despues = reglas
    return pct_inicial if meses_desde_firma < meses_iniciales else pct_despues


def comision(p, dia, importe):
    """Comisión estimada por amortizar `importe` el día `dia`."""
    inicio = fecha(p['inicio'])
    return r2(importe * pct_comision(p, max(0, meses_entre(inicio, fecha(dia)))) / 100)


# ── El cuadro ────────────────────────────────────────────────────────────────

def fecha_pago(p, k):
    inicio = fecha(p['inicio'])
    return sumar_meses(inicio, k, p.get('dia_cobro') or inicio.day)


def cuadro(p):
    """Cuadro de amortización, una fila por cuota.

    Cada fila: n, fecha, tipo, cuota, intereses, capital (de la cuota),
    extra (amortización anticipada aplicada tras ella), comision, pendiente.
    """
    inicio = fecha(p['inicio'])
    plazo = int(p['plazo_meses'])
    revisiones = _revisiones(p)
    ancla = p.get('ancla') or {}
    ancla_fecha = fecha(ancla.get('fecha')) if ancla.get('fecha') else None

    k = 1
    pendiente = float(p['capital'])
    restantes = plazo
    cuota_fija = None
    if ancla_fecha:
        hechas = 0
        while fecha_pago(p, hechas + 1) <= ancla_fecha:
            hechas += 1
        k = hechas + 1
        pendiente = float(ancla['saldo'])
        restantes = max(plazo - hechas, 1)
        if ancla.get('cuota') not in (None, ''):
            cuota_fija = r2(float(ancla['cuota']))

    extras = sorted(
        ({'fecha': fecha(a['fecha']), 'importe': float(a['importe']),
          'modo': a.get('modo') or 'plazo', 'comision': a.get('comision')}
         for a in p.get('amortizaciones') or [] if float(a.get('importe') or 0) > 0),
        key=lambda a: a['fecha'],
    )
    if ancla_fecha:
        extras = [a for a in extras if a['fecha'] > ancla_fecha]

    filas = []
    tipo_previo = None
    cuota_actual = None
    if cuota_fija is not None:
        tipo_previo = tipo_en(p, fecha_pago(p, k - 1), k - 1, revisiones)
        cuota_actual = cuota_fija
        n = cuotas_restantes(pendiente, tipo_previo, cuota_actual)
        if n is None:
            cuota_actual = None  # no cubre ni los intereses: se recalcula
        else:
            restantes = n

    def aplicar_extras(hasta, tipo):
        """Amortiza lo que tenga fecha anterior a `hasta`. Devuelve (extra,
        comisión) y deja la cuota o el plazo ajustados."""
        nonlocal pendiente, cuota_actual, restantes, tipo_previo
        extra = com = 0.0
        while extras and extras[0]['fecha'] < hasta and pendiente > 0.005:
            a = extras.pop(0)
            importe = min(a['importe'], pendiente)
            pendiente = r2(pendiente - importe)
            extra += importe
            com += r2(float(a['comision'])) if a['comision'] not in (None, '') else comision(p, a['fecha'], importe)
            if pendiente <= 0.005:
                break
            if a['modo'] == 'cuota':
                cuota_actual = r2(cuota(pendiente, tipo, restantes))
                tipo_previo = tipo
            else:
                n = cuotas_restantes(pendiente, tipo, cuota_actual or 0)
                restantes = n if n is not None else restantes
        return extra, com

    # Lo amortizado antes de la primera cuota del cuadro.
    tipo0 = tipo_en(p, fecha_pago(p, k - 1), k - 1, revisiones)
    if cuota_actual is None:
        cuota_actual = r2(cuota(pendiente, tipo0, restantes))
        tipo_previo = tipo0
    extra0, com0 = aplicar_extras(fecha_pago(p, k), tipo0)

    while pendiente > 0.005 and len(filas) < MAX_MESES:
        tipo = tipo_en(p, fecha_pago(p, k - 1), k - 1, revisiones)
        if tipo != tipo_previo:
            cuota_actual = r2(cuota(pendiente, tipo, restantes))
            tipo_previo = tipo
        intereses = r2(pendiente * tipo / 1200)
        if cuota_actual <= intereses and restantes > 1:
            cuota_actual = r2(cuota(pendiente, tipo, restantes))
        amortiza = r2(cuota_actual - intereses)
        if amortiza >= pendiente or restantes <= 1:
            amortiza = pendiente
        pendiente = r2(pendiente - amortiza)
        restantes = max(restantes - 1, 1)
        extra, com = aplicar_extras(fecha_pago(p, k + 1), tipo)
        if not filas:
            extra, com = extra + extra0, com + com0
        filas.append({
            'n': k,
            'fecha': fecha_pago(p, k),
            'tipo': tipo,
            'cuota': r2(intereses + amortiza),
            'intereses': intereses,
            'capital': r2(amortiza),
            'extra': r2(extra),
            'comision': r2(com),
            'pendiente': pendiente,
        })
        k += 1
    return filas


# ── Lecturas del cuadro ──────────────────────────────────────────────────────

def saldo_a(p, filas, dia):
    """Lo que se debe el día `dia`: tras la última cuota cobrada ese día o
    antes (y lo amortizado con ella)."""
    dia = fecha(dia)
    previas = [f for f in filas if f['fecha'] <= dia]
    if previas:
        return previas[-1]['pendiente']
    ancla = p.get('ancla') or {}
    if ancla.get('fecha') and fecha(ancla['fecha']) <= dia:
        return float(ancla['saldo'])
    return float(p['capital']) if dia >= fecha(p['inicio']) else 0.0


def resumen_anual(filas):
    """Por año natural: cuotas, intereses, capital, extra, comisiones y lo
    pendiente al cerrar el año."""
    anios = {}
    for f in filas:
        a = anios.setdefault(f['fecha'].year, {
            'anio': f['fecha'].year, 'cuotas': 0, 'pagado': 0.0, 'intereses': 0.0,
            'capital': 0.0, 'extra': 0.0, 'comision': 0.0, 'pendiente': 0.0,
        })
        a['cuotas'] += 1
        a['pagado'] += f['cuota']
        a['intereses'] += f['intereses']
        a['capital'] += f['capital']
        a['extra'] += f['extra']
        a['comision'] += f['comision']
        a['pendiente'] = f['pendiente']
    for a in anios.values():
        for clave in ('pagado', 'intereses', 'capital', 'extra', 'comision'):
            a[clave] = r2(a[clave])
    return [anios[k] for k in sorted(anios)]


def del_anio(filas, anio):
    """Lo del año `anio` (ceros si el préstamo no tiene cuotas ese año)."""
    for a in resumen_anual(filas):
        if a['anio'] == anio:
            return a
    return {'anio': anio, 'cuotas': 0, 'pagado': 0.0, 'intereses': 0.0, 'capital': 0.0,
            'extra': 0.0, 'comision': 0.0, 'pendiente': filas[-1]['pendiente'] if filas else 0.0}


def siguientes(filas, desde, meses=12):
    """Lo de las `meses` cuotas siguientes a `desde` (año móvil): intereses y
    capital de los próximos doce meses, que no es lo mismo que el año
    natural cuando estamos en octubre."""
    desde = fecha(desde)
    tramo = [f for f in filas if f['fecha'] > desde][:meses]
    return totales(tramo)


def totales(filas):
    return {
        'cuotas': len(filas),
        'pagado': r2(sum(f['cuota'] for f in filas)),
        'intereses': r2(sum(f['intereses'] for f in filas)),
        'capital': r2(sum(f['capital'] for f in filas)),
        'extra': r2(sum(f['extra'] for f in filas)),
        'comision': r2(sum(f['comision'] for f in filas)),
        'fin': filas[-1]['fecha'] if filas else None,
        'primera_cuota': filas[0]['cuota'] if filas else 0.0,
    }


# ── Pronto pago ──────────────────────────────────────────────────────────────

def extras_periodicos(importe, desde, hasta=None, cada_meses=12, maximo=MAX_MESES):
    """Fechas e importes de una amortización que se repite."""
    desde = fecha(desde)
    hasta = fecha(hasta)
    salida = []
    i = 0
    while importe > 0 and len(salida) < maximo:
        dia = sumar_meses(desde, i * cada_meses)
        if hasta and dia > hasta:
            break
        salida.append({'fecha': dia, 'importe': importe})
        i += 1
        if i * cada_meses > MAX_MESES:
            break
    return salida


def _tir_mensual(flujos):
    """Tipo mensual que hace cero el valor actual de `flujos` (bisección).
    None si no cambian de signo."""
    if not any(f < 0 for f in flujos) or not any(f > 0 for f in flujos):
        return None

    def va(r):
        return sum(f / (1 + r) ** t for t, f in enumerate(flujos))

    bajo, alto = -0.05, 0.2   # mensual: de −60 % a +240 % al año
    if va(bajo) * va(alto) > 0:
        return None
    for _ in range(200):
        medio = (bajo + alto) / 2
        if va(bajo) * va(medio) <= 0:
            alto = medio
        else:
            bajo = medio
    return (bajo + alto) / 2


def _rentabilidad_de_amortizar(base, nuevo, desde):
    """Lo que rinde el dinero metido en amortizar: la TIR de lo que dejas de
    pagar frente a lo que adelantas (y la comisión), en % anual nominal, que
    es como se expresa el tipo de una hipoteca. Sin comisión sale el tipo
    del préstamo; la comisión lo rebaja."""
    def por_mes(filas):
        m = {}
        for f in filas:
            if f['fecha'] < desde:
                continue
            clave = f['fecha'].year * 12 + f['fecha'].month
            m[clave] = m.get(clave, 0.0) + f['cuota'] + f['extra'] + f['comision']
        return m

    b, n = por_mes(base), por_mes(nuevo)
    if not b and not n:
        return None
    meses = sorted(set(b) | set(n))
    flujos = [b.get(m, 0.0) - n.get(m, 0.0) for m in range(meses[0], meses[-1] + 1)]
    r = _tir_mensual(flujos)
    return None if r is None else round(r * 1200, 2)


def _resultado(filas):
    t = totales(filas)
    t['anual'] = resumen_anual(filas)
    return t


def simular_pronto_pago(p, puntuales=None, periodica=None, tipo_referencia=None, hoy=None):
    """Compara no hacer nada con amortizar reduciendo cuota y reduciendo plazo.

    `puntuales`: [{"fecha", "importe"}]. `periodica`: {"importe", "desde",
    "hasta"?, "cada_meses"?}. Las amortizaciones ya hechas del préstamo se
    mantienen en los tres escenarios."""
    hoy = fecha(hoy) or date.today()
    nuevas = [{'fecha': fecha(a['fecha']), 'importe': float(a['importe'])}
              for a in puntuales or [] if float(a.get('importe') or 0) > 0]
    if periodica and float(periodica.get('importe') or 0) > 0:
        nuevas += extras_periodicos(
            float(periodica['importe']), periodica.get('desde') or hoy,
            periodica.get('hasta'), int(periodica.get('cada_meses') or 12),
        )
    base = cuadro(p)
    resultado = {'base': _resultado(base), 'tipo_referencia': tipo_referencia}
    desde = min([a['fecha'] for a in nuevas], default=hoy)
    for modo in ('cuota', 'plazo'):
        q = dict(p)
        q['amortizaciones'] = list(p.get('amortizaciones') or []) + [dict(a, modo=modo) for a in nuevas]
        filas = cuadro(q)
        r = _resultado(filas)
        r['intereses_ahorrados'] = r2(resultado['base']['intereses'] - r['intereses'])
        r['comision_pagada'] = r2(r['comision'] - resultado['base']['comision'])
        r['ahorro_neto'] = r2(r['intereses_ahorrados'] - r['comision_pagada'])
        r['amortizado'] = r2(r['extra'] - resultado['base']['extra'])
        r['meses_menos'] = resultado['base']['cuotas'] - r['cuotas']
        posteriores = [f for f in filas if f['fecha'] > desde]
        r['cuota_despues'] = posteriores[0]['cuota'] if posteriores else 0.0
        r['rentabilidad'] = _rentabilidad_de_amortizar(base, filas, sumar_meses(desde, -1))
        if tipo_referencia is not None and r['rentabilidad'] is not None:
            r['frente_a_invertir'] = round(r['rentabilidad'] - float(tipo_referencia), 2)
        resultado[modo] = r
    return resultado


# ── Conciliación con lo que pasó por el banco ────────────────────────────────

def conciliar(filas, pagos, tolerancia_eur=1.0, tolerancia_pct=1.0):
    """Compara lo que dice el cuadro con lo que se pagó, mes a mes.

    `pagos`: [(fecha, importe positivo)]. Solo se miran los meses entre el
    primer y el último pago conocidos. Devuelve los meses que no cuadran:
    {anio, mes, esperado, real, diferencia}."""
    if not pagos:
        return []
    real = {}
    for dia, importe in pagos:
        dia = fecha(dia)
        clave = (dia.year, dia.month)
        real[clave] = real.get(clave, 0.0) + float(importe)
    esperado = {}
    for f in filas:
        clave = (f['fecha'].year, f['fecha'].month)
        esperado[clave] = esperado.get(clave, 0.0) + f['cuota'] + f['extra'] + f['comision']
    primero, ultimo = min(real), max(real)
    avisos = []
    for clave in sorted(set(real) | set(esperado)):
        if not (primero <= clave <= ultimo):
            continue
        e, r = r2(esperado.get(clave, 0.0)), r2(real.get(clave, 0.0))
        dif = r2(r - e)
        if abs(dif) > max(tolerancia_eur, e * tolerancia_pct / 100):
            avisos.append({'anio': clave[0], 'mes': clave[1], 'esperado': e, 'real': r, 'diferencia': dif})
    return avisos
