"""Ritmo real del año y proyecciones a largo plazo.

Evolución ya registra dos cosas REALES mes a mes:

  · el saldo de cada fondo → lo AHORRADO en un mes es la diferencia con el mes
    anterior (nadie lo estima: son saldos, no previsiones);
  · el ingreso del mes, que viene de Distribución y queda congelado al cerrar
    el mes (ver `cierres.py`).

Con esas dos cifras el GASTO real del mes tampoco hay que estimarlo: es
exactamente `ingreso − ahorrado`. Este módulo toma esa serie y responde a las
dos preguntas que el presupuesto por sí solo no contesta:

  1. ¿A qué ritmo voy DE VERDAD? → media y mediana de ingreso, gasto y ahorro
     de los meses ya completos, frente a lo que el presupuesto decía para esos
     mismos meses. La media da el ritmo; la mediana, el mes típico (no la
     mueve un bonus, una derrama ni el IBI).
  2. ¿Dónde estaría dentro de 1, 2, 5 o 10 años si sigo a este ritmo?

Dos lecturas del ahorro, las mismas que ya usa el resto de la pantalla:

  · LIQUIDEZ  : lo que se queda disponible. Lo que sale hacia inversión deja de
    ser líquido, así que el presupuesto con el que se compara es gastos +
    inversión.
  · PATRIMONIO: todo lo que no se gasta, esté en cuenta o invertido. Ahí el
    presupuesto con el que se compara son solo los gastos.

Dos meses no entran nunca en las estadísticas:

  · el mes en curso, porque está a medias (ni ha entrado todo el ingreso ni ha
    terminado el gasto) y ensuciaría la media y la mediana;
  · enero, porque lo ahorrado necesita el saldo del mes anterior y diciembre
    pertenece a otro año.

Todo lo que sale de aquí son `float` (o `None`): es material de pantalla, se
serializa a JSON para el navegador.
"""

import statistics
from decimal import Decimal

from .cierres import mes_cerrado

# Horizontes de la proyección, en años.
HORIZONTES = (1, 2, 5, 10)

# Debajo de este desvío se considera que el presupuesto refleja la realidad:
# el mayor entre un 2 % de lo presupuestado y 25 € al mes.
TOLERANCIA_RELATIVA = Decimal('0.02')
TOLERANCIA_MINIMA = Decimal('25')


def _f(valor):
    return float(valor) if valor is not None else None


def _media(valores):
    return (sum(valores, Decimal('0')) / len(valores)) if valores else None


def _mediana(valores):
    return statistics.median(valores) if valores else None


def proyectar(base, ahorro_mensual, años, rentabilidad_anual=0.0):
    """Dónde estaría el capital dentro de `años` ahorrando `ahorro_mensual`
    todos los meses, con `rentabilidad_anual` sobre el capital acumulado
    (capitalización mensual). Con rentabilidad 0 es la suma pura del ahorro,
    que es la lectura honesta por defecto: proyecta tu ritmo, no la bolsa."""
    base = float(base or 0)
    ahorro_mensual = float(ahorro_mensual or 0)
    n = 12 * años
    if not rentabilidad_anual:
        return base + ahorro_mensual * n
    r = (1 + rentabilidad_anual) ** (1 / 12) - 1
    factor = (1 + r) ** n
    return base * factor + ahorro_mensual * (factor - 1) / r


def _filas_mensuales(valores_por_mes, flujos_por_mes, meses, gasto_incluye_inversion):
    """Serie mes a mes de lo real y lo presupuestado, solo para los meses en
    los que lo ahorrado se puede medir de verdad: hace falta saldo en el mes y
    en el inmediatamente anterior. Si falta un mes intermedio, la diferencia
    abarcaría dos meses y no sería un ritmo mensual: ese mes se salta."""
    filas = []
    for mes in meses:
        actual = valores_por_mes.get(mes)
        previo = valores_por_mes.get(mes - 1)
        if actual is None or previo is None:
            continue

        flujo = flujos_por_mes.get(mes)
        if not flujo:
            continue

        ingreso = flujo['ingreso_base_hogar']
        ahorro = actual - previo
        gasto = ingreso - ahorro

        gasto_prev = flujo['total_gastos_all']
        if gasto_incluye_inversion:
            gasto_prev += flujo['total_inversion']
        # Ingreso presupuestado = la nómina base, sin los ajustes ni los extras
        # del propio mes (que son justo lo que hace real a un mes).
        ingreso_prev = flujo['ingreso_base_puro_hogar']
        ahorro_prev = ingreso_prev - gasto_prev

        filas.append({
            'mes': mes,
            'ingreso': ingreso,
            'gasto': gasto,
            'ahorro': ahorro,
            'ingreso_prev': ingreso_prev,
            'gasto_prev': gasto_prev,
            'ahorro_prev': ahorro_prev,
        })
    return filas


