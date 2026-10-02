"""Lo que deja un piso alquilado, y lo que hay que pagar a Hacienda por ello.

Dos preguntas que la ficha de costes no contesta:

1. MES A MES, ¿cuánto entra de alquiler, cuánto cuesta el piso y qué queda?
   Todo sale de los extractos: el ingreso es lo que imputaste a la propiedad
   desde Movimientos (el chip «activo» de la fila, o «todos los de este
   comercio»), y el coste, los gastos imputados a ella.

2. ¿CUÁNTO IRPF supone? Un alquiler no es una ganancia patrimonial (eso es al
   vender): es un rendimiento del capital inmobiliario, va a la base GENERAL y
   tributa al tipo marginal de quien es el dueño, sumado a su nómina. Por eso
   el mismo alquiler sale más caro en la declaración de quien más gana, y por
   eso hay que saber de quién es el piso.

   rendimiento neto = ingresos
                      − gastos deducibles (IBI, comunidad, seguro, reparaciones…)
                      − intereses de la hipoteca (no la cuota: el capital no)
                      − amortización (3 % al año de la parte de construcción)
   reducido         = rendimiento neto × (1 − reducción) si es positivo y el
                      piso es vivienda habitual del inquilino
   impuesto         = cuota(nómina + alquiler) − cuota(nómina)

   Es una ESTIMACIÓN para planificar —y apartar el dinero—, no la declaración:
   no conoce las deducciones autonómicas, ni el arrastre de gastos de años
   anteriores, ni que intereses y reparaciones no pueden pasar de los ingresos.
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal

from . import costes_activo
from .fiscal import ES_GASTOS_DEDUCIBLES, ES_MINIMO_PERSONAL, _aplicar_tramos, _obtener_tramos, calcular_ss

CERO = Decimal('0')
AMORTIZACION = Decimal('0.03')
MESES = ['', 'ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sep', 'oct', 'nov', 'dic']
MESES_LARGOS = ['', 'Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio', 'Julio', 'Agosto',
                'Septiembre', 'Octubre', 'Noviembre', 'Diciembre']
# La cuota del préstamo no se deduce (es devolver capital): sus intereses
# entran aparte, dichos a mano. Se reconoce por el nombre de la categoría.
PALABRAS_HIPOTECA = ('hipoteca', 'prestamo', 'préstamo')


def _es_hipoteca(m):
    nombre = (m.categoria.nombre if m.categoria else '').lower()
    return any(p in nombre for p in PALABRAS_HIPOTECA)


def _cuota(base, tramos):
    """Cuota íntegra estatal+autonómica simplificada: tramos sobre la base menos
    los mismos tramos sobre el mínimo personal (como `calcular_irpf`)."""
    if base <= 0:
        return CERO
    return max(_aplicar_tramos(base, tramos) - _aplicar_tramos(ES_MINIMO_PERSONAL, tramos), CERO)


def _titulares(propiedad):
    """`[(usuario, parte)]`: de quién es el alquiler a efectos del IRPF."""
    if propiedad.propietario_id:
        return [(propiedad.propietario, Decimal('1'))]
    from core.models import UserProfile

    miembros = [p.user for p in UserProfile.objects.filter(hogar=propiedad.hogar).select_related('user')]
    if not miembros:
        return []
    parte = Decimal('1') / len(miembros)
    return [(u, parte) for u in miembros]


def _base_trabajo(usuario, anio):
    """Lo que ya tiene en la base general antes del alquiler: sus nóminas.

    Sale de sus fuentes de ingreso declaradas, menos las que son de un activo
    (el propio alquiler, si se declaró también como fuente). Una fuente
    declarada en neto se toma como si fuera bruto, y se avisa: el impuesto
    sale algo por debajo."""
    from .models import FuenteIngreso

    fuentes = FuenteIngreso.objects.filter(
        usuario=usuario, activo=True, propiedad__isnull=True, vehiculo__isnull=True,
    )
    bruto = CERO
    en_neto = []
    for f in fuentes:
        bruto += f.importe_anual_estimado
        if not f.es_bruto:
            en_neto.append(f.nombre)
    ss = calcular_ss(bruto, 'ES', anio) if bruto else CERO
    return max(bruto - ss - ES_GASTOS_DEDUCIBLES, CERO), bruto, en_neto


def analizar_alquiler(propiedad, anio, hoy=None):
    hoy = hoy or date.today()
    excluir = {propiedad.partida_irpf_id} - {None}
    todos = list(costes_activo._movimientos(propiedad))
    del_anio = [m for m in todos if m.fecha.year == anio]
    ingresos = [m for m in del_anio if m.cuenta_como_ingreso]
    gastos = [m for m in del_anio if m.cuenta_como_gasto]

    if anio < hoy.year:
        mes_hoy = 13
    elif anio > hoy.year:
        mes_hoy = 0
    else:
        mes_hoy = hoy.month

    # --- Mes a mes ---
    ing_mes, cos_mes = defaultdict(Decimal), defaultdict(Decimal)
    mov_mes = defaultdict(list)
    for m in ingresos:
        ing_mes[m.fecha.month] += m.importe
        mov_mes[m.fecha.month].append(('ingreso', m))
    for m in gastos:
        cos_mes[m.fecha.month] += -m.importe_neto
        mov_mes[m.fecha.month].append(('gasto', m))
    meses = []
    for n in range(1, 13):
        # El mes en curso sin nada todavía no es un mes de 0 €: no se pinta.
        futuro = n >= mes_hoy
        meses.append({
            'mes': n, 'etiqueta': MESES[n], 'nombre': f'{MESES_LARGOS[n]} {anio}',
            'ingreso': ing_mes[n], 'coste': cos_mes[n], 'neto': ing_mes[n] - cos_mes[n],
            'futuro': futuro and not (ing_mes[n] or cos_mes[n]),
            'movimientos': [
                {'tipo': t, 'fecha': m.fecha.strftime('%d/%m'), 'concepto': m.concepto[:60],
                 'importe': float(m.importe if t == 'ingreso' else -m.importe_neto)}
                for t, m in sorted(mov_mes[n], key=lambda x: (x[1].fecha, x[0]))
            ],
        })

    ingreso_real = sum(ing_mes.values(), CERO)
    coste_real = sum(cos_mes.values(), CERO)
    cerrados = [x for x in meses if x['mes'] < mes_hoy]
    meses_con_ingreso = [x for x in cerrados if x['ingreso'] > 0]
    media_ingreso = (
        sum((x['ingreso'] for x in meses_con_ingreso), CERO) / len(meses_con_ingreso)
        if meses_con_ingreso else CERO
    )
    media_neto = (
        sum((x['neto'] for x in cerrados), CERO) / len(cerrados) if cerrados else CERO
    )

    # --- El año entero, para la declaración ---
    # Un año pasado es lo que fue. El año en curso se completa: los meses que
    # aún no han cobrado, a la media de lo cobrado; los gastos que faltan, a
    # la provisión mensual de las partidas del piso.
    pendientes = [x for x in meses if x['mes'] >= mes_hoy and not x['ingreso']] if anio >= hoy.year else []
    ingresos_anio = ingreso_real + media_ingreso * len(pendientes)
    deducibles = [m for m in gastos if not _es_hipoteca(m) and m.partida_conciliada_id not in excluir]
    gastos_reales = sum((-m.importe_neto for m in deducibles), CERO)
    partidas = [
        p for p in costes_activo._partidas(propiedad)
        if p.id not in excluir and not any(k in (p.categoria.nombre if p.categoria else p.nombre).lower()
                                             for k in PALABRAS_HIPOTECA)
    ]
    restantes = max(12 - mes_hoy, 0) if anio == hoy.year else (12 if anio > hoy.year else 0)
    gastos_previstos = sum((p.importe_mensual for p in partidas), CERO) * restantes
    # Lo que de verdad saldrá del bolsillo en el año, hipoteca entera incluida:
    # para el neto tras impuestos, no para la base.
    coste_anio = coste_real - sum(
        (-m.importe_neto for m in gastos if m.partida_conciliada_id in excluir), CERO,
    ) + sum(
        (p.importe_mensual for p in costes_activo._partidas(propiedad) if p.id not in excluir), CERO,
    ) * restantes
    gastos_anio = gastos_reales + gastos_previstos
    intereses = propiedad.intereses_hipoteca_anuales or CERO
    amortizacion = (
        (propiedad.coste_base * propiedad.pct_construccion / 100 * AMORTIZACION).quantize(Decimal('0.01'))
        if propiedad.pct_construccion else CERO
    )
    rendimiento = ingresos_anio - gastos_anio - intereses - amortizacion
    pct_reduccion = Decimal(propiedad.reduccion_alquiler_pct or 0)
    reduccion = (rendimiento * pct_reduccion / 100).quantize(Decimal('0.01')) if rendimiento > 0 else CERO
    reducido = rendimiento - reduccion

    # --- Quién lo paga ---
    tramos = _obtener_tramos('ES', anio)
    titulares = []
    for usuario, parte in _titulares(propiedad):
        base, bruto, en_neto = _base_trabajo(usuario, anio)
        suyo = (reducido * parte).quantize(Decimal('0.01'))
        impuesto = (_cuota(base + suyo, tramos) - _cuota(base, tramos)).quantize(Decimal('0.01'))
        titulares.append({
            'usuario': usuario,
            'nombre': usuario.first_name or usuario.username,
            'parte': parte,
            'pct': round(float(parte * 100)),
            'bruto_trabajo': bruto,
            'base_trabajo': base,
            'rendimiento': suyo,
            'impuesto': impuesto,
            'tipo': round(float(impuesto / suyo * 100), 1) if suyo > 0 else 0.0,
            'en_neto': en_neto,
            'sin_ingresos': not bruto,
        })
    impuesto_total = sum((t['impuesto'] for t in titulares), CERO)

    return {
        'propiedad': propiedad,
        'anio': anio,
        'meses': meses,
        'grafico': {
            'meses': [
                {'etiqueta': x['etiqueta'], 'nombre': x['nombre'],
                 'ingreso': float(x['ingreso']), 'coste': float(x['coste']), 'neto': float(x['neto']),
                 'futuro': x['futuro'], 'movimientos': x['movimientos']}
                for x in meses
            ],
        },
        'ingreso_real': ingreso_real,
        'coste_real': coste_real,
        'neto_real': ingreso_real - coste_real,
        'media_ingreso': media_ingreso,
        'media_neto': media_neto,
        'meses_cerrados': len(cerrados),
        'hay_ingresos': bool(ingresos),
        'num_ingresos': len(ingresos),
        # La declaración
        'proyectado': bool(pendientes) or gastos_previstos > 0,
        'meses_pendientes': len(pendientes),
        'ingresos_anio': ingresos_anio,
        'gastos_anio': gastos_anio,
        'gastos_reales': gastos_reales,
        'gastos_previstos': gastos_previstos,
        'excluidos_hipoteca': sum((-m.importe_neto for m in gastos if _es_hipoteca(m)), CERO),
        'intereses': intereses,
        'amortizacion': amortizacion,
        'falta_construccion': not propiedad.pct_construccion,
        'rendimiento': rendimiento,
        'pct_reduccion': pct_reduccion,
        'reduccion': reduccion,
        'reducido': reducido,
        'titulares': titulares,
        'impuesto_total': impuesto_total,
        'impuesto_mensual': (impuesto_total / 12).quantize(Decimal('0.01')),
        'coste_anio': coste_anio,
        'neto_tras_impuesto': ingresos_anio - coste_anio - impuesto_total,
        'neto_tras_impuesto_mes': ((ingresos_anio - coste_anio - impuesto_total) / 12).quantize(Decimal('1')),
        'sin_tramos': not tramos,
        'partida_irpf': propiedad.partida_irpf if propiedad.partida_irpf_id else None,
    }
