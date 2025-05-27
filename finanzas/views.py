from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.timezone import now
from django.http import JsonResponse
from django.utils.timezone import now
from finanzas.models import RegistroMensual


from .forms import (
    CuentaBancariaForm,
    SaldoMensualCuentaForm,
    SaldoMensualTarjetaForm,
    TarjetaCreditoForm,
)

from .models import (
    CuentaBancaria,
    CuentaCredito,
    PrestamoSimple,
    RegistroMensual,
    SaldoMensualCuenta,
    SaldoMensualTarjeta,
    TarjetaCredito,
)


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
    tarjetas = TarjetaCredito.objects.filter(usuario=usuario)

    hoy = now().date()
    anio = hoy.year
    mes = hoy.month

    try:
        registro = RegistroMensual.objects.get(usuario=usuario, anio=anio, mes=mes)
    except RegistroMensual.DoesNotExist:
        registro = None

    saldos = {}
    # Añadir saldos de tarjetas
    saldos_tarjetas = {}
    if registro:
        for cuenta in cuentas_bancarias:
            try:
                saldo = SaldoMensualCuenta.objects.get(cuenta=cuenta, registro=registro)
                saldos[cuenta.id] = saldo.saldo
            except SaldoMensualCuenta.DoesNotExist:
                saldos[cuenta.id] = None
        for tarjeta in tarjetas:
            try:
                saldo = SaldoMensualTarjeta.objects.get(tarjeta=tarjeta, registro=registro)
                saldos_tarjetas[tarjeta.id] = saldo.saldo
            except SaldoMensualTarjeta.DoesNotExist:
                saldos_tarjetas[tarjeta.id] = None


    return render(request, 'finanzas/gestionar_cuentas.html', {
        'cuentas_bancarias': cuentas_bancarias,
        'cuentas_credito': cuentas_credito,
        'prestamos': prestamos,
        'tarjetas': tarjetas,
        'saldos': saldos,
        'saldos_tarjetas': saldos_tarjetas,
    })

@login_required
def nueva_cuenta_bancaria(request):
    if request.method == 'POST':
        form = CuentaBancariaForm(request.POST)
        if form.is_valid():
            cuenta = form.save(commit=False)
            cuenta.usuario = request.user
            cuenta.save()
            return redirect('finanzas:gestionar_cuentas')
    else:
        form = CuentaBancariaForm()

    return render(request, 'finanzas/nueva_cuenta.html', {'form': form})

@login_required
def nuevo_saldo(request):
    usuario = request.user
    hoy = now().date()
    anio = hoy.year
    mes = hoy.month

    # Obtener o crear el registro mensual del mes actual
    registro, _ = RegistroMensual.objects.get_or_create(usuario=usuario, anio=anio, mes=mes)

    if request.method == 'POST':
        form = SaldoMensualCuentaForm(request.POST, usuario=usuario)
        if form.is_valid():
            saldo = form.save(commit=False)
            saldo.registro = registro
            saldo.save()
            return redirect('finanzasgestionar_cuentas')
    else:
        form = SaldoMensualCuentaForm(usuario=usuario)

    return render(request, 'finanzas/nuevo_saldo.html', {'form': form})

@login_required
def detalle_cuenta(request, cuenta_id):
    cuenta = get_object_or_404(CuentaBancaria, id=cuenta_id, usuario=request.user)
    hoy = now().date()
    registro = None  # inicializamos para evitar UnboundLocalError

    if request.method == 'POST':
        form = SaldoMensualCuentaForm(request.POST)
        if form.is_valid():
            mes = int(form.cleaned_data['mes'])
            anio = int(form.cleaned_data['anio'])

            # Validar que no sea una fecha futura
            if anio > hoy.year or (anio == hoy.year and mes > hoy.month):
                form.add_error(None, "No puedes registrar saldos en el futuro.")
            else:
                registro, _ = RegistroMensual.objects.get_or_create(
                    usuario=request.user, anio=anio, mes=mes
                )
                SaldoMensualCuenta.objects.update_or_create(
                    cuenta=cuenta,
                    registro=registro,
                    defaults={'saldo': form.cleaned_data['saldo']}
                )
                messages.success(request, "Saldo guardado correctamente.")
                return redirect('finanzas:gestionar_cuentas')
        else:
            # Si el form no es válido, intentamos obtener el registro para la vista
            try:
                mes = int(request.POST.get('mes'))
                anio = int(request.POST.get('anio'))
                registro = RegistroMensual.objects.filter(
                    usuario=request.user, anio=anio, mes=mes
                ).first()
            except:
                registro = None
    else:
        # GET: vista inicial con mes/año actuales
        anio = hoy.year
        mes = hoy.month
        registro, _ = RegistroMensual.objects.get_or_create(
            usuario=request.user, anio=anio, mes=mes
        )
        saldo_obj = SaldoMensualCuenta.objects.filter(cuenta=cuenta, registro=registro).first()

        form = SaldoMensualCuentaForm(initial={
            'saldo': saldo_obj.saldo if saldo_obj else '',
            'mes': mes,
            'anio': anio
        })

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
    return redirect('finanzas:gestionar_cuentas')

