"""Datos y cálculos de la compra de vivienda.

Aquí vive lo que NO depende de la interacción: la tabla de impuestos por
comunidad autónoma y el desglose de los gastos de compra. El cálculo de cuotas
y escenarios vive en `static/js/hipoteca.js`, porque tiene que recalcularse a
cada movimiento de un slider; este módulo le pasa la tabla y comparte con él
los mismos porcentajes, de modo que no hay dos verdades.

AVISO sobre los impuestos: los tipos cambian con cada presupuesto autonómico y
las bonificaciones tienen letra pequeña (límite de precio, empadronamiento,
plazo para escriturar...). Lo de aquí es una ESTIMACIÓN de partida; el
simulador deja ajustar el porcentaje final a mano, que es lo que hay que hacer
antes de firmar nada.
"""

from decimal import Decimal

# Tipo general del ITP (vivienda de segunda mano) y tipo reducido habitual para
# menores de 35 años comprando vivienda habitual, cuando la comunidad lo tiene.
# `ajd` es el impuesto de Actos Jurídicos Documentados, que es el que se paga
# en obra nueva (junto al IVA del 10 %).
CCAA = [
    # código, nombre, itp general %, itp joven %, ajd %
    ('AN', 'Andalucía',            7.0,  3.5,  1.2),
    ('AR', 'Aragón',               8.0,  4.0,  1.5),
    ('AS', 'Asturias',             8.0,  3.0,  1.2),
    ('IB', 'Baleares',             8.0,  5.0,  1.5),
    ('CN', 'Canarias',             6.5,  5.0,  0.75),
    ('CB', 'Cantabria',            9.0,  5.0,  1.5),
    ('CM', 'Castilla-La Mancha',   9.0,  6.0,  1.5),
    ('CL', 'Castilla y León',      8.0,  5.0,  1.5),
    ('CT', 'Cataluña',            10.0,  5.0,  1.5),
    ('EX', 'Extremadura',          8.0,  6.0,  1.5),
    ('GA', 'Galicia',              9.0,  3.0,  1.5),
    ('MD', 'Madrid',               6.0,  6.0,  0.75),
    ('MC', 'Murcia',               8.0,  3.0,  1.5),
    ('NC', 'Navarra',              6.0,  5.0,  0.5),
    ('PV', 'País Vasco',           4.0,  2.5,  0.5),
    ('RI', 'La Rioja',             7.0,  5.0,  1.0),
    ('VC', 'Comunidad Valenciana', 10.0, 8.0,  1.5),
]

IVA_OBRA_NUEVA = 10.0

# Gastos fijos de la operación. Son estimaciones de mercado: un porcentaje con
# suelo y techo, que es como se comportan los aranceles.
NOTARIA_PCT, NOTARIA_MIN, NOTARIA_MAX = 0.35, 600, 1400
REGISTRO_PCT, REGISTRO_MIN, REGISTRO_MAX = 0.20, 400, 800
GESTORIA = 350
TASACION = 350

# Coste recurrente de tener la vivienda, aparte de la cuota. Son los valores de
# partida del simulador; todos se pueden ajustar.
MANTENIMIENTO_PCT_ANUAL = 1.0    # % del valor del inmueble al año
SEGURO_HOGAR_ANUAL = 300
IBI_PCT_ANUAL = 0.5              # % del valor, orientativo (varía por municipio)
COMUNIDAD_MENSUAL = 60


def tabla_ccaa():
    """La tabla en el formato que consume el JS del simulador."""
    return [
        {'codigo': c, 'nombre': n, 'itp': itp, 'itp_joven': joven, 'ajd': ajd}
        for c, n, itp, joven, ajd in CCAA
    ]


def _entre(valor, minimo, maximo):
    return max(minimo, min(maximo, valor))


