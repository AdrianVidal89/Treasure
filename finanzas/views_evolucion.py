import datetime
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404

from .models import FondoFamiliar, SaldoRealFondo, IngresoRealMes, Propiedad, HistorialPropiedad
from .distribucion import calcular_flujos

MESES_NOMBRES = [
    '', 'Ene', 'Feb', 'Mar', 'Abr', 'May', 'Jun',
    'Jul', 'Ago', 'Sep', 'Oct', 'Nov', 'Dic',
]


def _get_hogar(request):
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        return None, None
    return profile, profile.hogar


def _saldos_liquidez_patrimonio(saldos_qs, hogar=None):
    """
    Devuelve (liquidez, patrimonio).
    - Liquidez : fondos comun + ahorro (saldos manuales).
    - Patrimonio: liquidez + fondos inversion.
        · Saldo manual registrado tiene prioridad.
        · Si no hay saldo manual → valor_cartera (precio mercado real).
    """
    liquidez = Decimal('0')
    patrimonio = Decimal('0')
    inversion_con_saldo = set()

    for s in saldos_qs:
        if s.fondo.tipo_fondo in ('comun', 'ahorro'):
            liquidez += s.saldo
            patrimonio += s.saldo
        elif s.fondo.tipo_fondo == 'inversion':
            patrimonio += s.saldo
            inversion_con_saldo.add(s.fondo_id)

    if hogar:
        for f in FondoFamiliar.objects.filter(
            hogar=hogar, tipo_fondo='inversion', activo=True
        ).exclude(id__in=inversion_con_saldo):
            if f.valor_cartera:
                patrimonio += Decimal(str(f.valor_cartera))

    return liquidez, patrimonio

def _flujos_por_mes(hogar, año):
    """Calcula (una sola vez) el motor de distribución para los 12 meses.
    Se reutiliza tanto para el resumen como para el ingreso derivado de cada
    fila, evitando recalcular calcular_flujos por duplicado."""
    return {mes: calcular_flujos(hogar, mes=mes, anio=año) for mes in range(1, 13)}


def _calcular_resumen(hogar, año, flujos_por_mes):
    hoy = datetime.date.today()

    # --- Esperado según presupuesto (motor de distribución) ---
    ahorro_esperado_acum = Decimal('0')
    inversion_esperada_acum = Decimal('0')
    for mes in range(1, 13):
        d = flujos_por_mes[mes]
        ahorro_esperado_acum += d['total_ahorro']
        inversion_esperada_acum += d['total_inversion']

    # --- Saldo real último mes con datos ---
    ultimo_mes_con_datos = None
    for mes in range(hoy.month if año == hoy.year else 12, 0, -1):
        if SaldoRealFondo.objects.filter(fondo__hogar=hogar, año=año, mes=mes).exists():
            ultimo_mes_con_datos = mes
            break

    liquidez_actual = Decimal('0')
    patrimonio_actual = Decimal('0')
    if ultimo_mes_con_datos:
        saldos_actual = SaldoRealFondo.objects.filter(
            fondo__hogar=hogar, año=año, mes=ultimo_mes_con_datos
        ).select_related('fondo')
        liquidez_actual, patrimonio_actual = _saldos_liquidez_patrimonio(saldos_actual, hogar=hogar)

    # --- Crecimiento YTD: actual − Enero del MISMO año ---
    saldos_enero = SaldoRealFondo.objects.filter(
        fondo__hogar=hogar, año=año, mes=1
    ).select_related('fondo')
    tiene_enero = saldos_enero.exists()
    liquidez_enero, patrimonio_enero = _saldos_liquidez_patrimonio(saldos_enero) if tiene_enero else (Decimal('0'), Decimal('0'))

    crecimiento_liquidez_ytd = (liquidez_actual - liquidez_enero) if tiene_enero else None
    crecimiento_patrimonio_ytd = (patrimonio_actual - patrimonio_enero) if tiene_enero else None

    return {
        'liquidez_actual': liquidez_actual,
        'patrimonio_financiero_actual': patrimonio_actual,
        'liquidez_enero': liquidez_enero,
        'patrimonio_enero': patrimonio_enero,
        'tiene_enero': tiene_enero,
        'crecimiento_liquidez_ytd': crecimiento_liquidez_ytd,
        'crecimiento_patrimonio_ytd': crecimiento_patrimonio_ytd,
        'ahorro_esperado_acum': ahorro_esperado_acum,
        'patrimonio_esperado_acum': ahorro_esperado_acum + inversion_esperada_acum,
        'ultimo_mes_con_datos': ultimo_mes_con_datos,
        'ultimo_mes_nombre': MESES_NOMBRES[ultimo_mes_con_datos] if ultimo_mes_con_datos else '',
    }