@login_required
def obtener_saldo_ajax(request):
    cuenta_id = request.GET.get('cuenta_id')
    anio = request.GET.get('anio')
    mes = request.GET.get('mes')

    try:
        registro = RegistroMensual.objects.get(usuario=request.user, anio=anio, mes=mes)
        saldo = SaldoMensualCuenta.objects.get(cuenta_id=cuenta_id, registro=registro)
        return JsonResponse({'success': True, 'saldo': float(saldo.saldo)})
    except (RegistroMensual.DoesNotExist, SaldoMensualCuenta.DoesNotExist):
        return JsonResponse({'success': False, 'saldo': None})


@login_required
def gestionar_tarjetas(request):
    tarjetas = TarjetaCredito.objects.filter(usuario=request.user)
    hoy = now().date()
    registro = RegistroMensual.objects.filter(usuario=request.user, anio=hoy.year, mes=hoy.month).first()
    saldos = {
        t.id: SaldoMensualTarjeta.objects.filter(tarjeta=t, registro=registro).first()
        for t in tarjetas
    } if registro else {}
    return render(request, 'finanzas/gestionar_cuentas.html', {
        'tarjetas': tarjetas,
        'saldos': saldos,
        'registro': registro
    })

@login_required
def nueva_tarjeta(request):
    if request.method == 'POST':
        form = TarjetaCreditoForm(request.POST)
        if form.is_valid():
            tarjeta = form.save(commit=False)
            tarjeta.usuario = request.user
            tarjeta.save()
        return redirect('finanzas:gestionar_cuentas')
    else:
        form = TarjetaCreditoForm()
    return render(request, 'finanzas/nueva_tarjeta.html', {'form': form})

@login_required
def detalle_tarjeta(request, tarjeta_id):
    tarjeta = get_object_or_404(TarjetaCredito, id=tarjeta_id, usuario=request.user)
    hoy = now().date()
    registro = None

    if request.method == 'POST':
        form = SaldoMensualTarjetaForm(request.POST)
        if form.is_valid():
            mes = int(form.cleaned_data['mes'])
            anio = int(form.cleaned_data['anio'])

            if anio > hoy.year or (anio == hoy.year and mes > hoy.month):
                form.add_error(None, "No puedes registrar saldos en el futuro.")
            else:
                registro, _ = RegistroMensual.objects.get_or_create(usuario=request.user, anio=anio, mes=mes)
                SaldoMensualTarjeta.objects.update_or_create(
                    tarjeta=tarjeta,
                    registro=registro,
                    defaults={'saldo': form.cleaned_data['saldo']}
                )
                return redirect('finanzas:gestionar_cuentas')
    else:
        anio = hoy.year
        mes = hoy.month
        registro, _ = RegistroMensual.objects.get_or_create(usuario=request.user, anio=anio, mes=mes)
        saldo = SaldoMensualTarjeta.objects.filter(tarjeta=tarjeta, registro=registro).first()

        form = SaldoMensualTarjetaForm(initial={
            'saldo': saldo.saldo if saldo else '',
            'mes': mes,
            'anio': anio
        })

    return render(request, 'finanzas/detalle_tarjeta.html', {
        'tarjeta': tarjeta,
        'form': form,
        'registro': registro
    })

@login_required
def eliminar_tarjeta(request, tarjeta_id):
    tarjeta = get_object_or_404(TarjetaCredito, id=tarjeta_id, usuario=request.user)
    tarjeta.delete()
    return redirect('finanzas:gestionar_cuentas')


@login_required
def obtener_saldo_tarjeta_ajax(request):
    tarjeta_id = request.GET.get('tarjeta_id')
    anio = request.GET.get('anio')
    mes = request.GET.get('mes')

    try:
        registro = RegistroMensual.objects.get(usuario=request.user, anio=anio, mes=mes)
        saldo = SaldoMensualTarjeta.objects.filter(tarjeta_id=tarjeta_id, registro=registro).order_by('-id').first()
        if saldo:
            return JsonResponse({'success': True, 'saldo': float(saldo.saldo)})
        else:
            return JsonResponse({'success': False, 'saldo': None})
    except RegistroMensual.DoesNotExist:
        return JsonResponse({'success': False, 'saldo': None})

