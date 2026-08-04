"""Motor de cálculo de depósitos.

Un depósito se define por un tipo de interés anual, una frecuencia de
capitalización y una serie de flujos de caja fechados (aportaciones y
retiradas de capital; la primera aportación es la apertura). El valor a una
fecha es el capital compuesto: entre dos flujos, el saldo crece según el
interés; en cada flujo, el saldo aumenta (aportación) o disminuye (retirada).

Convención de interés: tipo nominal anual `r` capitalizado `m` veces al año.
El factor entre dos fechas separadas por `dias` días es:

    (1 + r/m) ** (m * dias / 365)

lo que soporta periodos fraccionados (p. ej. 17 días con capitalización
mensual) de forma continua y coherente para diaria/mensual/trimestral/anual.
Todo en Decimal para no arrastrar errores de coma flotante en dinero.
"""
from decimal import Decimal

# Capitalizaciones por año.
PERIODOS_ANO = {
    'diaria': 365,
    'mensual': 12,
    'trimestral': 4,
    'semestral': 2,
    'anual': 1,
}

FRECUENCIA_CHOICES = [
    ('diaria', 'Diaria'),
    ('mensual', 'Mensual'),
    ('trimestral', 'Trimestral'),
    ('semestral', 'Semestral'),
    ('anual', 'Anual'),
]


def factor_interes(r_anual, periodos_ano, dias):
    """Factor de capitalización para `dias` días con tipo nominal anual
    `r_anual` (en tanto por uno) capitalizado `periodos_ano` veces al año."""
    if r_anual == 0 or dias <= 0:
        return Decimal('1')
    m = Decimal(periodos_ano)
    exponente = m * Decimal(dias) / Decimal('365')
    return (Decimal('1') + r_anual / m) ** exponente


def valor_a_fecha(flujos, r_anual, periodos_ano, hasta, anclaje=None):
    """Devuelve (valor, aportado_neto) del depósito a la fecha `hasta`.

    - `flujos`: iterable de (fecha, importe_neto) con importe positivo para
      aportaciones y negativo para retiradas. No hace falta que venga ordenado.
    - `anclaje`: (fecha, saldo) opcional. Es el saldo REAL conocido en esa fecha
      (p. ej. el que dice el banco). Se toma como punto de partida —los flujos
      anteriores ya están contenidos en él— y a partir de ahí se siguen
      aplicando los flujos posteriores y el interés. NO es un valor fijo: si
      después retiras dinero, el saldo baja.
    - `valor`: saldo con intereses a `hasta`.
    - `aportado_neto`: suma de los flujos de capital hasta `hasta` (sin interés).
    """
    flujos = sorted(((f, Decimal(str(i))) for (f, i) in flujos), key=lambda x: x[0])
    flujos_hasta = [(f, i) for (f, i) in flujos if f <= hasta]
    aportado = sum((i for _, i in flujos_hasta), Decimal('0'))

    # El anclaje solo tiene sentido si el depósito ya existía en esa fecha; si
    # es anterior o igual a la apertura, no hay saldo previo que anclar.
    if anclaje is not None and flujos and anclaje[0] <= flujos[0][0]:
        anclaje = None

    if anclaje is not None:
        ancla_fecha, ancla_saldo = anclaje[0], Decimal(str(anclaje[1]))
        # El saldo real es la foto ANTES de los movimientos de ese día: se
        # aplican los flujos de esa fecha en adelante (así, si hoy indicas
        # 6.066 y retiras 6.066, el saldo queda en 0).
        posteriores = [(f, i) for (f, i) in flujos_hasta if f >= ancla_fecha]
        balance = ancla_saldo
        fecha_ant = ancla_fecha
        for fecha, importe in posteriores:
            balance = balance * factor_interes(r_anual, periodos_ano, (fecha - fecha_ant).days)
            balance += importe
            fecha_ant = fecha
        balance = balance * factor_interes(r_anual, periodos_ano, (hasta - fecha_ant).days)
        return balance, aportado

    if not flujos_hasta:
        return Decimal('0'), Decimal('0')

    balance = Decimal('0')
    fecha_ant = flujos_hasta[0][0]
    for fecha, importe in flujos_hasta:
        balance = balance * factor_interes(r_anual, periodos_ano, (fecha - fecha_ant).days)
        balance += importe
        fecha_ant = fecha
    balance = balance * factor_interes(r_anual, periodos_ano, (hasta - fecha_ant).days)

    return balance, aportado