def _construir_tabla(hogar, año, flujos_por_mes):
    fondos = list(FondoFamiliar.objects.filter(hogar=hogar, activo=True).order_by('orden', 'nombre'))
    hoy = datetime.date.today()
    meses_mostrados = list(range(1, min(hoy.month + 1, 13))) if año == hoy.year else list(range(1, 13))

    saldos_qs = SaldoRealFondo.objects.filter(
        fondo__hogar=hogar, año=año
    ).select_related('fondo')
    saldos_map = {(s.fondo_id, s.mes): s for s in saldos_qs}

    filas = []
    prev_liquidez = None
    for mes in meses_mostrados:
        celdas_fondos = []
        liquidez_mes = Decimal('0')
        patrimonio_mes = Decimal('0')
        for f in fondos:
            sr = saldos_map.get((f.id, mes))
            celdas_fondos.append({
                'fondo': f,
                'saldo_real': sr,
                'saldo_valor': sr.saldo if sr else None,
            })
            if sr:
                if f.tipo_fondo in ('comun', 'ahorro'):
                    liquidez_mes += sr.saldo
                patrimonio_mes += sr.saldo

        # El ingreso del mes se DERIVA de Distribución (suma de ingresos reales
        # del mes, con los ajustes aplicados allí), no se introduce a mano.
        ingreso_mes = flujos_por_mes[mes]['ingreso_base_hogar']

        if prev_liquidez is not None and liquidez_mes > 0:
            ahorro_neto = liquidez_mes - prev_liquidez
        else:
            ahorro_neto = None

        if liquidez_mes > 0:
            prev_liquidez = liquidez_mes

        filas.append({
            'mes': mes,
            'mes_nombre': MESES_NOMBRES[mes],
            'celdas': celdas_fondos,
            'ingreso_mes': ingreso_mes if ingreso_mes and ingreso_mes > 0 else None,
            'liquidez': liquidez_mes if liquidez_mes > 0 else None,
            'patrimonio': patrimonio_mes if patrimonio_mes > 0 else None,
            'ahorro_neto': ahorro_neto,
        })

    filas.reverse()
    return fondos, filas


def _estado_json(resumen, filas):
    """Serializa el estado recalculado para refrescar la vista sin recargar."""
    def f(valor):
        return float(valor) if valor is not None else None

    return {
        'resumen': {
            'liquidez_actual': f(resumen['liquidez_actual']),
            'patrimonio_financiero_actual': f(resumen['patrimonio_financiero_actual']),
            'crecimiento_liquidez_ytd': f(resumen['crecimiento_liquidez_ytd']),
            'crecimiento_patrimonio_ytd': f(resumen['crecimiento_patrimonio_ytd']),
            'tiene_enero': resumen['tiene_enero'],
        },
        'meses': {
            fila['mes']: {
                'liquidez': f(fila['liquidez']),
                'patrimonio': f(fila['patrimonio']),
                'ahorro_neto': f(fila['ahorro_neto']),
                'ingreso': f(fila['ingreso_mes']),
            }
            for fila in filas
        },
    }


def _calcular_estado_json(hogar, año):
    flujos_por_mes = _flujos_por_mes(hogar, año)
    resumen = _calcular_resumen(hogar, año, flujos_por_mes)
    _, filas = _construir_tabla(hogar, año, flujos_por_mes)
    return _estado_json(resumen, filas)


def _construir_tabla_propiedades(hogar, año):
    propiedades = list(Propiedad.objects.filter(hogar=hogar, activo=True).order_by('nombre'))
    hoy = datetime.date.today()
    meses_mostrados = list(range(1, min(hoy.month + 1, 13))) if año == hoy.year else list(range(1, 13))

    historial_qs = HistorialPropiedad.objects.filter(
        propiedad__hogar=hogar, año=año
    ).select_related('propiedad')
    hist_map = {(h.propiedad_id, h.mes): h for h in historial_qs}

    filas = []
    for mes in meses_mostrados:
        celdas = []
        total_neto = Decimal('0')
        tiene_datos = False
        for p in propiedades:
            h = hist_map.get((p.id, mes))
            neto = (h.valor_mercado - h.deuda_hipotecaria) if h else None
            if neto is not None:
                total_neto += neto
                tiene_datos = True
            celdas.append({
                'propiedad': p,
                'historial': h,
                'valor': h.valor_mercado if h else None,
                'deuda': h.deuda_hipotecaria if h else None,
                'neto': neto,
            })
        filas.append({
            'mes': mes,
            'mes_nombre': MESES_NOMBRES[mes],
            'celdas': celdas,
            'total_neto': total_neto if tiene_datos else None,
        })

    filas.reverse()
    return propiedades, filas


@login_required
def vista_evolucion(request):
    profile, hogar = _get_hogar(request)
    if not hogar:
        messages.error(request, "Necesitas pertenecer a un hogar.")
        return redirect('dashboard')

    hoy = datetime.date.today()
    try:
        año = int(request.GET.get('año', hoy.year))
    except (ValueError, TypeError):
        año = hoy.year

    flujos_por_mes = _flujos_por_mes(hogar, año)
    resumen = _calcular_resumen(hogar, año, flujos_por_mes)
    fondos, filas = _construir_tabla(hogar, año, flujos_por_mes)
    propiedades, filas_propiedades = _construir_tabla_propiedades(hogar, año)
    años_disponibles = [hoy.year - 1, hoy.year, hoy.year + 1]

    return render(request, 'finanzas/evolucion/vista.html', {
        'hogar': hogar,
        'profile': profile,
        'resumen': resumen,
        'fondos': fondos,
        'filas': filas,
        'propiedades': propiedades,
        'filas_propiedades': filas_propiedades,
        'año_actual': año,
        'años': años_disponibles,
    })


