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


def _calcular_resumen(hogar, año):
    """
    Construye el resumen superior para un año dado.
    Retorna dict con liquidez, patrimonio financiero y crecimientos.
    """
    hoy = datetime.date.today()
    fondos = list(FondoFamiliar.objects.filter(hogar=hogar, activo=True))

    # Acumular crecimiento esperado (proyección distribución) hasta mes actual
    meses_pasados = list(range(1, min(hoy.month + 1, 13))) if año == hoy.year else list(range(1, 13))

    ahorro_esperado_acum = Decimal('0')
    inversion_esperada_acum = Decimal('0')
    for mes in meses_pasados:
        d = calcular_flujos(hogar, mes=mes, anio=año)
        ahorro_esperado_acum += d['total_ahorro']
        inversion_esperada_acum += d['total_inversion']

    # Saldo real último mes con datos
    ultimo_mes_con_datos = None
    for mes in range(hoy.month if año == hoy.year else 12, 0, -1):
        if SaldoRealFondo.objects.filter(fondo__hogar=hogar, año=año, mes=mes).exists():
            ultimo_mes_con_datos = mes
            break

    liquidez_actual = Decimal('0')
    patrimonio_financiero_actual = Decimal('0')
    if ultimo_mes_con_datos:
        saldos = SaldoRealFondo.objects.filter(
            fondo__hogar=hogar, año=año, mes=ultimo_mes_con_datos
        ).select_related('fondo')
        for s in saldos:
            if s.fondo.tipo_fondo in ('comun', 'ahorro'):
                liquidez_actual += s.saldo
            patrimonio_financiero_actual += s.saldo

    # Crecimiento inter-anual: mismo mes año anterior
    liquidez_año_anterior = Decimal('0')
    patrimonio_año_anterior = Decimal('0')
    if ultimo_mes_con_datos:
        saldos_prev = SaldoRealFondo.objects.filter(
            fondo__hogar=hogar, año=año - 1, mes=ultimo_mes_con_datos
        ).select_related('fondo')
        for s in saldos_prev:
            if s.fondo.tipo_fondo in ('comun', 'ahorro'):
                liquidez_año_anterior += s.saldo
            patrimonio_año_anterior += s.saldo

    return {
        'liquidez_actual': liquidez_actual,
        'patrimonio_financiero_actual': patrimonio_financiero_actual,
        'crecimiento_liquidez_interanual': liquidez_actual - liquidez_año_anterior,
        'crecimiento_patrimonio_interanual': patrimonio_financiero_actual - patrimonio_año_anterior,
        'ahorro_esperado_acum': ahorro_esperado_acum,
        'patrimonio_esperado_acum': ahorro_esperado_acum + inversion_esperada_acum,
    }


def _construir_tabla(hogar, año):
    """
    Construye la tabla mensual: filas por mes, columnas por fondo + ingresos + ahorro_neto.
    """
    fondos = list(FondoFamiliar.objects.filter(hogar=hogar, activo=True).order_by('orden', 'nombre'))
    hoy = datetime.date.today()
    meses_mostrados = list(range(1, min(hoy.month + 1, 13))) if año == hoy.year else list(range(1, 13))

    # Precarga todos los saldos del año de una vez (evita N+1)
    saldos_qs = SaldoRealFondo.objects.filter(
        fondo__hogar=hogar, año=año
    ).select_related('fondo')
    saldos_map = {}  # (fondo_id, mes) → SaldoRealFondo
    for s in saldos_qs:
        saldos_map[(s.fondo_id, s.mes)] = s

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

        # Ahorro neto = liquidez este mes - liquidez mes anterior
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

    filas.reverse()  # más reciente primero
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
    """POST: registra o actualiza el saldo real de un fondo en un mes."""
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
    """POST: registra o actualiza el ingreso real del hogar en un mes."""
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
    """Crear un fondo nuevo desde la vista evolución (redirige a distribución o vuelve aquí)."""
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