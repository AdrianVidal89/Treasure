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
    from .alquiler import analizar_alquiler

    propiedades_con_venta = []
    for p in propiedades:
        costes = costes_activo.costes(p, anio)
        item = {'propiedad': p, 'neto_venta': p.calcular_neto_venta(), 'costes': costes, 'alquiler': None}
        # Una alquilada no es solo lo que cuesta: lo que importa es lo que
        # deja. Se resta del coste mensual —la misma cifra de la tarjeta— lo
        # cobrado al mes, sobre los mismos meses cerrados.
        if p.alquilada or costes['renta']:
            a = analizar_alquiler(p, anio)
            item['alquiler'] = a
            item['neto_mensual'] = a['media_ingreso_cerrados'] - costes['ritmo_mensual']
        propiedades_con_venta.append(item)
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


def _decimal_o_none(texto, campo=''):
    texto = (texto or '').strip().replace('%', '').replace('€', '').replace(' ', '')
    # «None» era lo que pintaba el formulario en un campo vacío: es vacío.
    if not texto or texto.lower() == 'none':
        return None
    if ',' in texto:
        texto = texto.replace('.', '').replace(',', '.')
    elif re.fullmatch(r'\d{1,3}(\.\d{3})+', texto):
        texto = texto.replace('.', '')  # «1.200» son mil doscientos, no uno coma dos
    try:
        return Decimal(texto).quantize(Decimal('0.01'))
    except InvalidOperation:
        raise ValueError(f'«{texto}» no es un número{" en " + campo if campo else ""}.')


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
    from django.utils.dateparse import parse_date
    propiedad.alquilada_desde = parse_date(post.get('alquilada_desde') or '') or None
    propiedad.pct_construccion = _decimal_o_none(post.get('pct_construccion'), 'el % de construcción')
    propiedad.intereses_hipoteca_anuales = _decimal_o_none(
        post.get('intereses_hipoteca_anuales'), 'los intereses de la hipoteca',
    )


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


@login_required
def alquiler_cobros(request, pk):
    """Elegir, de los ingresos de los extractos, cuáles son el alquiler de esta
    propiedad.

    Es lo mismo que el chip «activo» de cada fila en Movimientos, pero desde la
    pregunta que uno se hace en la propiedad —«¿cuáles son los cobros del
    inquilino?»—, con todos los ingresos a la vista y marcando varios de una
    vez. Al guardar, los marcados quedan imputados a la propiedad y los que
    estaban imputados y se desmarcan, sueltos. Un cobro que contaba como
    traspaso (una transferencia que se tomó por propia) pasa a «Alquileres»,
    o no sumaría como ingreso.
    """
    from datetime import timedelta
    from extractos.models import MovimientoBancario
    from .models import COMPUTO_SUMA

    profile, hogar = _get_hogar(request)
    if not hogar:
        return redirect('dashboard')
    propiedad = get_object_or_404(Propiedad, pk=pk, hogar=hogar)
    desde = date.today().replace(day=1) - timedelta(days=550)
    candidatos = [
        m for m in MovimientoBancario.objects.filter(
            hogar=hogar, importe__gt=0, fecha__gte=desde, dividido_de__isnull=True,
        ).select_related('categoria', 'propiedad', 'vehiculo').prefetch_related('partes').order_by('-fecha', '-id')
        if not m.es_cobertura and not m.es_reembolso and not m.esta_dividido
    ]

    if request.method == 'POST':
        marcados = {int(x) for x in request.POST.getlist('mov') if x.isdigit()}
        cat_alquiler = None
        puestos = quitados = recategorizados = 0
        for m in candidatos:
            if m.pk in marcados:
                cambios = []
                if m.propiedad_id != propiedad.pk or m.vehiculo_id:
                    m.propiedad, m.vehiculo = propiedad, None
                    cambios += ['propiedad', 'vehiculo']
                    puestos += 1
                if not m.cuenta_como_ingreso:
                    if cat_alquiler is None:
                        cat_alquiler, _ = CategoriaGasto.objects.get_or_create(
                            hogar=hogar, nombre='Alquileres',
                            defaults={'tipo': 'ingreso', 'computo': COMPUTO_SUMA},
                        )
                    m.categoria, m.es_traspaso, m.estado_categorizacion = cat_alquiler, False, 'manual'
                    cambios += ['categoria', 'es_traspaso', 'estado_categorizacion']
                    recategorizados += 1
                if cambios:
                    m.save(update_fields=cambios)
            elif m.propiedad_id == propiedad.pk:
                m.propiedad = None
                m.save(update_fields=['propiedad'])
                quitados += 1
        texto = f'{propiedad.nombre}: {len(marcados)} cobro{"s" if len(marcados) != 1 else ""} de alquiler.'
        if recategorizados:
            texto += f' {recategorizados} contaban como traspaso y ahora cuentan como ingreso («Alquileres»).'
        messages.success(request, texto + ' Los que lleguen del mismo pagador en próximos extractos se asignarán solos.')
        return redirect(reverse('finanzas:listar_propiedades') + f'#propiedad-{propiedad.pk}')

    # Los que vienen del mismo pagador que los ya marcados, arriba y sugeridos:
    # el alquiler es el cobro que se repite cada mes.
    comercios = {m.comercio for m in candidatos if m.propiedad_id == propiedad.pk and m.comercio}
    filas = [
        {
            'mov': m,
            'marcado': m.propiedad_id == propiedad.pk,
            'sugerido': m.propiedad_id != propiedad.pk and m.comercio in comercios,
            'otro': m.propiedad if m.propiedad_id and m.propiedad_id != propiedad.pk else (m.vehiculo or None),
            'no_suma': not m.cuenta_como_ingreso,
        }
        for m in candidatos
    ]
    return render(request, 'finanzas/propiedades/cobros.html', {
        'propiedad': propiedad,
        'filas': filas,
        'num_marcados': sum(1 for f in filas if f['marcado']),
    })
