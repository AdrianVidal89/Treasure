from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from decimal import Decimal

from .models import FuenteIngreso, DestinoIngreso
from .fiscal import calcular_neto_anual


@login_required
def listar_ingresos(request):
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        messages.error(request, "Necesitas pertenecer a un hogar.")
        return redirect('dashboard')

    hogar = profile.hogar

    # Crear destinos predefinidos si no existen
    for nombre in DestinoIngreso.PREDEFINIDOS:
        DestinoIngreso.objects.get_or_create(
            hogar=hogar, nombre=nombre,
            defaults={'es_predefinido': True}
        )

    # Ingresos de todos los miembros del hogar
    miembros = hogar.miembros.select_related('user').all()
    ingresos_por_miembro = []

    total_mensual_hogar = Decimal('0')
    total_anual_hogar = Decimal('0')

    for miembro in miembros:
        fuentes = FuenteIngreso.objects.filter(
            usuario=miembro.user, hogar=hogar, activo=True
        ).select_related('destino')

        total_mensual = Decimal('0')
        total_anual = Decimal('0')
        fuentes_detalle = []

        for f in fuentes:
            # Calcular neto si es bruto
            if f.es_bruto and f.importe_anual > 0:
                resultado = calcular_neto_anual(f.importe_anual, f.pais_fiscal)
                neto_anual = resultado['neto']
                neto_mensual = round(neto_anual / 12, 2)
                tipo_efectivo = resultado['tipo_efectivo']
            else:
                neto_anual = f.importe_anual
                neto_mensual = f.importe_mensual_equivalente
                tipo_efectivo = Decimal('0')

            fuentes_detalle.append({
                'fuente': f,
                'neto_mensual': neto_mensual,
                'neto_anual': neto_anual,
                'tipo_efectivo': tipo_efectivo,
            })

            if f.es_mensual:
                total_mensual += neto_mensual
            total_anual += neto_anual

        total_mensual_hogar += total_mensual
        total_anual_hogar += total_anual

        ingresos_por_miembro.append({
            'miembro': miembro,
            'fuentes': fuentes_detalle,
            'total_mensual': total_mensual,
            'total_anual': total_anual,
        })

    destinos = DestinoIngreso.objects.filter(hogar=hogar, activo=True)

    return render(request, 'finanzas/ingresos/listar.html', {
        'ingresos_por_miembro': ingresos_por_miembro,
        'total_mensual_hogar': total_mensual_hogar,
        'total_anual_hogar': total_anual_hogar,
        'destinos': destinos,
        'hogar': hogar,
    })


@login_required
def crear_ingreso(request):
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        return redirect('dashboard')

    hogar = profile.hogar
    miembros = hogar.miembros.select_related('user').all()
    destinos = DestinoIngreso.objects.filter(hogar=hogar, activo=True)

    if request.method == 'POST':
        usuario_id = request.POST.get('usuario_id')
        nombre = request.POST.get('nombre', '').strip()
        importe = request.POST.get('importe')
        es_bruto = request.POST.get('es_bruto') == 'true'
        pais_fiscal = request.POST.get('pais_fiscal', 'ES')
        periodicidad = request.POST.get('periodicidad')
        mes_cobro = request.POST.get('mes_cobro') or None
        destino_id = request.POST.get('destino_id') or None

        if not nombre or not importe:
            messages.error(request, "Nombre e importe son obligatorios.")
        else:
            from django.contrib.auth.models import User
            usuario = get_object_or_404(User, id=usuario_id)

            fuente = FuenteIngreso.objects.create(
                usuario=usuario,
                hogar=hogar,
                nombre=nombre,
                importe=Decimal(importe),
                es_bruto=es_bruto,
                pais_fiscal=pais_fiscal,
                periodicidad=periodicidad,
                mes_cobro=int(mes_cobro) if mes_cobro else None,
                destino_id=int(destino_id) if destino_id else None,
            )
            messages.success(request, f"Ingreso '{nombre}' creado.")
            return redirect('finanzas:listar_ingresos')

    return render(request, 'finanzas/ingresos/crear.html', {
        'miembros': miembros,
        'destinos': destinos,
        'hogar': hogar,
    })

@login_required
def editar_ingreso(request, ingreso_id):
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        return redirect('dashboard')

    hogar = profile.hogar
    fuente = get_object_or_404(FuenteIngreso, id=ingreso_id, hogar=hogar)
    miembros = hogar.miembros.select_related('user').all()
    destinos = DestinoIngreso.objects.filter(hogar=hogar, activo=True)

    if request.method == 'POST':
        fuente.usuario_id = request.POST.get('usuario_id')
        fuente.nombre = request.POST.get('nombre', '').strip()
        fuente.importe = Decimal(request.POST.get('importe', '0'))
        fuente.es_bruto = request.POST.get('es_bruto') == 'true'
        fuente.pais_fiscal = request.POST.get('pais_fiscal', 'ES')
        fuente.periodicidad = request.POST.get('periodicidad')
        mes_cobro = request.POST.get('mes_cobro')
        fuente.mes_cobro = int(mes_cobro) if mes_cobro else None
        destino_id = request.POST.get('destino_id')
        fuente.destino_id = int(destino_id) if destino_id else None
        fuente.save()
        messages.success(request, f"Ingreso '{fuente.nombre}' actualizado.")
        return redirect('finanzas:listar_ingresos')

    from .models import MESES_CHOICES
    return render(request, 'finanzas/ingresos/editar.html', {
        'fuente': fuente,
        'miembros': miembros,
        'destinos': destinos,
        'hogar': hogar,
        'meses': MESES_CHOICES,
    })

@login_required
def eliminar_ingreso(request, ingreso_id):
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        return redirect('dashboard')

    fuente = get_object_or_404(FuenteIngreso, id=ingreso_id, hogar=profile.hogar)
    nombre = fuente.nombre
    fuente.delete()
    messages.success(request, f"Ingreso '{nombre}' eliminado.")
    return redirect('finanzas:listar_ingresos')


@login_required
def crear_destino(request):
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        return redirect('dashboard')

    if request.method == 'POST':
        nombre = request.POST.get('nombre', '').strip()
        if nombre:
            DestinoIngreso.objects.get_or_create(
                hogar=profile.hogar, nombre=nombre,
                defaults={'es_predefinido': False}
            )
            messages.success(request, f"Destino '{nombre}' creado.")
        else:
            messages.error(request, "El nombre no puede estar vacío.")

    return redirect('finanzas:listar_ingresos')


@login_required
def simular_neto(request):
    """Endpoint AJAX para calcular neto en tiempo real."""
    bruto = request.GET.get('bruto', '0')
    pais = request.GET.get('pais', 'ES')

    try:
        resultado = calcular_neto_anual(Decimal(bruto), pais)
        return JsonResponse({
            'success': True,
            'neto': float(resultado['neto']),
            'irpf': float(resultado['irpf']),
            'ss': float(resultado['ss']),
            'tipo_efectivo': float(resultado['tipo_efectivo']),
            'neto_mensual': float(resultado['neto'] / 12),
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})