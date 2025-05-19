from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse
from datetime import datetime
from .models import RegistroMensual
from .models import CuentaBancaria, CuentaCredito, PrestamoSimple, SaldoMensualCuenta
from django.contrib.auth.decorators import login_required
from .forms import CuentaBancariaForm, SaldoMensualCuentaForm
from django.contrib import messages



def index(request):
    return HttpResponse("Bienvenido a finanzas")



def resumen_mensual(request, anio, mes):
    usuario = request.user

    registro, created = RegistroMensual.objects.get_or_create(
        usuario=usuario,
        anio=anio,
        mes=mes
    )

    context = {
        'registro': registro,
        'anio': anio,
        'mes': mes,
        'nuevo': created
    }
    return render(request, 'finanzas/resumen_mensual.html', context)


@login_required
def gestionar_cuentas(request):
    usuario = request.user
    cuentas_bancarias = CuentaBancaria.objects.filter(usuario=usuario)
    cuentas_credito = CuentaCredito.objects.filter(usuario=usuario)
    prestamos = PrestamoSimple.objects.filter(usuario=usuario)

    anio = datetime.now().year
    mes = datetime.now().month

    try:
        registro = RegistroMensual.objects.get(usuario=usuario, anio=anio, mes=mes)
    except RegistroMensual.DoesNotExist:
        registro = None

    saldos = {}
    if registro:
        for cuenta in cuentas_bancarias:
            try:
                saldo = SaldoMensualCuenta.objects.get(cuenta=cuenta, registro=registro)
                saldos[cuenta.id] = saldo.saldo
            except SaldoMensualCuenta.DoesNotExist:
                saldos[cuenta.id] = None

    return render(request, 'finanzas/gestionar_cuentas.html', {
        'cuentas_bancarias': cuentas_bancarias,
        'cuentas_credito': cuentas_credito,
        'prestamos': prestamos,
        'saldos': saldos,
    })


@login_required
def nueva_cuenta_bancaria(request):
    if request.method == 'POST':
        form = CuentaBancariaForm(request.POST)
        if form.is_valid():
            cuenta = form.save(commit=False)
            cuenta.usuario = request.user
            cuenta.save()
            return redirect('gestionar_cuentas')
    else:
        form = CuentaBancariaForm()

    return render(request, 'finanzas/nueva_cuenta.html', {'form': form})


@login_required
def nuevo_saldo(request):
    usuario = request.user
    anio = datetime.now().year
    mes = datetime.now().month

    # Obtener o crear el registro mensual del mes actual
    registro, _ = RegistroMensual.objects.get_or_create(usuario=usuario, anio=anio, mes=mes)

    if request.method == 'POST':
        form = SaldoMensualCuentaForm(request.POST, usuario=usuario)
        if form.is_valid():
            saldo = form.save(commit=False)
            saldo.registro = registro
            saldo.save()
            return redirect('gestionar_cuentas')
    else:
        form = SaldoMensualCuentaForm(usuario=usuario)

    return render(request, 'finanzas/nuevo_saldo.html', {'form': form})


@login_required
def detalle_cuenta(request, cuenta_id):
    cuenta = get_object_or_404(CuentaBancaria, id=cuenta_id, usuario=request.user)
    anio = datetime.now().year
    mes = datetime.now().month

    # Obtener o crear registro mensual
    registro, _ = RegistroMensual.objects.get_or_create(usuario=request.user, anio=anio, mes=mes)

    # Obtener o crear saldo mensual para esta cuenta y mes
    try:
        saldo_obj = SaldoMensualCuenta.objects.get(cuenta=cuenta, registro=registro)
    except SaldoMensualCuenta.DoesNotExist:
        saldo_obj = None

    if request.method == 'POST':
        form = SaldoMensualCuentaForm(request.POST, instance=saldo_obj)
        if form.is_valid():
            saldo = form.save(commit=False)
            saldo.cuenta = cuenta
            saldo.registro = registro
            saldo.save()
            return redirect('gestionar_cuentas')
    else:
        form = SaldoMensualCuentaForm(instance=saldo_obj)

    return render(request, 'finanzas/detalle_cuenta.html', {
        'cuenta': cuenta,
        'form': form,
        'registro': registro
    })

@login_required
def eliminar_cuenta(request, cuenta_id):
    cuenta = get_object_or_404(CuentaBancaria, id=cuenta_id, usuario=request.user)
    cuenta.delete()
    messages.success(request, "La cuenta fue eliminada correctamente.")
    return redirect('gestionar_cuentas')