@login_required
def registrar_saldo_fondo(request):
    profile, hogar = _get_hogar(request)
    if not hogar:
        return redirect('dashboard')

    es_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    if request.method == 'POST':
        try:
            fondo_id = int(request.POST.get('fondo_id', 0))
            mes = int(request.POST.get('mes', 0))
            año = int(request.POST.get('año', 0))
            saldo_raw = request.POST.get('saldo', '').strip()
        except (ValueError, TypeError):
            if es_ajax:
                return JsonResponse({'ok': False, 'error': 'Datos inválidos.'}, status=400)
            messages.error(request, "Datos inválidos.")
            return redirect('finanzas:vista_evolucion')

        fondo = get_object_or_404(FondoFamiliar, id=fondo_id, hogar=hogar)

        if not saldo_raw:
            SaldoRealFondo.objects.filter(fondo=fondo, año=año, mes=mes).delete()
            if es_ajax:
                return JsonResponse({'ok': True, 'valor': None, 'estado': _calcular_estado_json(hogar, año)})
            messages.success(request, f"Saldo de '{fondo.nombre}' eliminado.")
        else:
            from decimal import InvalidOperation
            try:
                saldo = Decimal(saldo_raw.replace(',', '.'))
            except InvalidOperation:
                if es_ajax:
                    return JsonResponse({'ok': False, 'error': 'Importe inválido.'}, status=400)
                messages.error(request, "Importe inválido.")
                return redirect(f"/finanzas/evolucion/?año={año}")

            nota = request.POST.get('nota', '').strip()
            _, created = SaldoRealFondo.objects.update_or_create(
                fondo=fondo, año=año, mes=mes,
                defaults={'saldo': saldo, 'nota': nota},
            )
            if es_ajax:
                return JsonResponse({'ok': True, 'valor': float(saldo), 'estado': _calcular_estado_json(hogar, año)})
            accion = 'registrado' if created else 'actualizado'
            messages.success(request, f"'{fondo.nombre}' {mes}/{año}: €{saldo} {accion}.")

    año_redirect = request.POST.get('año', datetime.date.today().year)
    return redirect(f"/finanzas/evolucion/?año={año_redirect}")


@login_required
def registrar_ingreso_mes(request):
    profile, hogar = _get_hogar(request)
    if not hogar:
        return redirect('dashboard')

    es_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    if request.method == 'POST':
        try:
            mes = int(request.POST.get('mes', 0))
            año = int(request.POST.get('año', 0))
            importe_raw = request.POST.get('importe', '').strip()
        except (ValueError, TypeError):
            if es_ajax:
                return JsonResponse({'ok': False, 'error': 'Datos inválidos.'}, status=400)
            messages.error(request, "Datos inválidos.")
            return redirect('finanzas:vista_evolucion')

        if not importe_raw:
            IngresoRealMes.objects.filter(hogar=hogar, año=año, mes=mes).delete()
            if es_ajax:
                return JsonResponse({'ok': True, 'valor': None})
        else:
            from decimal import InvalidOperation
            try:
                importe = Decimal(importe_raw.replace(',', '.'))
            except InvalidOperation:
                if es_ajax:
                    return JsonResponse({'ok': False, 'error': 'Importe inválido.'}, status=400)
                messages.error(request, "Importe inválido.")
                return redirect(f"/finanzas/evolucion/?año={año}")

            nota = request.POST.get('nota', '').strip()
            IngresoRealMes.objects.update_or_create(
                hogar=hogar, año=año, mes=mes,
                defaults={'importe': importe, 'nota': nota},
            )
            if es_ajax:
                return JsonResponse({'ok': True, 'valor': float(importe)})

    año_redirect = request.POST.get('año', datetime.date.today().year)
    return redirect(f"/finanzas/evolucion/?año={año_redirect}")


@login_required
def crear_fondo_evolucion(request):
    profile, hogar = _get_hogar(request)
    if not hogar:
        return redirect('dashboard')

    if request.method == 'POST':
        nombre = request.POST.get('nombre', '').strip()
        tipo_fondo = request.POST.get('tipo_fondo', 'ahorro')
        color = request.POST.get('color', '#00ff88')
        cuenta = request.POST.get('cuenta_asociada', '').strip()

        if not nombre:
            messages.error(request, "El nombre es obligatorio.")
        else:
            max_orden = FondoFamiliar.objects.filter(hogar=hogar).count()
            FondoFamiliar.objects.get_or_create(
                hogar=hogar, nombre=nombre,
                defaults={
                    'tipo_fondo': tipo_fondo,
                    'color': color,
                    'cuenta_asociada': cuenta,
                    'orden': max_orden,
                }
            )
            messages.success(request, f"Fondo '{nombre}' creado.")

    return redirect('finanzas:vista_evolucion')