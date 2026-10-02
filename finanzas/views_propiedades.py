import datetime
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse

from . import costes_activo
from .models import CategoriaGasto, PartidaGasto, Propiedad, HistorialPropiedad

MESES_NOMBRES = ['', 'Ene', 'Feb', 'Mar', 'Abr', 'May', 'Jun',
                 'Jul', 'Ago', 'Sep', 'Oct', 'Nov', 'Dic']


def _get_hogar(request):
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        return None, None
    return profile, profile.hogar


@login_required
def listar_propiedades(request):
    profile, hogar = _get_hogar(request)
    if not hogar:
        return redirect('dashboard')

    propiedades = Propiedad.objects.filter(hogar=hogar, activo=True)
    total_valor = sum(p.valor_actual for p in propiedades)
    total_deuda = sum(p.deuda_hipotecaria for p in propiedades)
    total_neto = total_valor - total_deuda

    # Una propiedad no solo vale dinero: cuesta dinero. El coste de tenerla
    # (IBI, comunidad, seguro, derramas) es tan parte de la foto como su valor.
    anio = _anio_elegido(request)
    propiedades_con_venta = [
        {
            'propiedad': p,
            'neto_venta': p.calcular_neto_venta(),
            'costes': costes_activo.costes(p, anio),
        }
        for p in propiedades
    ]
    total_coste_anual = sum(
        (d['costes']['real_anual'] for d in propiedades_con_venta), Decimal('0'),
    )
    total_coste_teorico = sum(
        (d['costes']['teorico_anual'] for d in propiedades_con_venta), Decimal('0'),
    )
    anios_disponibles = sorted(
        {a for d in propiedades_con_venta for a in d['costes']['anios_con_datos']}
        | {date.today().year, anio},
        reverse=True,
    )

    return render(request, 'finanzas/propiedades/listar.html', {
        'hogar': hogar,
        'propiedades_con_venta': propiedades_con_venta,
        'total_valor': total_valor,
        'total_deuda': total_deuda,
        'total_neto': total_neto,
        'anio': anio,
        'anios_disponibles': anios_disponibles,
        'total_coste_anual': total_coste_anual,
        'total_coste_teorico': total_coste_teorico,
        'sin_imputar': PartidaGasto.objects.filter(
            hogar=hogar, activo=True, vehiculo__isnull=True, propiedad__isnull=True,
        ).select_related('categoria'),
    })


def _anio_elegido(request):
    try:
        return int(request.GET.get('anio'))
    except (TypeError, ValueError):
        return date.today().year


def _decimal_o_none(texto):
    texto = (texto or '').strip().replace('%', '').replace('€', '').replace(' ', '')
    if not texto:
        return None
    if ',' in texto:
        texto = texto.replace('.', '').replace(',', '.')
    elif re.fullmatch(r'\d{1,3}(\.\d{3})+', texto):
        texto = texto.replace('.', '')  # «1.200» son mil doscientos, no uno coma dos
    return Decimal(texto).quantize(Decimal('0.01'))


def _miembros(hogar):
    from core.models import UserProfile
    return [p.user for p in UserProfile.objects.filter(hogar=hogar).select_related('user')]


def _leer_alquiler(propiedad, post, hogar):
    """Los datos del alquiler del formulario de la propiedad."""
    propiedad.alquilada = 'alquilada' in post
    titular = post.get('propietario') or ''
    propiedad.propietario = next((u for u in _miembros(hogar) if str(u.pk) == titular), None)
    try:
        propiedad.reduccion_alquiler_pct = int(post.get('reduccion_alquiler_pct') or 0)
    except ValueError:
        propiedad.reduccion_alquiler_pct = 0
    propiedad.pct_construccion = _decimal_o_none(post.get('pct_construccion'))
    propiedad.intereses_hipoteca_anuales = _decimal_o_none(post.get('intereses_hipoteca_anuales'))


def _contexto_form(hogar, accion, propiedad):
    return {
        'hogar': hogar,
        'accion': accion,
        'propiedad': propiedad,
        'tipos': Propiedad.TIPO_CHOICES,
        'miembros': _miembros(hogar),
        'reducciones': Propiedad.REDUCCION_CHOICES,
    }


