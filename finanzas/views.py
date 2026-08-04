import logging
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.timezone import now
from django.views.decorators.http import require_POST
from django.db import models as db_models
import urllib.request
import urllib.parse
import json
import csv
import io
from decimal import Decimal, InvalidOperation
import calendar
import datetime
from django.views.generic import UpdateView

from .parsing import parse_decimal, parse_fecha

logger = logging.getLogger(__name__)

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
    TickerCatalogo,
)


def index(request):
    return HttpResponse("Bienvenido a finanzas")

@login_required
def resumen_mensual(request, anio, mes):
    usuario = request.user
    registro, created = RegistroMensual.objects.get_or_create(
        usuario=usuario, anio=anio, mes=mes
    )
    context = {'registro': registro, 'anio': anio, 'mes': mes, 'nuevo': created}
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
    registro, _ = RegistroMensual.objects.get_or_create(usuario=usuario, anio=anio, mes=mes)

    if request.method == 'POST':
        form = SaldoMensualCuentaForm(request.POST, usuario=usuario)
        if form.is_valid():
            saldo = form.save(commit=False)
            saldo.registro = registro
            saldo.save()
            return redirect('finanzas:gestionar_cuentas')
    else:
        form = SaldoMensualCuentaForm(usuario=usuario)
    return render(request, 'finanzas/nuevo_saldo.html', {'form': form})

