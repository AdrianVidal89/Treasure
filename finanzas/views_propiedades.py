import datetime
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.shortcuts import render, redirect, get_object_or_404

from .models import Propiedad, HistorialPropiedad

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

    propiedades_con_venta = [
        {'propiedad': p, 'neto_venta': p.calcular_neto_venta()}
        for p in propiedades
    ]

    return render(request, 'finanzas/propiedades/listar.html', {
        'hogar': hogar,
        'propiedades_con_venta': propiedades_con_venta,
        'total_valor': total_valor,
        'total_deuda': total_deuda,
        'total_neto': total_neto,
    })


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
            p.full_clean()
            p.save()
            messages.success(request, f"Propiedad '{p.nombre}' añadida.")
            return redirect('finanzas:listar_propiedades')
        except Exception as e:
            messages.error(request, f"Error al guardar: {e}")

    return render(request, 'finanzas/propiedades/form.html', {
        'hogar': hogar,
        'accion': 'Añadir propiedad',
        'propiedad': None,
        'tipos': Propiedad.TIPO_CHOICES,
    })


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
            propiedad.full_clean()
            propiedad.save()
            messages.success(request, f"Propiedad '{propiedad.nombre}' actualizada.")
            return redirect('finanzas:listar_propiedades')
        except Exception as e:
            messages.error(request, f"Error al guardar: {e}")

    return render(request, 'finanzas/propiedades/form.html', {
        'hogar': hogar,
        'accion': 'Editar propiedad',
        'propiedad': propiedad,
        'tipos': Propiedad.TIPO_CHOICES,
    })


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