@login_required
def patrimonio_total_actual(request):
    usuario = request.user
    fecha = now()

    # Obtiene el último registro del usuario autenticado
    registro = RegistroMensual.objects.filter(
        usuario=usuario,
        anio=fecha.year,
        mes=fecha.month
    ).first()

    if not registro:
        return JsonResponse({'valor': 0})

    return JsonResponse({'valor': float(registro.patrimonio_total)})


### Sub-modulo Inversiones ###

from django.contrib.auth.mixins import LoginRequiredMixin
from django.urls import reverse_lazy
from django.views.generic import ListView, DetailView, CreateView, UpdateView
from django.shortcuts import get_object_or_404
from .models import Inversion, MovimientoInversion, ValorActualInversion
from .forms import InversionForm, MovimientoInversionForm
from django.views.generic import DetailView
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, render
from .models import ResumenInversionesMensual


# 📌 Vista 1: Lista de inversiones del usuario
class InversionListView(LoginRequiredMixin, ListView):
    model = Inversion
    template_name = 'inversiones/inversion_list.html'
    context_object_name = 'inversiones'

    def get_queryset(self):
        return Inversion.objects.filter(usuario=self.request.user)
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        inversiones = context['inversiones']

        total_actual = sum(inv.valor_total_actual for inv in inversiones)
        total_aportado = sum(inv.valor_aportado for inv in inversiones)
        total_activos = sum(inv.total_activos for inv in inversiones)

        rentabilidad = 0
        if total_aportado > 0:
            rentabilidad = ((total_actual - total_aportado) / total_aportado) * 100

        context.update({
            'total_valor_actual': total_actual,
            'total_aportado': total_aportado,
            'total_activos': total_activos,
            'rentabilidad_total': rentabilidad,
        })
        return context


# 📌 Vista 2: Detalle de una inversión + movimientos asociados
class InversionDetailView(LoginRequiredMixin, DetailView):
    model = Inversion
    template_name = 'inversiones/inversion_detail.html'
    context_object_name = 'inversion'

    def get_queryset(self):
        return Inversion.objects.filter(usuario=self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['movimientos'] = MovimientoInversion.objects.filter(inversion=self.object).order_by('-fecha')
        return context

# 📌 Vista 3: Crear nueva inversión
class InversionCreateView(LoginRequiredMixin, CreateView):
    model = Inversion
    form_class = InversionForm
    template_name = 'inversiones/inversion_form.html'
    success_url = reverse_lazy('finanzas:listar')

    def form_valid(self, form):
        form.instance.usuario = self.request.user
        response = super().form_valid(form)

        if not form.instance.actualizable:
            valor_manual = form.cleaned_data.get('valor_unitario_manual')
            if valor_manual is not None:
                ValorActualInversion.objects.update_or_create(
                    inversion=form.instance,
                    defaults={
                        'valor_unitario': valor_manual,
                        'fuente': 'Manual'
                    }
                )
        else:
            # Si vuelve a marcar como actualizable, borramos el valor manual
            ValorActualInversion.objects.filter(inversion=form.instance).delete()

        return response

# 📌 Vista 4: Editar inversión existente
class InversionUpdateView(LoginRequiredMixin, UpdateView):
    model = Inversion
    form_class = InversionForm
    template_name = 'inversiones/inversion_form.html'
    success_url = reverse_lazy('finanzas:listar')

    def get_queryset(self):
        return Inversion.objects.filter(usuario=self.request.user)
    
    def form_valid(self, form):
        form.instance.usuario = self.request.user
        response = super().form_valid(form)

        if not form.instance.actualizable:
            valor_manual = form.cleaned_data.get('valor_unitario_manual')
            if valor_manual is not None:
                ValorActualInversion.objects.update_or_create(
                    inversion=form.instance,
                    defaults={
                        'valor_unitario': valor_manual,
                        'fuente': 'Manual'
                    }
                )
        else:
            # Si vuelve a marcar como actualizable, borramos el valor manual
            ValorActualInversion.objects.filter(inversion=form.instance).delete()

        return response


# 📌 Vista 5: Añadir movimiento a una inversión
class MovimientoCreateView(LoginRequiredMixin, CreateView):
    model = MovimientoInversion
    form_class = MovimientoInversionForm
    template_name = 'inversiones/movimiento_form.html'

    def dispatch(self, request, *args, **kwargs):
        self.inversion = get_object_or_404(Inversion, id=self.kwargs['pk'], usuario=request.user)
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        form.instance.inversion = self.inversion
        return super().form_valid(form)

    def get_success_url(self):
        return reverse_lazy('inversiones:detalle', kwargs={'pk': self.inversion.id})

class ResumenInversionesMensualView(LoginRequiredMixin, DetailView):
    model = ResumenInversionesMensual
    template_name = 'inversiones/resumen_mensual.html'
    context_object_name = 'resumen'

    def get_queryset(self):
        return ResumenInversionesMensual.objects.filter(usuario=self.request.user)