@login_required
def detalle_cuenta(request, cuenta_id):
    cuenta = get_object_or_404(CuentaBancaria, id=cuenta_id, usuario=request.user)
    hoy = now().date()
    registro = None

    if request.method == 'POST':
        form = SaldoMensualCuentaForm(request.POST)
        if form.is_valid():
            mes = int(form.cleaned_data['mes'])
            anio = int(form.cleaned_data['anio'])
            if anio > hoy.year or (anio == hoy.year and mes > hoy.month):
                form.add_error(None, "No puedes registrar saldos en el futuro.")
            else:
                registro, _ = RegistroMensual.objects.get_or_create(
                    usuario=request.user, anio=anio, mes=mes
                )
                SaldoMensualCuenta.objects.update_or_create(
                    cuenta=cuenta, registro=registro,
                    defaults={'saldo': form.cleaned_data['saldo']}
                )
                messages.success(request, "Saldo guardado correctamente.")
                return redirect('finanzas:gestionar_cuentas')
        else:
            try:
                mes = int(request.POST.get('mes'))
                anio = int(request.POST.get('anio'))
                registro = RegistroMensual.objects.filter(
                    usuario=request.user, anio=anio, mes=mes
                ).first()
            except (ValueError, TypeError):
                registro = None
    else:
        anio = hoy.year
        mes = hoy.month
        registro, _ = RegistroMensual.objects.get_or_create(
            usuario=request.user, anio=anio, mes=mes
        )
        saldo_obj = SaldoMensualCuenta.objects.filter(cuenta=cuenta, registro=registro).first()
        form = SaldoMensualCuentaForm(initial={
            'saldo': saldo_obj.saldo if saldo_obj else '',
            'mes': mes, 'anio': anio
        })

    return render(request, 'finanzas/detalle_cuenta.html', {
        'cuenta': cuenta, 'form': form, 'registro': registro
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
        'tarjetas': tarjetas, 'saldos': saldos, 'registro': registro
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
                    tarjeta=tarjeta, registro=registro,
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
            'mes': mes, 'anio': anio
        })

    return render(request, 'finanzas/detalle_tarjeta.html', {
        'tarjeta': tarjeta, 'form': form, 'registro': registro
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
    registro = RegistroMensual.objects.filter(
        usuario=usuario, anio=fecha.year, mes=fecha.month
    ).first()
    if not registro:
        return JsonResponse({'valor': 0})
    return JsonResponse({'valor': float(registro.patrimonio_total)})


# ---------------------------------------------------------------------------
# Búsqueda de tickers (AJAX) — Yahoo Finance + cache local
# ---------------------------------------------------------------------------

@login_required
def buscar_ticker(request):
    """Endpoint AJAX: busca tickers en cache local y Yahoo Finance."""
    q = request.GET.get('q', '').strip()
    if len(q) < 2:
        return JsonResponse({'results': []})

    # 1. Buscar en cache local
    local = TickerCatalogo.objects.filter(
        db_models.Q(symbol__icontains=q) | db_models.Q(nombre__icontains=q)
    )[:8]

    if local.exists():
        results = [{
            'symbol': t.symbol, 'nombre': t.nombre,
            'exchange': t.exchange, 'tipo': t.tipo_activo,
        } for t in local]
        return JsonResponse({'results': results, 'source': 'cache'})

    # 2. Buscar en Yahoo Finance
    try:
        url = (
            'https://query2.finance.yahoo.com/v1/finance/search'
            f'?q={urllib.parse.quote(q)}&quotesCount=8&newsCount=0'
        )
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())

        quotes = data.get('quotes', [])
        results = []
        for quote in quotes:
            symbol = quote.get('symbol', '')
            nombre = quote.get('shortname', '') or quote.get('longname', '')
            exchange = quote.get('exchange', '')
            tipo = quote.get('quoteType', '')

            # Cache en local para futuras búsquedas
            TickerCatalogo.objects.update_or_create(
                symbol=symbol,
                defaults={
                    'nombre': nombre,
                    'exchange': exchange,
                    'tipo_activo': tipo,
                }
            )
            results.append({
                'symbol': symbol, 'nombre': nombre,
                'exchange': exchange, 'tipo': tipo,
            })

        return JsonResponse({'results': results, 'source': 'yahoo'})

    except Exception as e:
        logger.warning("Yahoo Finance fetch error for '%s': %s", q, e)
        # Fallback: búsqueda parcial en cache
        fallback = TickerCatalogo.objects.filter(
            db_models.Q(symbol__icontains=q) | db_models.Q(nombre__icontains=q)
        )[:8]
        results = [{
            'symbol': t.symbol, 'nombre': t.nombre,
            'exchange': t.exchange, 'tipo': t.tipo_activo,
        } for t in fallback]
        return JsonResponse({'results': results, 'source': 'cache_fallback'})


### Sub-modulo Inversiones ###

from django.contrib.auth.mixins import LoginRequiredMixin
from django.urls import reverse_lazy
from django.views.generic import ListView, DetailView, CreateView, UpdateView
from .models import Inversion, MovimientoInversion, ValorActualInversion, GrupoInversion, TIPOS_INVERSION, AportacionRecurrente
from .forms import InversionForm, MovimientoInversionForm, AportacionRecurrenteForm
from .models import ResumenInversionesMensual

MESES_ES_LARGO = [
    '', 'enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio',
    'julio', 'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre',
]


class InversionDetailView(LoginRequiredMixin, DetailView):
    model = Inversion
    template_name = 'inversiones/inversion_detail.html'
    context_object_name = 'inversion'

    def get_queryset(self):
        return Inversion.objects.filter(usuario=self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['movimientos'] = MovimientoInversion.objects.filter(inversion=self.object).order_by('-fecha')
        reglas = list(self.object.aportaciones_recurrentes.all())
        for regla in reglas:
            regla.pendientes = regla.meses_pendientes()
            regla.pendientes_fmt = [f'{MESES_ES_LARGO[m]} {a}' for a, m in regla.pendientes]
        context['aportaciones_recurrentes'] = reglas
        context['form_aportacion'] = AportacionRecurrenteForm()
        return context


class InversionCreateView(LoginRequiredMixin, CreateView):
    model = Inversion
    form_class = InversionForm
    template_name = 'inversiones/inversion_form.html'
    success_url = reverse_lazy('finanzas:listar')

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        profile = getattr(self.request.user, 'userprofile', None)
        kwargs['hogar'] = profile.hogar if profile else None
        kwargs['usuario'] = self.request.user
        return kwargs

    def form_valid(self, form):
        form.instance.usuario = self.request.user
        response = super().form_valid(form)
        if not form.instance.actualizable:
            valor_manual = form.cleaned_data.get('valor_unitario_manual')
            if valor_manual is not None:
                ValorActualInversion.objects.update_or_create(
                    inversion=form.instance,
                    defaults={'valor_unitario': valor_manual, 'fuente': 'Manual'}
                )
        else:
            ValorActualInversion.objects.filter(inversion=form.instance).delete()
        return response


class InversionUpdateView(LoginRequiredMixin, UpdateView):
    model = Inversion
    form_class = InversionForm
    template_name = 'inversiones/inversion_form.html'
    success_url = reverse_lazy('finanzas:listar')

    def get_queryset(self):
        return Inversion.objects.filter(usuario=self.request.user)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        profile = getattr(self.request.user, 'userprofile', None)
        kwargs['hogar'] = profile.hogar if profile else None
        kwargs['usuario'] = self.request.user
        return kwargs

    def form_valid(self, form):
        form.instance.usuario = self.request.user
        response = super().form_valid(form)
        if not form.instance.actualizable:
            valor_manual = form.cleaned_data.get('valor_unitario_manual')
            if valor_manual is not None:
                ValorActualInversion.objects.update_or_create(
                    inversion=form.instance,
                    defaults={'valor_unitario': valor_manual, 'fuente': 'Manual'}
                )
        else:
            ValorActualInversion.objects.filter(inversion=form.instance).delete()
        return response


class MovimientoCreateView(LoginRequiredMixin, CreateView):
    model = MovimientoInversion
    form_class = MovimientoInversionForm
    template_name = 'inversiones/movimiento_form.html'

    def dispatch(self, request, *args, **kwargs):
        self.inversion = get_object_or_404(Inversion, id=self.kwargs['pk'], usuario=request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['usuario'] = self.request.user
        return kwargs

    def get_initial(self):
        initial = super().get_initial()
        # Cartera por defecto del activo (prellenar la compra)
        if getattr(self, 'inversion', None) and self.inversion.grupo_id:
            initial['grupo'] = self.inversion.grupo_id
        return initial

    def form_valid(self, form):
        form.instance.inversion = self.inversion
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['view'] = self
        return context

    def get_success_url(self):
        return reverse_lazy('finanzas:listar')

class ResumenInversionesMensualView(LoginRequiredMixin, DetailView):
    model = ResumenInversionesMensual
    template_name = 'inversiones/resumen_mensual.html'
    context_object_name = 'resumen'

    def get_queryset(self):
        return ResumenInversionesMensual.objects.filter(usuario=self.request.user)

@login_required
@require_POST
def actualizar_precios_inversiones(request):
    """Llama a Yahoo Finance para actualizar el precio de cada activo con actualizable=True."""
    inversiones = Inversion.objects.filter(
        usuario=request.user,
        actualizable=True
    ).exclude(ticker__isnull=True).exclude(ticker='')

    actualizados = []
    errores = []

    for inv in inversiones:
        try:
            url = (
                'https://query2.finance.yahoo.com/v8/finance/chart/'
                f'{urllib.parse.quote(inv.ticker)}?interval=1d&range=1d'
            )
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read())

            precio = (
                data['chart']['result'][0]['meta'].get('regularMarketPrice')
                or data['chart']['result'][0]['meta'].get('previousClose')
            )

            if precio:
                ValorActualInversion.objects.update_or_create(
                    inversion=inv,
                    defaults={
                        'valor_unitario': precio,
                        'fuente': 'Yahoo Finance'
                    }
                )
                actualizados.append(f"{inv.ticker}: {precio}")

        except Exception as e:
            errores.append(f"{inv.ticker}: {e}")

    if actualizados:
        messages.success(request, f"Precios actualizados: {', '.join(actualizados)}")
    if errores:
        messages.warning(request, f"Errores: {', '.join(errores)}")

    return redirect('finanzas:listar')

@login_required
def importar_movimientos_csv(request):
    """
    Importa movimientos desde CSV de una sola tabla.
    Cabecera en la primera fila no vacía que empiece por 'Fecha'.
    Crea una Inversion por combinación ticker+broker (posiciones separadas por cuenta).
    """
    columnas = [
        'Fecha de Operación', 'Activo (Ticker)', 'Tipo de Operación',
        'Cantidad de Activos', 'Precio por Unidad', 'Monto Total (€)',
        'Broker/Plataforma', 'Comisiones (€)',
    ]

    if request.method != 'POST':
        return render(request, 'inversiones/importar_csv.html', {'columnas': columnas})

    archivo = request.FILES.get('archivo')
    if not archivo:
        messages.error(request, "No se subió ningún archivo.")
        return render(request, 'inversiones/importar_csv.html', {'columnas': columnas})

    # Decodificar — utf-8-sig elimina BOM de Excel/Sheets
    try:
        contenido = archivo.read().decode('utf-8-sig')
    except UnicodeDecodeError:
        try:
            archivo.seek(0)
            contenido = archivo.read().decode('latin-1')
        except Exception:
            messages.error(request, "No se pudo leer el archivo. Asegúrate de que es UTF-8 o Latin-1.")
            return redirect('finanzas:importar_csv')

    reader = csv.reader(io.StringIO(contenido))
    todas_filas = list(reader)

    # Localizar cabecera: primera fila con "fecha" en col 0
    cabecera_idx = None
    for i, fila in enumerate(todas_filas):
        if fila and fila[0].strip().lower().startswith('fecha'):
            cabecera_idx = i
            break

    if cabecera_idx is None:
        messages.error(request, "No se encontró la cabecera en el CSV. La primera columna debe llamarse 'Fecha de Operación'.")
        return redirect('finanzas:importar_csv')

    # Solo filas con algún contenido
    filas_datos = [
        f for f in todas_filas[cabecera_idx + 1:]
        if any(c.strip() for c in f)
    ]

    # ─── Helpers ────────────────────────────────────────────────────────────
    # parse_decimal y parse_fecha viven ahora en finanzas.parsing (compartidos
    # con la importación de extractos bancarios).

    def parse_tipo(s):
        s = s.strip().upper()
        if 'COMPRA' in s or 'BUY' in s:
            return 'COMPRA'
        if 'VENTA' in s or 'SELL' in s:
            return 'VENTA'
        if 'DIVID' in s:
            return 'DIVIDENDO'
        return None

    def inferir_tipo_activo(ticker):
        t = ticker.upper()
        if any(x in t for x in ('BTC', 'ETH', 'EUR', 'USDT', 'SOL', 'XRP')):
            return 'CRIPTO'
        if t.endswith('.PA') or t.endswith('.MC') or t.endswith('.DU') or t.endswith('.L'):
            return 'ACCION'
        if t.endswith('.DU') or 'ETF' in t or t.startswith('I5') or t.startswith('IWDA'):
            return 'ETF'
        return 'OTRO'

    # ─── Procesar filas ──────────────────────────────────────────────────────

    creados   = 0
    ignorados = 0
    errores   = []
    inversiones_cache = {}  # clave: "TICKER|BROKER"

    for i, fila in enumerate(filas_datos, start=cabecera_idx + 2):
        # Padding por si la fila tiene menos columnas
        while len(fila) < 8:
            fila.append('')

        fecha_str  = fila[0].strip()
        ticker_raw = fila[1].strip().upper()
        tipo_str   = fila[2].strip()
        cantidad_s = fila[3].strip()
        precio_s   = fila[4].strip()
        monto_s    = fila[5].strip()
        broker     = fila[6].strip()
        comision_s = fila[7].strip()

        # Ignorar filas vacías
        if not fecha_str or not ticker_raw:
            continue

        # Fecha
        fecha = parse_fecha(fecha_str)
        if not fecha:
            errores.append(f"Fila {i}: fecha inválida '{fecha_str}'")
            continue

        # Tipo de operación
        tipo = parse_tipo(tipo_str)
        if not tipo:
            errores.append(f"Fila {i}: tipo desconocido '{tipo_str}' para {ticker_raw}")
            continue

        # Importes
        cantidad = parse_decimal(cantidad_s)
        precio   = parse_decimal(precio_s)
        monto    = parse_decimal(monto_s)
        comision = parse_decimal(comision_s) or Decimal('0')

        # Derivar campo faltante entre cantidad y precio
        if cantidad and not precio and monto and monto > 0:
            precio = round(monto / cantidad, 8)
        elif precio and not cantidad and monto and monto > 0:
            cantidad = round(monto / precio, 8)

        if not cantidad or not precio:
            errores.append(f"Fila {i}: datos insuficientes (cantidad/precio) para {ticker_raw} ({fecha_str})")
            continue

        # ─── Buscar o crear Inversion (por ticker + broker) ─────────────────
        cache_key = f"{ticker_raw}|{broker.upper()}"

        if cache_key not in inversiones_cache:
            inv = Inversion.objects.filter(
                usuario=request.user,
                ticker__iexact=ticker_raw,
                plataforma__iexact=broker,
            ).first()

            if not inv:
                inv = Inversion.objects.create(
                    usuario=request.user,
                    nombre=ticker_raw,
                    ticker=ticker_raw,
                    tipo=inferir_tipo_activo(ticker_raw),
                    moneda='EUR',
                    plataforma=broker,
                    cantidad_actual=Decimal('0'),
                    actualizable=True,
                )

            inversiones_cache[cache_key] = inv

        inv = inversiones_cache[cache_key]

        # ─── Evitar duplicados exactos ───────────────────────────────────────
        existe = MovimientoInversion.objects.filter(
            inversion=inv,
            fecha=fecha,
            tipo=tipo,
            cantidad=cantidad,
            precio_unitario=precio,
        ).exists()

        if existe:
            ignorados += 1
            continue

        # ─── Crear movimiento ────────────────────────────────────────────────
        mov = MovimientoInversion(
            inversion=inv,
            fecha=fecha,
            tipo=tipo,
            cantidad=cantidad,
            precio_unitario=precio,
            comision=comision,
        )
        try:
            mov.full_clean()
        except ValidationError as e:
            errores.append(f"Fila {i}: {'; '.join(e.messages)} ({ticker_raw})")
            continue
        mov.save()
        creados += 1

    # ─── Sincronizar cantidad_actual desde movimientos ───────────────────────
    for inv in inversiones_cache.values():
        inv.sincronizar_cantidad()

    # ─── Mensajes de resultado ───────────────────────────────────────────────
    resumen = f"✅ {creados} movimientos importados."
    if ignorados:
        resumen += f" {ignorados} duplicados ignorados."
    messages.success(request, resumen)

    for e in errores[:5]:
        messages.warning(request, e)
    if len(errores) > 5:
        messages.warning(request, f"... y {len(errores) - 5} errores más.")

    return redirect('finanzas:listar')

# ──────────────────────────────────────────────────────────────────────
# Vista listado de inversiones
# ──────────────────────────────────────────────────────────────────────
class InversionListView(LoginRequiredMixin, ListView):
    model = Inversion
    template_name = 'inversiones/inversion_list.html'
    context_object_name = 'inversiones'

    def get_queryset(self):
        from core.models import UserProfile
        profile = UserProfile.objects.filter(user=self.request.user).select_related('hogar').first()
        if profile and profile.hogar:
            miembros_ids = profile.hogar.miembros.values_list('user_id', flat=True)
            qs = Inversion.objects.filter(usuario_id__in=miembros_ids)
        else:
            qs = Inversion.objects.filter(usuario=self.request.user)
        # Filtro por cartera (grupo de inversión): las carteras son del propio usuario.
        # La cartera es a nivel de COMPRA, así que incluimos los activos que tengan
        # al menos una compra asignada a esa cartera (no solo la "cartera por defecto").
        cartera_id = self.request.GET.get('cartera')
        if cartera_id:
            qs = qs.filter(
                usuario=self.request.user,
                movimientos__grupo_id=cartera_id,
                movimientos__tipo=MovimientoInversion.COMPRA,
            ).distinct()
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        inversiones = context['inversiones']

        inv_data = []
        for inv in inversiones:
            try:
                valor_unitario = inv.valor_actual.valor_unitario
            except AttributeError:
                valor_unitario = None

            cantidad = inv.total_activos
            valor_total = inv.valor_total_actual
            coste_base = inv.coste_base_actual
            rentabilidad = inv.rentabilidad_latente_pct
            ganancia_real = inv.ganancia_realizada

            movs = []
            carteras_activo = []
            carteras_vistas = set()
            for m in inv.movimientos.select_related('grupo').order_by('-fecha'):
                movs.append({
                    'id': m.id,
                    'fecha': m.fecha,
                    'tipo': m.tipo,
                    'cantidad': m.cantidad,
                    'precio_unitario': m.precio_unitario,
                    'comision': m.comision,
                    'coste_total': (m.cantidad * m.precio_unitario) + m.comision,
                    'grupo_nombre': m.grupo.nombre if m.grupo_id else None,
                    'grupo_color': m.grupo.color if m.grupo_id else None,
                })
                # Carteras presentes en las COMPRA de este activo (para el badge de fila)
                if m.tipo == MovimientoInversion.COMPRA and m.grupo_id and m.grupo_id not in carteras_vistas:
                    carteras_vistas.add(m.grupo_id)
                    carteras_activo.append({'nombre': m.grupo.nombre, 'color': m.grupo.color})

            inv_data.append({
                'inv': inv,
                'owner': inv.usuario.first_name or inv.usuario.username,
                'es_propio': inv.usuario_id == self.request.user.id,
                'valor_unitario': valor_unitario,
                'valor_total': valor_total,
                'cantidad': cantidad,
                'coste_base': coste_base,
                'rentabilidad': rentabilidad,
                'ganancia_realizada': ganancia_real,
                'carteras': carteras_activo,
                'movimientos': movs,
            })

        # ─── Separar depósitos (excluir_de_patrimonio) del resto de la cartera ──
        # No se mezclan en los totales/rentabilidad de mercado: tienen su propia
        # sección y sus propios subtotales.
        deposit_data = [d for d in inv_data if d['inv'].excluir_de_patrimonio]
        inv_data = [d for d in inv_data if not d['inv'].excluir_de_patrimonio]

        # ─── Totales cartera (sin depósitos) ─────────────────────────────────
        total_valor_actual = sum(d['valor_total'] for d in inv_data)
        total_coste_base = sum(d['coste_base'] for d in inv_data)
        total_aportado = sum(d['inv'].valor_aportado for d in inv_data)
        total_ganancia_realizada = sum(d['ganancia_realizada'] for d in inv_data)

        rentabilidad_total = 0
        if total_coste_base > 0:
            rentabilidad_total = float((total_valor_actual - total_coste_base) / total_coste_base * 100)

        # ─── Subtotales de depósitos ──────────────────────────────────────────
        deposit_total_aportado = sum(d['inv'].valor_aportado for d in deposit_data)
        deposit_total_valor = sum(d['valor_total'] for d in deposit_data)

        # ─── Carteras (grupos de inversión) del usuario ─────────────────────
        carteras = list(GrupoInversion.objects.filter(usuario=self.request.user))
        cartera_sel = None
        cartera_sel_id = self.request.GET.get('cartera') or ''
        if cartera_sel_id:
            cartera_sel = next(
                (c for c in carteras if str(c.id) == str(cartera_sel_id)), None
            )
        cartera_kpis = None
        if cartera_sel is not None:
            valor = cartera_sel.valor_cartera or 0
            aportado = cartera_sel.total_aportado or 0
            cartera_kpis = {
                'nombre': cartera_sel.nombre,
                'color': cartera_sel.color,
                'valor': valor,
                'aportado': aportado,
                'ganancia': float(valor) - float(aportado),
                'rentabilidad': cartera_sel.rentabilidad,
                'num_activos': cartera_sel.num_activos,
            }

        context.update({
            'inv_data': inv_data,
            'total_valor_actual': total_valor_actual,
            'total_coste_base': total_coste_base,
            'total_aportado': total_aportado,
            'total_ganancia_realizada': total_ganancia_realizada,
            'rentabilidad_total': rentabilidad_total,
            'deposit_data': deposit_data,
            'deposit_total_aportado': deposit_total_aportado,
            'deposit_total_valor': deposit_total_valor,
            'carteras': carteras,
            'cartera_sel_id': cartera_sel_id,
            'cartera_kpis': cartera_kpis,
            'tipos_inversion': TIPOS_INVERSION,
        })
        return context

# ──────────────────────────────────────────────────────────────────────
# Vista editar movimiento (separada, sin contexto de inversiones)
# ──────────────────────────────────────────────────────────────────────
class MovimientoUpdateView(LoginRequiredMixin, UpdateView):
    model = MovimientoInversion
    form_class = MovimientoInversionForm
    template_name = 'inversiones/movimiento_form.html'

    def get_queryset(self):
        return MovimientoInversion.objects.filter(inversion__usuario=self.request.user)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['usuario'] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # El template movimiento_form.html usa "view.inversion" — lo exponemos:
        context['view'] = self
        self.inversion = self.object.inversion
        return context

    def get_success_url(self):
        return reverse_lazy('finanzas:listar')

# ──────────────────────────────────────────────────────────────────────
# Carteras de inversión (GrupoInversion) + acción masiva sobre inversiones
# ──────────────────────────────────────────────────────────────────────
@login_required
def carteras_inversion(request):
    """Listado y gestión (crear / renombrar / borrar) de las carteras del usuario,
    con la rentabilidad agregada de cada una."""
    if request.method == 'POST':
        accion = request.POST.get('accion')
        if accion == 'crear':
            nombre = (request.POST.get('nombre') or '').strip()
            color = (request.POST.get('color') or '#2d6a4f').strip()
            if nombre:
                if GrupoInversion.objects.filter(usuario=request.user, nombre=nombre).exists():
                    messages.error(request, f'Ya tienes una cartera llamada «{nombre}».')
                else:
                    GrupoInversion.objects.create(usuario=request.user, nombre=nombre, color=color)
                    messages.success(request, f'Cartera «{nombre}» creada.')
            else:
                messages.error(request, 'El nombre de la cartera no puede estar vacío.')
        elif accion == 'editar':
            cartera = get_object_or_404(GrupoInversion, id=request.POST.get('cartera_id'), usuario=request.user)
            nombre = (request.POST.get('nombre') or '').strip()
            color = (request.POST.get('color') or cartera.color).strip()
            if nombre:
                dup = GrupoInversion.objects.filter(usuario=request.user, nombre=nombre).exclude(id=cartera.id)
                if dup.exists():
                    messages.error(request, f'Ya tienes una cartera llamada «{nombre}».')
                else:
                    cartera.nombre = nombre
                    cartera.color = color
                    cartera.save(update_fields=['nombre', 'color'])
                    messages.success(request, 'Cartera actualizada.')
        elif accion == 'borrar':
            cartera = get_object_or_404(GrupoInversion, id=request.POST.get('cartera_id'), usuario=request.user)
            nombre = cartera.nombre
            cartera.delete()  # Inversion.grupo -> SET_NULL, los activos no se borran
            messages.success(request, f'Cartera «{nombre}» eliminada.')
        return redirect('finanzas:carteras')

    carteras = GrupoInversion.objects.filter(usuario=request.user)
    carteras_data = [{
        'obj': c,
        'valor': c.valor_cartera or 0,
        'aportado': c.total_aportado or 0,
        'rentabilidad': c.rentabilidad,
        'num_activos': c.num_activos,
    } for c in carteras]
    return render(request, 'inversiones/carteras.html', {
        'carteras_data': carteras_data,
    })


@login_required
@require_POST
def inversiones_accion_masiva(request):
    """Aplica una acción en bloque a las inversiones seleccionadas:
    cambiar el tipo de producto (set_tipo) o asignarlas a una cartera (set_grupo)."""
    ids = request.POST.getlist('inversion_ids')
    accion = request.POST.get('accion_masiva')
    # Solo inversiones del propio usuario (aunque el listado muestre las del hogar)
    qs = Inversion.objects.filter(usuario=request.user, id__in=ids)
    total = qs.count()

    if not ids:
        messages.error(request, 'No has seleccionado ninguna inversión.')
    elif total == 0:
        messages.error(request, 'No se han encontrado inversiones propias en la selección.')
    elif accion == 'set_tipo':
        tipo = request.POST.get('valor_tipo')
        tipos_validos = {t[0] for t in TIPOS_INVERSION}
        if tipo in tipos_validos:
            qs.update(tipo=tipo)
            messages.success(request, f'Tipo actualizado en {total} inversión(es).')
        else:
            messages.error(request, 'Tipo de producto no válido.')
    elif accion == 'set_grupo':
        grupo_id = request.POST.get('valor_grupo')
        # La cartera es a nivel de compra: fijamos la cartera por defecto del activo
        # (Inversion.grupo) y la propagamos a todas sus COMPRA.
        compras = MovimientoInversion.objects.filter(inversion__in=qs, tipo=MovimientoInversion.COMPRA)
        if grupo_id:
            grupo = get_object_or_404(GrupoInversion, id=grupo_id, usuario=request.user)
            qs.update(grupo=grupo)
            compras.update(grupo=grupo)
            messages.success(request, f'{total} activo(s) y sus compras asignados a «{grupo.nombre}».')
        else:
            qs.update(grupo=None)
            compras.update(grupo=None)
            messages.success(request, f'Cartera retirada de {total} activo(s) y sus compras.')
    else:
        messages.error(request, 'Acción no reconocida.')

    destino = request.POST.get('next') or str(reverse_lazy('finanzas:listar'))
    return redirect(destino)


# ──────────────────────────────────────────────────────────────────────
# Aportación recurrente (para depósitos y activos con aportaciones periódicas)
# ──────────────────────────────────────────────────────────────────────
@login_required
def aportacion_recurrente_crear(request, pk):
    inv = get_object_or_404(Inversion, id=pk, usuario=request.user)
    if request.method == 'POST':
        form = AportacionRecurrenteForm(request.POST)
        if form.is_valid():
            regla = form.save(commit=False)
            regla.inversion = inv
            regla.save()
            messages.success(request, 'Aportación recurrente creada.')
        else:
            messages.error(request, 'Revisa los datos de la aportación recurrente.')
    return redirect('finanzas:detalle', pk=inv.id)


@login_required
def aportacion_recurrente_editar(request, pk, regla_id):
    inv = get_object_or_404(Inversion, id=pk, usuario=request.user)
    regla = get_object_or_404(AportacionRecurrente, id=regla_id, inversion=inv)
    if request.method == 'POST':
        form = AportacionRecurrenteForm(request.POST, instance=regla)
        if form.is_valid():
            form.save()
            messages.success(request, 'Aportación recurrente actualizada.')
        else:
            messages.error(request, 'Revisa los datos de la aportación recurrente.')
    return redirect('finanzas:detalle', pk=inv.id)


@login_required
@require_POST
def aportacion_recurrente_eliminar(request, pk, regla_id):
    inv = get_object_or_404(Inversion, id=pk, usuario=request.user)
    regla = get_object_or_404(AportacionRecurrente, id=regla_id, inversion=inv)
    regla.delete()
    messages.success(request, 'Aportación recurrente eliminada. Los movimientos ya generados no se borran.')
    return redirect('finanzas:detalle', pk=inv.id)


@login_required
@require_POST
def aportacion_recurrente_generar(request, pk, regla_id):
    """Genera los movimientos de COMPRA pendientes de la regla, hasta hoy (o
    hasta su fecha_fin). No hace nada automáticamente en segundo plano: solo
    actúa cuando el usuario pulsa el botón, y cada movimiento generado queda
    como un movimiento normal, editable o borrable desde el listado."""
    inv = get_object_or_404(Inversion, id=pk, usuario=request.user)
    regla = get_object_or_404(AportacionRecurrente, id=regla_id, inversion=inv)

    pendientes = regla.meses_pendientes()
    creados = 0
    for anio, mes in pendientes:
        dia = min(regla.dia_mes, calendar.monthrange(anio, mes)[1])
        MovimientoInversion.objects.create(
            inversion=inv,
            fecha=datetime.date(anio, mes, dia),
            tipo=MovimientoInversion.COMPRA,
            cantidad=regla.importe,
            precio_unitario=Decimal('1'),
            grupo=inv.grupo,
            origen_recurrente=regla,
        )
        creados += 1

    if creados:
        messages.success(request, f'Se han generado {creados} aportación(es) pendiente(s).')
    else:
        messages.info(request, 'No había aportaciones pendientes que generar.')
    return redirect('finanzas:detalle', pk=inv.id)
