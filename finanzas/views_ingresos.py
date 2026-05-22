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

    miembros = hogar.miembros.select_related('user').all()
    ingresos_por_miembro = []

    total_mensual_hogar = Decimal('0')
    total_ponderado_hogar = Decimal('0')
    total_anual_hogar = Decimal('0')

    for miembro in miembros:
        fuentes = FuenteIngreso.objects.filter(
            usuario=miembro.user, hogar=hogar, activo=True
        ).select_related('destino')

        total_mensual = Decimal('0')
        total_ponderado = Decimal('0')
        total_anual = Decimal('0')
        fuentes_detalle = []


        for f in fuentes:
            bruto_anual = f.importe_anual_bruto
            estimado_anual = f.importe_anual_estimado

            if f.es_bruto and bruto_anual > 0:
                resultado = calcular_neto_anual(bruto_anual, f.pais_fiscal)
                ratio_neto = resultado['neto'] / bruto_anual if bruto_anual > 0 else Decimal('1')

                # Base mensual neta = base bruta * ratio
                neto_mensual_base = round(f.importe_mensual_base * ratio_neto, 2)
                # Ponderado neto = estimado anual * ratio / 12
                neto_mensual_ponderado = round(estimado_anual * ratio_neto / Decimal('12'), 2)
                # Neto anual
                neto_anual = round(estimado_anual * ratio_neto, 2)
                # Neto por cobro (lo que llega cada vez)
                neto_por_cobro = round(f.importe_neto_por_cobro * ratio_neto, 2)

                tipo_efectivo = resultado['tipo_efectivo']
                irpf = resultado['irpf']
                ss = resultado['ss']
            else:
                neto_mensual_base = f.importe_mensual_base
                neto_mensual_ponderado = f.importe_mensual_ponderado
                neto_anual = estimado_anual
                neto_por_cobro = f.importe_neto_por_cobro
                tipo_efectivo = Decimal('0')
                irpf = Decimal('0')
                ss = Decimal('0')

            fuentes_detalle.append({
                'fuente': f,
                'neto_mensual_base': neto_mensual_base,
                'neto_mensual_ponderado': neto_mensual_ponderado,
                'neto_anual': neto_anual,
                'neto_por_cobro': neto_por_cobro,
                'tipo_efectivo': tipo_efectivo,
                'irpf': irpf,
                'ss': ss,
            })

            if f.es_mensual_recurrente and f.incluir_en_mensual:
                total_mensual += neto_mensual_base
            total_ponderado += neto_mensual_ponderado
            total_anual += neto_anual

        total_mensual_hogar += total_mensual
        total_ponderado_hogar += total_ponderado
        total_anual_hogar += total_anual

        ingresos_por_miembro.append({
            'miembro': miembro,
            'fuentes': fuentes_detalle,
            'total_mensual': total_mensual,
            'total_ponderado': total_ponderado,
            'total_anual': total_anual,
        })

    destinos = DestinoIngreso.objects.filter(hogar=hogar, activo=True)

    return render(request, 'finanzas/ingresos/listar.html', {
        'ingresos_por_miembro': ingresos_por_miembro,
        'total_mensual_hogar': total_mensual_hogar,
        'total_ponderado_hogar': total_ponderado_hogar,
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
        tipo = request.POST.get('tipo', 'fijo')
        modo_entrada = request.POST.get('modo_entrada', 'anual')
        importe_declarado = request.POST.get('importe_declarado')
        es_bruto = request.POST.get('es_bruto') == 'true'
        pais_fiscal = request.POST.get('pais_fiscal', 'ES')
        num_pagas = int(request.POST.get('num_pagas', 12))
        meses_pagas_extras = request.POST.get('meses_pagas_extras', '6,12')
        periodicidad = request.POST.get('periodicidad', 'mensual')
        meses_cobro_list = request.POST.getlist('meses_cobro')
        meses_cobro = ','.join(meses_cobro_list) if meses_cobro_list else ''
        porcentaje_variabilidad = request.POST.get('porcentaje_variabilidad') or '0'
        incluir_en_mensual = 'incluir_en_mensual' in request.POST
        destino_id = request.POST.get('destino_id') or None

        if not nombre or not importe_declarado:
            messages.error(request, "Nombre e importe son obligatorios.")
        else:
            from django.contrib.auth.models import User
            usuario = get_object_or_404(User, id=usuario_id)

            FuenteIngreso.objects.create(
                usuario=usuario,
                hogar=hogar,
                nombre=nombre,
                tipo=tipo,
                modo_entrada=modo_entrada,
                importe_declarado=Decimal(importe_declarado),
                es_bruto=es_bruto,
                pais_fiscal=pais_fiscal,
                num_pagas=num_pagas,
                meses_pagas_extras=meses_pagas_extras,
                periodicidad=periodicidad,
                meses_cobro=meses_cobro,
                porcentaje_variabilidad=Decimal(porcentaje_variabilidad),
                incluir_en_mensual=incluir_en_mensual,
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
        fuente.tipo = request.POST.get('tipo', 'fijo')
        fuente.modo_entrada = request.POST.get('modo_entrada', 'anual')
        fuente.importe_declarado = Decimal(request.POST.get('importe_declarado', '0'))
        fuente.es_bruto = request.POST.get('es_bruto') == 'true'
        fuente.pais_fiscal = request.POST.get('pais_fiscal', 'ES')
        fuente.num_pagas = int(request.POST.get('num_pagas', 12))
        fuente.meses_pagas_extras = request.POST.get('meses_pagas_extras', '6,12')
        fuente.periodicidad = request.POST.get('periodicidad', 'mensual')
        meses_cobro_list = request.POST.getlist('meses_cobro')
        fuente.meses_cobro = ','.join(meses_cobro_list) if meses_cobro_list else ''
        porcentaje_var = request.POST.get('porcentaje_variabilidad') or '0'
        fuente.porcentaje_variabilidad = Decimal(porcentaje_var)
        fuente.incluir_en_mensual = 'incluir_en_mensual' in request.POST
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