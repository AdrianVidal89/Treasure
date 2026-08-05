import datetime
import json
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.views.decorators.http import require_POST

from .models import (
    FondoFamiliar, SaldoRealFondo, PartidaGasto, FuenteIngreso, Propiedad,
    SimulacionVivienda, TIPOS_GASTO,
)
from .distribucion import _neto_fuente_base, calcular_flujos
from .views_evolucion import _delta_esperado_mes, _liquidez_patrimonio_por_mes

# Categoría del presupuesto que recoge lo que ya se paga hoy por vivienda.
CATEGORIA_VIVIENDA = 'Hipoteca / Alquiler'


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
    # Gasto mensual prorrateado por bloque del presupuesto. Se mantienen los
    # cuatro separados (no se funden discrecional y variable) para que el
    # simulador pueda enseñar el presupuesto igual que la pantalla de Gastos.
    por_bloque = {t: Decimal('0') for t in TIPOS_GASTO}
    # Lo que ya se paga por vivienda hoy: si al comprar dejas de pagarlo, deja
    # de ser un gasto y esa parte de la cuota nueva no es coste adicional.
    vivienda_actual = Decimal('0')
    for p in PartidaGasto.objects.filter(hogar=hogar, activo=True).select_related('categoria'):
        tipo_cat = getattr(p.categoria, 'tipo', 'fijo')
        if tipo_cat not in TIPOS_GASTO:
            continue  # ingresos y traspasos no son gasto
        por_bloque[tipo_cat] += p.importe_mensual
        if getattr(p.categoria, 'nombre', '') == CATEGORIA_VIVIENDA:
            vivienda_actual += p.importe_mensual

    gastos_fijos = por_bloque['fijo']
    gastos_variables = por_bloque['variable']
    gastos_anuales = por_bloque['anual']
    gastos_discrecionales = por_bloque['discrecional']

    gastos_totales = sum(por_bloque.values(), Decimal('0'))
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
        'gastos_desg_discrecionales': round(float(gastos_discrecionales), 2),
        'gastos_totales_mensuales': round(float(gastos_totales), 2),
        # Lo que hoy ya se destina a vivienda (alquiler o hipoteca en curso).
        'vivienda_actual_mensual': round(float(vivienda_actual), 2),
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
        'vivienda_actual_mensual': vivienda_actual,
        'sim_data': sim_data,
        'desglose_fondos': desglose_fondos,
        'ultimo_mes': ultimo_mes,
        'ultimo_anio': ultimo_anio if ultimo_mes else None,
    }


@login_required
def simulador_vivienda(request):
    profile, hogar = _get_hogar(request)
    if not hogar:
        return redirect('dashboard')

    datos = _datos_financieros(hogar)
    propiedades_data = [
        p.calcular_neto_venta()
        for p in Propiedad.objects.filter(hogar=hogar, activo=True)
    ]
    simulacion, _ = SimulacionVivienda.objects.get_or_create(hogar=hogar)
    return render(request, 'finanzas/simuladores/vivienda.html', {
        'hogar': hogar,
        'propiedades_data': propiedades_data,
        'simulacion': simulacion.como_dict(),
        **datos,
    })


# Campos que el simulador puede guardar, con su conversión desde JSON. Se
# valida aquí y no en el cliente para que un valor manipulado no llegue a la BD.
_CAMPOS_SIMULACION = {
    'modo': ('choice', [c[0] for c in SimulacionVivienda.MODO_CHOICES]),
    'base_esfuerzo': ('choice', [c[0] for c in SimulacionVivienda.BASE_CHOICES]),
    'precio': ('decimal', (0, 5_000_000)),
    'entrada_pct': ('decimal', (0, 100)),
    'tipo_interes': ('decimal', (0, 20)),
    'gastos_compra_pct': ('decimal', (0, 30)),
    'max_esfuerzo_pct': ('decimal', (1, 100)),
    'ibi_pct_anual': ('decimal', (0, 5)),
    'comunidad_mes': ('decimal', (0, 2000)),
    'seguro_hogar_anio': ('decimal', (0, 10000)),
    'mantenimiento_pct_anual': ('decimal', (0, 5)),
    'plazo_anios': ('int', (1, 40)),
    'meses_colchon': ('int', (0, 36)),
    'sustituye_vivienda_actual': ('bool', None),
    'propiedades_vendidas': ('ids', None),
}


@login_required
@require_POST
def guardar_simulacion_vivienda(request):
    """Guarda el escenario del simulador. Se llama en cada cambio de un control
    (con retardo) para que los valores sigan ahí al volver a entrar."""
    profile, hogar = _get_hogar(request)
    if not hogar:
        return JsonResponse({'ok': False, 'error': 'sin_hogar'}, status=403)

    try:
        datos = json.loads(request.body.decode('utf-8') or '{}')
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({'ok': False, 'error': 'json_invalido'}, status=400)
    if not isinstance(datos, dict):
        return JsonResponse({'ok': False, 'error': 'json_invalido'}, status=400)

    simulacion, _ = SimulacionVivienda.objects.get_or_create(hogar=hogar)
    cambios = []
    for campo, (tipo, limites) in _CAMPOS_SIMULACION.items():
        if campo not in datos:
            continue
        valor = _limpiar_valor(datos[campo], tipo, limites)
        if valor is None:
            continue
        setattr(simulacion, campo, valor)
        cambios.append(campo)

    if cambios:
        simulacion.save(update_fields=cambios + ['actualizado_en'])
    return JsonResponse({'ok': True, 'guardados': cambios})


def _limpiar_valor(valor, tipo, limites):
    """Convierte y acota un valor recibido del simulador; None si no es válido."""
    if tipo == 'choice':
        return valor if valor in limites else None
    if tipo == 'bool':
        return bool(valor)
    if tipo == 'ids':
        if not isinstance(valor, list):
            return None
        return [int(v) for v in valor if str(v).lstrip('-').isdigit()]
    try:
        numero = Decimal(str(valor))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not numero.is_finite():
        return None
    minimo, maximo = limites
    numero = max(Decimal(str(minimo)), min(Decimal(str(maximo)), numero))
    return int(numero) if tipo == 'int' else numero


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