@login_required
def crear_propiedad(request):
    profile, hogar = _get_hogar(request)
    if not hogar:
        return redirect('dashboard')

    if request.method == 'POST':
        try:
            p = Propiedad(
                hogar=hogar,
                nombre=request.POST['nombre'].strip(),
                tipo=request.POST.get('tipo', 'vivienda'),
                descripcion=request.POST.get('descripcion', '').strip(),
                fecha_compra=request.POST['fecha_compra'],
                precio_compra=Decimal(request.POST['precio_compra'].replace(',', '.')),
                gastos_compra=Decimal(request.POST.get('gastos_compra', '0').replace(',', '.') or '0'),
                valor_actual=Decimal(request.POST['valor_actual'].replace(',', '.')),
                deuda_hipotecaria=Decimal(request.POST.get('deuda_hipotecaria', '0').replace(',', '.') or '0'),
                gastos_venta_pct=Decimal(request.POST.get('gastos_venta_pct', '6').replace(',', '.') or '6'),
                es_residencia_habitual='es_residencia_habitual' in request.POST,
                color=request.POST.get('color', '#e67e22'),
            )
            _leer_alquiler(p, request.POST, hogar)
            p.full_clean()
            p.save()
            # Auto-registrar snapshot del mes actual para que aparezca en Evolución
            hoy = datetime.date.today()
            HistorialPropiedad.objects.get_or_create(
                propiedad=p, año=hoy.year, mes=hoy.month,
                defaults={'valor_mercado': p.valor_actual, 'deuda_hipotecaria': p.deuda_hipotecaria},
            )
            messages.success(request, f"Propiedad '{p.nombre}' añadida.")
            return redirect('finanzas:listar_propiedades')
        except Exception as e:
            messages.error(request, f"Error al guardar: {e}")

    return render(request, 'finanzas/propiedades/form.html',
                  _contexto_form(hogar, 'Añadir propiedad', None))


@login_required
def editar_propiedad(request, pk):
    profile, hogar = _get_hogar(request)
    if not hogar:
        return redirect('dashboard')

    propiedad = get_object_or_404(Propiedad, pk=pk, hogar=hogar)

    if request.method == 'POST':
        try:
            propiedad.nombre = request.POST['nombre'].strip()
            propiedad.tipo = request.POST.get('tipo', 'vivienda')
            propiedad.descripcion = request.POST.get('descripcion', '').strip()
            propiedad.fecha_compra = request.POST['fecha_compra']
            propiedad.precio_compra = Decimal(request.POST['precio_compra'].replace(',', '.'))
            propiedad.gastos_compra = Decimal(request.POST.get('gastos_compra', '0').replace(',', '.') or '0')
            propiedad.valor_actual = Decimal(request.POST['valor_actual'].replace(',', '.'))
            propiedad.deuda_hipotecaria = Decimal(request.POST.get('deuda_hipotecaria', '0').replace(',', '.') or '0')
            propiedad.gastos_venta_pct = Decimal(request.POST.get('gastos_venta_pct', '6').replace(',', '.') or '6')
            propiedad.es_residencia_habitual = 'es_residencia_habitual' in request.POST
            propiedad.color = request.POST.get('color', '#e67e22')
            _leer_alquiler(propiedad, request.POST, hogar)
            propiedad.full_clean()
            propiedad.save()
            # Sincronizar snapshot del mes actual con los valores editados
            hoy = datetime.date.today()
            HistorialPropiedad.objects.update_or_create(
                propiedad=propiedad, año=hoy.year, mes=hoy.month,
                defaults={'valor_mercado': propiedad.valor_actual, 'deuda_hipotecaria': propiedad.deuda_hipotecaria},
            )
            messages.success(request, f"Propiedad '{propiedad.nombre}' actualizada.")
            return redirect('finanzas:listar_propiedades')
        except Exception as e:
            messages.error(request, f"Error al guardar: {e}")

    return render(request, 'finanzas/propiedades/form.html',
                  _contexto_form(hogar, 'Editar propiedad', propiedad))


@login_required
def eliminar_propiedad(request, pk):
    profile, hogar = _get_hogar(request)
    if not hogar:
        return redirect('dashboard')

    propiedad = get_object_or_404(Propiedad, pk=pk, hogar=hogar)
    if request.method == 'POST':
        nombre = propiedad.nombre
        propiedad.activo = False
        propiedad.save()
        messages.success(request, f"Propiedad '{nombre}' archivada.")
    return redirect('finanzas:listar_propiedades')


