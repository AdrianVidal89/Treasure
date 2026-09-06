import datetime
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect

from .models import FondoFamiliar, SaldoRealFondo, PartidaGasto, FuenteIngreso, Propiedad
from .distribucion import _neto_fuente_base, calcular_flujos
from .views_evolucion import _delta_esperado_mes, _liquidez_patrimonio_por_mes


def _get_hogar(request):
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        return None, None
    return profile, profile.hogar


def _datos_financieros(hogar):
    hoy = datetime.date.today()

    # Último mes con datos de saldo real
    ultimo_mes = None
    ultimo_anio = hoy.year
    for anio in [hoy.year, hoy.year - 1]:
        for mes in range(12, 0, -1):
            if mes > hoy.month and anio == hoy.year:
                continue
            if SaldoRealFondo.objects.filter(fondo__hogar=hogar, año=anio, mes=mes).exists():
                ultimo_mes = mes
                ultimo_anio = anio
                break
        if ultimo_mes:
            break

    capital_liquidez = Decimal('0')
    capital_inversiones = Decimal('0')
    desglose_fondos = []

    if ultimo_mes:
        saldos = SaldoRealFondo.objects.filter(
            fondo__hogar=hogar, año=ultimo_anio, mes=ultimo_mes
        ).select_related('fondo')
        for s in saldos:
            if s.fondo.tipo_fondo in ('comun', 'ahorro'):
                capital_liquidez += s.saldo
            elif s.fondo.tipo_fondo == 'inversion':
                capital_inversiones += s.saldo
            desglose_fondos.append({
                'nombre': s.fondo.nombre,
                'tipo': s.fondo.tipo_fondo,
                'saldo': float(s.saldo),
                'color': s.fondo.color,
            })
        fondos_con_saldo = {s.fondo_id for s in saldos}
        for f in FondoFamiliar.objects.filter(hogar=hogar, tipo_fondo='inversion', activo=True):
            if f.id not in fondos_con_saldo and f.valor_cartera:
                capital_inversiones += Decimal(str(f.valor_cartera))

    # Ingresos netos mensuales RECURRENTES (base, sin pagas extras ni variables del mes)
    ingresos_netos = Decimal('0')
    for miembro in hogar.miembros.select_related('user').all():
        for fuente in FuenteIngreso.objects.filter(usuario=miembro.user, hogar=hogar, activo=True):
            base, _ = _neto_fuente_base(fuente)
            ingresos_netos += base

    # Gastos mensuales (prorrateados) desglosados por tipo de categoría:
    # fijo / variable / anual (provisión). Reutiliza PartidaGasto.importe_mensual.
    gastos_fijos = Decimal('0')
    gastos_variables = Decimal('0')
    gastos_anuales = Decimal('0')
    for p in PartidaGasto.objects.filter(hogar=hogar, activo=True).select_related('categoria'):
        tipo_cat = getattr(p.categoria, 'tipo', 'fijo')
        if tipo_cat in ('variable', 'discrecional'):
            # El gasto discrecional no es un compromiso fijo: para el simulador
            # cuenta como variable, no como coste ineludible.
            gastos_variables += p.importe_mensual
        elif tipo_cat == 'anual':
            gastos_anuales += p.importe_mensual
        elif tipo_cat in ('ingreso', 'traspaso'):
            continue  # no son gasto
        else:
            gastos_fijos += p.importe_mensual

    gastos_totales = gastos_fijos + gastos_variables + gastos_anuales
    libre = max(Decimal('0'), ingresos_netos - gastos_totales)

    # ── Ahorro mensual para la proyección a futuro ──────────────────────────
    # (a) Estimado / presupuestado: incremento de liquidez previsto por el motor
    #     de distribución para el mes actual (mismo criterio que la Evolución).
    ahorro_mensual_estimado = Decimal('0')
    try:
        flujos_mes = calcular_flujos(hogar, mes=hoy.month, anio=hoy.year)
        delta_liq_est, _ = _delta_esperado_mes(flujos_mes)
        ahorro_mensual_estimado = delta_liq_est
    except Exception:
        ahorro_mensual_estimado = libre  # fallback prudente

    # (b) Real: media mensual de la liquidez ahorrada desde el primer mes con
    #     datos hasta el último (lo ahorrado de enero a hoy entre los meses
    #     transcurridos), según los saldos reales registrados en Evolución.
    ahorro_mensual_real = None
    datos_liq = _liquidez_patrimonio_por_mes(hogar, hoy.year)
    meses_con_dato = [m for m in range(1, 13) if datos_liq[m][0] is not None]
    if len(meses_con_dato) >= 2:
        primer_mes = meses_con_dato[0]
        ultimo_mes_liq = meses_con_dato[-1]
        intervalos = ultimo_mes_liq - primer_mes
        if intervalos > 0:
            crecimiento = datos_liq[ultimo_mes_liq][0] - datos_liq[primer_mes][0]
            ahorro_mensual_real = crecimiento / intervalos

    # Pasar como dict Python: json_script lo serializa de forma segura (sin problemas de locale)
    sim_data = {
        'capital_liquidez': round(float(capital_liquidez), 2),
        'capital_inversiones': round(float(capital_inversiones), 2),
        'ingresos_netos_mensuales': round(float(ingresos_netos), 2),
        # gastos_fijos_mensuales = TOTAL de gastos (retrocompat: se usa para el colchón)
        'gastos_fijos_mensuales': round(float(gastos_totales), 2),
        'gastos_desg_fijos': round(float(gastos_fijos), 2),
        'gastos_desg_variables': round(float(gastos_variables), 2),
        'gastos_desg_anuales': round(float(gastos_anuales), 2),
        'gastos_totales_mensuales': round(float(gastos_totales), 2),
        'libre_mensual': round(float(libre), 2),
        'ahorro_mensual_estimado': round(float(ahorro_mensual_estimado), 2),
        'ahorro_mensual_real': (round(float(ahorro_mensual_real), 2)
                                if ahorro_mensual_real is not None else None),
    }

    return {
        'capital_liquidez': capital_liquidez,
        'capital_inversiones': capital_inversiones,
        'ingresos_netos_mensuales': ingresos_netos,
        'gastos_fijos_mensuales': gastos_totales,
        'libre_mensual': libre,
        'sim_data': sim_data,
        'desglose_fondos': desglose_fondos,
        'ultimo_mes': ultimo_mes,
        'ultimo_anio': ultimo_anio if ultimo_mes else None,
    }


