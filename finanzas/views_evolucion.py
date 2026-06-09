import datetime
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.shortcuts import render, redirect, get_object_or_404

from .models import FondoFamiliar, SaldoRealFondo, IngresoRealMes
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


def _saldos_liquidez_patrimonio(saldos_qs):
    """Dado un queryset de SaldoRealFondo, devuelve (liquidez, patrimonio)."""
    liquidez = Decimal('0')
    patrimonio = Decimal('0')
    for s in saldos_qs:
        if s.fondo.tipo_fondo in ('comun', 'ahorro'):
            liquidez += s.saldo
        patrimonio += s.saldo
    return liquidez, patrimonio


def _calcular_resumen(hogar, año):
    hoy = datetime.date.today()
    
    # Iteramos siempre los 12 meses para el esperado anual
    meses_totales = range(1, 13)

    # --- Esperado según presupuesto (motor de distribución) ---
    ahorro_esperado_acum = Decimal('0')
    inversion_esperada_acum = Decimal('0')
    for mes in meses_totales:
        d = calcular_flujos(hogar, mes=mes, anio=año)
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
        liquidez_actual, patrimonio_actual = _saldos_liquidez_patrimonio(saldos_actual)

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


def _construir_tabla(hogar, año):
    fondos = list(FondoFamiliar.objects.filter(hogar=hogar, activo=True).order_by('orden', 'nombre'))
    hoy = datetime.date.today()
    meses_mostrados = list(range(1, min(hoy.month + 1, 13))) if año == hoy.year else list(range(1, 13))

    saldos_qs = SaldoRealFondo.objects.filter(
        fondo__hogar=hogar, año=año
    ).select_related('fondo')
    saldos_map = {(s.fondo_id, s.mes): s for s in saldos_qs}

    ingresos_map = {
        ir.mes: ir
        for ir in IngresoRealMes.objects.filter(hogar=hogar, año=año)
    }

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

        ingreso_real = ingresos_map.get(mes)

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
            'ingreso_real': ingreso_real,
            'liquidez': liquidez_mes if liquidez_mes > 0 else None,
            'patrimonio': patrimonio_mes if patrimonio_mes > 0 else None,
            'ahorro_neto': ahorro_neto,
        })

    filas.reverse()
    return fondos, filas


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

    resumen = _calcular_resumen(hogar, año)
    fondos, filas = _construir_tabla(hogar, año)
    años_disponibles = [hoy.year - 1, hoy.year, hoy.year + 1]

    return render(request, 'finanzas/evolucion/vista.html', {
        'hogar': hogar,
        'profile': profile,
        'resumen': resumen,
        'fondos': fondos,
        'filas': filas,
        'año_actual': año,
        'años': años_disponibles,
    })


@login_required
def registrar_saldo_fondo(request):
    profile, hogar = _get_hogar(request)
    if not hogar:
        return redirect('dashboard')

    if request.method == 'POST':
        try:
            fondo_id = int(request.POST.get('fondo_id', 0))
            mes = int(request.POST.get('mes', 0))
            año = int(request.POST.get('año', 0))
            saldo_raw = request.POST.get('saldo', '').strip()
        except (ValueError, TypeError):
            messages.error(request, "Datos inválidos.")
            return redirect('finanzas:vista_evolucion')

        fondo = get_object_or_404(FondoFamiliar, id=fondo_id, hogar=hogar)

        if not saldo_raw:
            SaldoRealFondo.objects.filter(fondo=fondo, año=año, mes=mes).delete()
            messages.success(request, f"Saldo de '{fondo.nombre}' eliminado.")
        else:
            from decimal import InvalidOperation
            try:
                saldo = Decimal(saldo_raw.replace(',', '.'))
            except InvalidOperation:
                messages.error(request, "Importe inválido.")
                return redirect(f"/finanzas/evolucion/?año={año}")

            nota = request.POST.get('nota', '').strip()
            _, created = SaldoRealFondo.objects.update_or_create(
                fondo=fondo, año=año, mes=mes,
                defaults={'saldo': saldo, 'nota': nota},
            )
            accion = 'registrado' if created else 'actualizado'
            messages.success(request, f"'{fondo.nombre}' {mes}/{año}: €{saldo} {accion}.")

    año_redirect = request.POST.get('año', datetime.date.today().year)
    return redirect(f"/finanzas/evolucion/?año={año_redirect}")


@login_required
def registrar_ingreso_mes(request):
    profile, hogar = _get_hogar(request)
    if not hogar:
        return redirect('dashboard')

    if request.method == 'POST':
        try:
            mes = int(request.POST.get('mes', 0))
            año = int(request.POST.get('año', 0))
            importe_raw = request.POST.get('importe', '').strip()
        except (ValueError, TypeError):
            messages.error(request, "Datos inválidos.")
            return redirect('finanzas:vista_evolucion')

        if not importe_raw:
            IngresoRealMes.objects.filter(hogar=hogar, año=año, mes=mes).delete()
        else:
            from decimal import InvalidOperation
            try:
                importe = Decimal(importe_raw.replace(',', '.'))
            except InvalidOperation:
                messages.error(request, "Importe inválido.")
                return redirect(f"/finanzas/evolucion/?año={año}")

            nota = request.POST.get('nota', '').strip()
            IngresoRealMes.objects.update_or_create(
                hogar=hogar, año=año, mes=mes,
                defaults={'importe': importe, 'nota': nota},
            )

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