@login_required
def registrar_historial(request):
    """Registra o actualiza el valor+deuda mensual de una propiedad."""
    profile, hogar = _get_hogar(request)
    if not hogar:
        return redirect('dashboard')

    if request.method == 'POST':
        try:
            prop_id = int(request.POST.get('propiedad_id', 0))
            mes = int(request.POST.get('mes', 0))
            año = int(request.POST.get('año', 0))
            valor_raw = request.POST.get('valor_mercado', '').strip()
            deuda_raw = request.POST.get('deuda_hipotecaria', '').strip()
        except (ValueError, TypeError):
            messages.error(request, "Datos inválidos.")
            return redirect('finanzas:vista_evolucion')

        propiedad = get_object_or_404(Propiedad, id=prop_id, hogar=hogar)

        if not valor_raw:
            HistorialPropiedad.objects.filter(propiedad=propiedad, año=año, mes=mes).delete()
            messages.success(request, f"Historial de '{propiedad.nombre}' eliminado.")
        else:
            try:
                valor = Decimal(valor_raw.replace(',', '.'))
                deuda = Decimal(deuda_raw.replace(',', '.')) if deuda_raw else Decimal('0')
            except InvalidOperation:
                messages.error(request, "Importe inválido.")
                return redirect(f"/finanzas/evolucion/?año={año}")

            nota = request.POST.get('nota', '').strip()
            HistorialPropiedad.objects.update_or_create(
                propiedad=propiedad, año=año, mes=mes,
                defaults={'valor_mercado': valor, 'deuda_hipotecaria': deuda, 'nota': nota},
            )
            # Sincronizar valores actuales si es el mes en curso
            hoy = datetime.date.today()
            if año == hoy.year and mes == hoy.month:
                propiedad.valor_actual = valor
                propiedad.deuda_hipotecaria = deuda
                propiedad.save(update_fields=['valor_actual', 'deuda_hipotecaria'])
            messages.success(request, f"'{propiedad.nombre}' {mes}/{año}: actualizado.")

    año_redirect = request.POST.get('año', datetime.date.today().year)
    return redirect(f"/finanzas/evolucion/?año={año_redirect}")


@login_required
def alquiler_propiedad(request, pk):
    """Lo que deja una propiedad alquilada mes a mes y el IRPF que supone
    (ver `alquiler.py`)."""
    from .alquiler import analizar_alquiler

    profile, hogar = _get_hogar(request)
    if not hogar:
        return redirect('dashboard')
    propiedad = get_object_or_404(Propiedad, pk=pk, hogar=hogar)
    anio = _anio_elegido(request)
    datos = analizar_alquiler(propiedad, anio)
    anios = sorted(
        set(costes_activo.costes(propiedad, anio)['anios_con_datos']) | {date.today().year, anio},
        reverse=True,
    )
    return render(request, 'finanzas/propiedades/alquiler.html', {
        'a': datos,
        'anios': anios,
    })


@login_required
def alquiler_a_gasto(request, pk):
    """Crea (o actualiza) el gasto anual con el IRPF estimado del alquiler.

    Se paga con la declaración, en junio del año siguiente; se imputa a la
    propiedad —es parte de lo que cuesta tenerla alquilada— y a su titular.
    """
    from .alquiler import analizar_alquiler

    profile, hogar = _get_hogar(request)
    if not hogar:
        return redirect('dashboard')
    propiedad = get_object_or_404(Propiedad, pk=pk, hogar=hogar)
    try:
        anio = int(request.POST.get('anio'))
    except (TypeError, ValueError):
        anio = date.today().year
    destino = reverse('finanzas:alquiler_propiedad', args=[propiedad.pk]) + f'?anio={anio}'
    if request.method != 'POST':
        return redirect(destino)

    datos = analizar_alquiler(propiedad, anio)
    importe = datos['impuesto_total']
    if importe <= 0:
        messages.info(request, 'Con estas cifras el alquiler no sale a pagar: no hay gasto que añadir.')
        return redirect(destino)

    categoria, _ = CategoriaGasto.objects.get_or_create(
        hogar=hogar, nombre='IRPF alquiler', defaults={'tipo': 'anual'},
    )
    partida = propiedad.partida_irpf
    if partida is None or not partida.activo:
        partida = PartidaGasto(hogar=hogar, categoria=categoria, periodicidad='anual', mes_pago=6)
    partida.nombre = f'IRPF alquiler · {propiedad.nombre}'[:150]
    partida.importe = importe
    partida.categoria = categoria
    partida.propiedad = propiedad
    partida.vehiculo = None
    partida.responsable = propiedad.propietario
    partida.save()
    if propiedad.partida_irpf_id != partida.pk:
        propiedad.partida_irpf = partida
        propiedad.save(update_fields=['partida_irpf'])
    messages.success(
        request,
        f'Gasto anual «{partida.nombre}»: {importe} € en junio. Su provisión entra en la reserva de los fijos anuales.',
    )
    return redirect(destino)