def gastos_compra(precio, ccaa='MD', obra_nueva=False, joven=False):
    """Desglose de lo que cuesta comprar, aparte del precio.

    Devuelve importes en euros y el total. En obra nueva se paga IVA + AJD; en
    segunda mano, ITP (con el tipo reducido si aplica la bonificación joven)."""
    precio = float(precio or 0)
    fila = next((f for f in CCAA if f[0] == ccaa), None)
    if fila is None:
        fila = next(f for f in CCAA if f[0] == 'MD')
    _, _, itp_general, itp_joven, ajd_pct = fila

    if obra_nueva:
        impuesto_pct = IVA_OBRA_NUEVA + ajd_pct
        impuesto_nombre = f'IVA {IVA_OBRA_NUEVA:g}% + AJD {ajd_pct:g}%'
    else:
        impuesto_pct = itp_joven if (joven and itp_joven is not None) else itp_general
        impuesto_nombre = f'ITP {impuesto_pct:g}%'

    impuestos = precio * impuesto_pct / 100
    notaria = _entre(precio * NOTARIA_PCT / 100, NOTARIA_MIN, NOTARIA_MAX) if precio else 0
    registro = _entre(precio * REGISTRO_PCT / 100, REGISTRO_MIN, REGISTRO_MAX) if precio else 0
    gestoria = GESTORIA if precio else 0
    tasacion = TASACION if precio else 0

    total = impuestos + notaria + registro + gestoria + tasacion
    return {
        'impuestos': round(impuestos, 2),
        'impuesto_pct': impuesto_pct,
        'impuesto_nombre': impuesto_nombre,
        'notaria': round(notaria, 2),
        'registro': round(registro, 2),
        'gestoria': gestoria,
        'tasacion': tasacion,
        'total': round(total, 2),
        'total_pct': round(total / precio * 100, 2) if precio else 0,
    }


def cuota_mensual(capital, tipo_anual, años):
    """Cuota de un préstamo francés. Es la misma fórmula que usa el JS; está
    aquí para poder comprobarla con tests."""
    capital = float(capital or 0)
    años = int(años or 0)
    if capital <= 0 or años <= 0:
        return 0.0
    n = años * 12
    if not tipo_anual:
        return capital / n
    r = float(tipo_anual) / 100 / 12
    return capital * r * (1 + r) ** n / ((1 + r) ** n - 1)


def capital_maximo(cuota_max, tipo_anual, años):
    """Cuánto capital soporta una cuota dada: la inversa de `cuota_mensual`."""
    cuota_max = float(cuota_max or 0)
    años = int(años or 0)
    if cuota_max <= 0 or años <= 0:
        return 0.0
    n = años * 12
    if not tipo_anual:
        return cuota_max * n
    r = float(tipo_anual) / 100 / 12
    return cuota_max * ((1 + r) ** n - 1) / (r * (1 + r) ** n)


def coste_recurrente_mensual(valor_vivienda, mantenimiento_pct=None, ibi_pct=None,
                             seguro_anual=None, comunidad_mensual=None):
    """Lo que cuesta TENER la vivienda cada mes, sin contar la hipoteca.

    Es la diferencia entre poder pagar la cuota y poder permitirse la casa."""
    valor = float(valor_vivienda or 0)
    mantenimiento_pct = MANTENIMIENTO_PCT_ANUAL if mantenimiento_pct is None else mantenimiento_pct
    ibi_pct = IBI_PCT_ANUAL if ibi_pct is None else ibi_pct
    seguro_anual = SEGURO_HOGAR_ANUAL if seguro_anual is None else seguro_anual
    comunidad_mensual = COMUNIDAD_MENSUAL if comunidad_mensual is None else comunidad_mensual

    mantenimiento = valor * float(mantenimiento_pct) / 100 / 12
    ibi = valor * float(ibi_pct) / 100 / 12
    seguro = float(seguro_anual) / 12
    return {
        'mantenimiento': round(mantenimiento, 2),
        'ibi': round(ibi, 2),
        'seguro': round(seguro, 2),
        'comunidad': round(float(comunidad_mensual), 2),
        'total': round(mantenimiento + ibi + seguro + float(comunidad_mensual), 2),
    }


def defaults_para_js():
    """Valores de partida que el simulador enseña antes de que toques nada."""
    return {
        'iva_obra_nueva': IVA_OBRA_NUEVA,
        'notaria_pct': NOTARIA_PCT, 'notaria_min': NOTARIA_MIN, 'notaria_max': NOTARIA_MAX,
        'registro_pct': REGISTRO_PCT, 'registro_min': REGISTRO_MIN, 'registro_max': REGISTRO_MAX,
        'gestoria': GESTORIA, 'tasacion': TASACION,
        'mantenimiento_pct': MANTENIMIENTO_PCT_ANUAL,
        'ibi_pct': IBI_PCT_ANUAL,
        'seguro_anual': SEGURO_HOGAR_ANUAL,
        'comunidad_mensual': COMUNIDAD_MENSUAL,
    }