def _aviso_desvio(concepto, real_mediana, presupuestado):
    """Traduce un desvío a una recomendación sobre el presupuesto. Se usa la
    MEDIANA como referencia: es el mes típico, y corregir el presupuesto por un
    mes excepcional es justo lo que no se quiere hacer."""
    if real_mediana is None or presupuestado is None:
        return None
    if presupuestado <= 0:
        # Sin presupuesto no hay nada que comparar; en gastos eso es en sí mismo
        # lo que hay que contar (nadie ha dado de alta sus partidas todavía).
        if concepto == 'gasto' and real_mediana > 0:
            return {'tono': 'info', 'texto': (
                'No hay gastos presupuestados con los que comparar. Da de alta tus '
                'partidas de gasto y aquí verás cuánto se desvían de la realidad.')}
        return None

    desvio = real_mediana - presupuestado
    umbral = max(presupuestado * TOLERANCIA_RELATIVA, TOLERANCIA_MINIMA)
    if abs(desvio) <= umbral:
        return None

    importe = abs(desvio)
    if concepto == 'gasto':
        if desvio > 0:
            return {'tono': 'aviso', 'texto': (
                f'Gastas {_euros(importe)} al mes más de lo presupuestado '
                f'({_euros(real_mediana)} reales frente a {_euros(presupuestado)}). '
                f'Sube el presupuesto ese importe o recorta ahí.')}
        return {'tono': 'ok', 'texto': (
            f'Gastas {_euros(importe)} al mes menos de lo presupuestado. '
            f'Puedes ajustar el presupuesto a {_euros(real_mediana)} y subir el objetivo de ahorro.')}

    if desvio > 0:
        return {'tono': 'ok', 'texto': (
            f'Ingresas {_euros(importe)} al mes por encima de lo presupuestado. '
            f'Actualiza tus fuentes de ingreso si ha venido para quedarse.')}
    return {'tono': 'aviso', 'texto': (
        f'Ingresas {_euros(importe)} al mes por debajo de lo presupuestado: '
        f'el presupuesto está contando con dinero que no llega.')}


def _euros(valor):
    """Mismo formato que el resto de la pantalla: el símbolo delante y el punto
    como separador de miles."""
    return '€' + f'{round(float(valor)):,.0f}'.replace(',', '.')


def _analisis_metrica(valores_por_mes, flujos_por_mes, meses, base_actual,
                      gasto_incluye_inversion):
    filas = _filas_mensuales(valores_por_mes, flujos_por_mes, meses,
                             gasto_incluye_inversion)

    def estadisticas(clave):
        valores = [f[clave] for f in filas]
        return {'media': _media(valores), 'mediana': _mediana(valores)}

    real = {c: estadisticas(c) for c in ('ingreso', 'gasto', 'ahorro')}
    presupuesto = {c: _media([f[c + '_prev'] for f in filas])
                   for c in ('ingreso', 'gasto', 'ahorro')}

    desvio = {}
    for c in ('ingreso', 'gasto', 'ahorro'):
        prev = presupuesto[c]
        desvio[c] = {
            k: (real[c][k] - prev) if (real[c][k] is not None and prev is not None) else None
            for k in ('media', 'mediana')
        }

    avisos = []
    if filas:
        desvios = [_aviso_desvio(c, real[c]['mediana'], presupuesto[c])
                   for c in ('gasto', 'ingreso')]
        avisos = [a for a in desvios if a]
        if not avisos:
            avisos.append({'tono': 'ok', 'texto':
                           'Tu presupuesto refleja bien lo que pasa de verdad: '
                           'ingresos y gastos van donde decía el plan.'})
        if len(filas) < 3:
            plural = 'mes completo' if len(filas) == 1 else 'meses completos'
            avisos.insert(0, {'tono': 'info', 'texto': (
                f'Solo {len(filas)} {plural} con lo ahorrado medido: la media y la '
                f'mediana todavía se mueven mucho.')})

    escenarios = []
    for clave, etiqueta, ahorro in (
        ('media', 'Al ritmo actual (media)', real['ahorro']['media']),
        ('mediana', 'En un mes típico (mediana)', real['ahorro']['mediana']),
        ('presupuesto', 'Si cumplieras el presupuesto', presupuesto['ahorro']),
    ):
        if ahorro is None:
            continue
        escenarios.append({
            'clave': clave,
            'etiqueta': etiqueta,
            'ahorro_mensual': _f(ahorro),
            'valores': {str(h): proyectar(base_actual, ahorro, h) for h in HORIZONTES},
        })

    def tasa(ahorro, ingreso):
        if not ahorro or not ingreso or ingreso <= 0:
            return None
        return round(float(ahorro / ingreso * 100), 1)

    return {
        'n_meses': len(filas),
        'meses': [{k: (v if k == 'mes' else _f(v)) for k, v in fila.items()} for fila in filas],
        'real': {c: {k: _f(v) for k, v in real[c].items()} for c in real},
        'presupuesto': {c: _f(v) for c, v in presupuesto.items()},
        'desvio': {c: {k: _f(v) for k, v in desvio[c].items()} for c in desvio},
        'tasa_ahorro': {
            'real': tasa(real['ahorro']['media'], real['ingreso']['media']),
            'presupuesto': tasa(presupuesto['ahorro'], presupuesto['ingreso']),
        },
        'base': _f(base_actual),
        'escenarios': escenarios,
        'avisos': avisos,
    }


def analizar(datos_por_mes, flujos_por_mes, año, base_liquidez, base_patrimonio,
             hoy=None):
    """Ritmo real y proyecciones del año, en sus dos lecturas.

    `datos_por_mes`: {mes: (liquidez|None, patrimonio|None)} con los saldos
    reales registrados (los mismos que alimentan la tabla y el gráfico).
    `flujos_por_mes`: el motor de distribución del año, ya con los meses
    cerrados aplicados.
    `base_liquidez` / `base_patrimonio`: el punto de partida de la proyección,
    que es la foto de hoy (último mes con datos)."""
    meses = [m for m in range(1, 13) if mes_cerrado(año, m, hoy)]

    liquidez_por_mes = {m: v[0] for m, v in datos_por_mes.items()}
    patrimonio_por_mes = {m: v[1] for m, v in datos_por_mes.items()}

    return {
        'año': año,
        'horizontes': list(HORIZONTES),
        'meses_completos': meses,
        'liquidez': _analisis_metrica(
            liquidez_por_mes, flujos_por_mes, meses, base_liquidez,
            gasto_incluye_inversion=True),
        'patrimonio': _analisis_metrica(
            patrimonio_por_mes, flujos_por_mes, meses, base_patrimonio,
            gasto_incluye_inversion=False),
    }