# Costes recurrentes de una vivienda, como porcentaje ANUAL sobre su precio.
# Son sugerencias de partida (órdenes de magnitud habituales en España), no
# verdades: el simulador las propone ya calculadas y el usuario puede cambiar
# cualquiera de ellas por lo que sepa de la vivienda concreta.
#   · Comunidad     0,30 %/año → 300.000 € ≈ 75 €/mes
#   · Seguro hogar  0,12 %/año → 300.000 € ≈ 30 €/mes
#   · IBI y tasas   0,30 %/año (≈0,5 % de un catastral del 60 % del precio)
#   · Mantenimiento 0,50 %/año (derramas, averías, reformas menores)
RECURRENTES_VIVIENDA = [
    {'clave': 'comunidad', 'etiqueta': 'Comunidad', 'pct_anual': 0.30},
    {'clave': 'seguro', 'etiqueta': 'Seguro de hogar', 'pct_anual': 0.12},
    {'clave': 'ibi', 'etiqueta': 'IBI y tasas', 'pct_anual': 0.30},
    {'clave': 'mantenimiento', 'etiqueta': 'Mantenimiento y derramas', 'pct_anual': 0.50},
]

MESES_NOMBRES_SIM = [
    '', 'enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio',
    'julio', 'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre',
]


def _fuentes_de_datos(hogar, sim_data):
    """Con qué cifras simular.

    El presupuesto dice lo que DEBERÍA pasar cada mes; Evolución sabe lo que
    pasa de verdad (ingresos reales, y el gasto que sale de restarles lo
    ahorrado). Un simulador que solo mira el plan contesta a "¿me lo puedo
    permitir si cumplo el presupuesto?", que no es la pregunta.

    Devuelve las cuatro lecturas entre las que se puede elegir: el presupuesto,
    la media real, la mediana real (el mes típico, que no mueve un bonus ni una
    derrama) y el último mes cerrado. Las reales salen del análisis de ritmo de
    Evolución en su lectura de LIQUIDEZ: lo que va a inversión sale de la
    cuenta, así que cuenta como salida. Para "¿cuánto me queda al mes si compro
    esto?" esa es la lectura prudente.
    """
    from .views_evolucion import (_flujos_por_mes, _calcular_resumen,
                                  _datos_reales_por_mes, _analizar_ritmo)

    def fuente(clave, etiqueta, detalle, ingresos, gastos, ahorro,
               desglose=None, disponible=True):
        return {
            'clave': clave,
            'etiqueta': etiqueta,
            'detalle': detalle,
            'disponible': bool(disponible),
            'ingresos': round(float(ingresos or 0), 2),
            'gastos': round(float(gastos or 0), 2),
            'ahorro': round(float(ahorro or 0), 2),
            'desglose': desglose,
        }

    fuentes = {
        'presupuesto': fuente(
            'presupuesto', 'Presupuesto', 'Lo que dice tu plan de ingresos y gastos',
            sim_data['ingresos_netos_mensuales'],
            sim_data['gastos_totales_mensuales'],
            sim_data['ahorro_mensual_estimado'],
            desglose=[
                {'etiqueta': 'Gastos fijos', 'importe': sim_data['gastos_desg_fijos']},
                {'etiqueta': 'Gastos variables', 'importe': sim_data['gastos_desg_variables']},
                {'etiqueta': 'Gastos anuales (prorrateados)', 'importe': sim_data['gastos_desg_anuales']},
            ],
        ),
    }

    # Lo real: el año en curso, y si todavía no da para medias (enero, febrero),
    # el anterior, que sí tiene meses cerrados.
    hoy = datetime.date.today()
    liquidez, año_datos = None, None
    for año in (hoy.year, hoy.year - 1):
        flujos = _flujos_por_mes(hogar, año)
        resumen = _calcular_resumen(hogar, año, flujos)
        datos_por_mes = _datos_reales_por_mes(hogar, año, resumen)
        analisis = _analizar_ritmo(año, datos_por_mes, flujos, resumen)
        if analisis['liquidez']['n_meses']:
            liquidez, año_datos = analisis['liquidez'], año
            break

    if liquidez is None:
        sin_datos = 'Necesitas dos meses seguidos con saldo registrado en Evolución'
        for clave, etiqueta in (('media', 'Media real'), ('mediana', 'Mediana real'),
                                ('ultimo', 'Último mes cerrado')):
            fuentes[clave] = fuente(clave, etiqueta, sin_datos, 0, 0, 0, disponible=False)
        return fuentes

    n = liquidez['n_meses']
    plural = 'mes cerrado' if n == 1 else 'meses cerrados'
    fuentes['media'] = fuente(
        'media', 'Media real', f'Media de {n} {plural} de {año_datos}',
        liquidez['real']['ingreso']['media'],
        liquidez['real']['gasto']['media'],
        liquidez['real']['ahorro']['media'],
    )
    fuentes['mediana'] = fuente(
        'mediana', 'Mediana real', f'Tu mes típico, sobre {n} {plural} de {año_datos}',
        liquidez['real']['ingreso']['mediana'],
        liquidez['real']['gasto']['mediana'],
        liquidez['real']['ahorro']['mediana'],
    )
    ultimo = liquidez['meses'][-1]
    fuentes['ultimo'] = fuente(
        'ultimo', 'Último mes cerrado',
        f"Lo que pasó en {MESES_NOMBRES_SIM[ultimo['mes']]} de {año_datos}",
        ultimo['ingreso'], ultimo['gasto'], ultimo['ahorro'],
    )
    return fuentes


@login_required
def simulador_vivienda(request):
    profile, hogar = _get_hogar(request)
    if not hogar:
        return redirect('dashboard')

    datos = _datos_financieros(hogar)
    datos['sim_data'] = {
        **datos['sim_data'],
        'fuentes': _fuentes_de_datos(hogar, datos['sim_data']),
        'recurrentes': RECURRENTES_VIVIENDA,
    }
    propiedades_data = [
        p.calcular_neto_venta()
        for p in Propiedad.objects.filter(hogar=hogar, activo=True)
    ]
    return render(request, 'finanzas/simuladores/vivienda.html', {
        'hogar': hogar,
        'propiedades_data': propiedades_data,
        **datos,
    })


@login_required
def simulador_vehiculo(request):
    profile, hogar = _get_hogar(request)
    if not hogar:
        return redirect('dashboard')

    datos = _datos_financieros(hogar)
    return render(request, 'finanzas/simuladores/vehiculo.html', {
        'hogar': hogar,
        **datos,
    })
