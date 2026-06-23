import datetime
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect

from .models import FondoFamiliar, SaldoRealFondo, PartidaGasto, FuenteIngreso
from .distribucion import _neto_fuente_base


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

    # Gastos fijos mensuales
    gastos_fijos = sum(
        p.importe_mensual
        for p in PartidaGasto.objects.filter(hogar=hogar, activo=True)
    )

    libre = max(Decimal('0'), ingresos_netos - gastos_fijos)

    # Pasar como dict Python: json_script lo serializa de forma segura (sin problemas de locale)
    sim_data = {
        'capital_liquidez': round(float(capital_liquidez), 2),
        'capital_inversiones': round(float(capital_inversiones), 2),
        'ingresos_netos_mensuales': round(float(ingresos_netos), 2),
        'gastos_fijos_mensuales': round(float(gastos_fijos), 2),
        'libre_mensual': round(float(libre), 2),
    }

    return {
        'capital_liquidez': capital_liquidez,
        'capital_inversiones': capital_inversiones,
        'ingresos_netos_mensuales': ingresos_netos,
        'gastos_fijos_mensuales': gastos_fijos,
        'libre_mensual': libre,
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
    return render(request, 'finanzas/simuladores/vivienda.html', {
        'hogar': hogar,
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
