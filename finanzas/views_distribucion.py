from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from decimal import Decimal

from .models import ReglaReparto, FondoFamiliar
from .distribucion import calcular_flujos


@login_required
def vista_distribucion(request):
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        messages.error(request, "Necesitas pertenecer a un hogar.")
        return redirect('dashboard')

    hogar = profile.hogar
    datos = calcular_flujos(hogar)
    fondos = FondoFamiliar.objects.filter(hogar=hogar, activo=True)
    reglas = ReglaReparto.objects.filter(hogar=hogar, activo=True).select_related('fondo', 'usuario')
    miembros = hogar.miembros.select_related('user').all()

    return render(request, 'finanzas/distribucion/vista.html', {
        'hogar': hogar,
        'd': datos,
        'fondos': fondos,
        'reglas': reglas,
        'miembros': miembros,
        'profile': profile,
    })


@login_required
def crear_fondo(request):
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        return redirect('dashboard')

    if request.method == 'POST':
        nombre = request.POST.get('nombre', '').strip()
        modo = request.POST.get('modo_aportacion', 'proporcional')
        color = request.POST.get('color', '#a259ff')
        cuenta = request.POST.get('cuenta_asociada', '').strip()

        if not nombre:
            messages.error(request, "El nombre es obligatorio.")
        else:
            max_orden = FondoFamiliar.objects.filter(hogar=profile.hogar).count()
            FondoFamiliar.objects.get_or_create(
                hogar=profile.hogar, nombre=nombre,
                defaults={
                    'modo_aportacion': modo,
                    'color': color,
                    'cuenta_asociada': cuenta,
                    'orden': max_orden,
                }
            )
            messages.success(request, f"Fondo '{nombre}' creado.")

    return redirect('finanzas:vista_distribucion')


@login_required
def eliminar_fondo(request, fondo_id):
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        return redirect('dashboard')

    fondo = get_object_or_404(FondoFamiliar, id=fondo_id, hogar=profile.hogar)
    nombre = fondo.nombre
    fondo.delete()
    messages.success(request, f"Fondo '{nombre}' eliminado.")
    return redirect('finanzas:vista_distribucion')


@login_required
def crear_regla(request):
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        return redirect('dashboard')

    if request.method == 'POST':
        nombre = request.POST.get('nombre', '').strip()
        tipo_regla = request.POST.get('tipo_regla', 'porcentaje')
        porcentaje = request.POST.get('porcentaje', '0') or '0'
        importe_fijo = request.POST.get('importe_fijo', '0') or '0'
        fondo_id = request.POST.get('fondo_id') or None
        usuario_id = request.POST.get('usuario_id') or None
        color = request.POST.get('color', '#a259ff')

        if not nombre:
            messages.error(request, "El nombre es obligatorio.")
        else:
            max_orden = ReglaReparto.objects.filter(hogar=profile.hogar).count()
            ReglaReparto.objects.create(
                hogar=profile.hogar,
                nombre=nombre,
                tipo_regla=tipo_regla,
                porcentaje=Decimal(porcentaje) if tipo_regla == 'porcentaje' else Decimal('0'),
                importe_fijo=Decimal(importe_fijo) if tipo_regla == 'fijo' else Decimal('0'),
                fondo_id=int(fondo_id) if fondo_id else None,
                usuario_id=int(usuario_id) if usuario_id else None,
                color=color,
                orden=max_orden,
            )
            messages.success(request, f"Regla '{nombre}' creada.")

    return redirect('finanzas:vista_distribucion')


@login_required
def editar_regla(request, regla_id):
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        return redirect('dashboard')

    regla = get_object_or_404(ReglaReparto, id=regla_id, hogar=profile.hogar)

    if request.method == 'POST':
        regla.nombre = request.POST.get('nombre', '').strip()
        regla.tipo_regla = request.POST.get('tipo_regla', 'porcentaje')
        porcentaje = request.POST.get('porcentaje', '0') or '0'
        importe_fijo = request.POST.get('importe_fijo', '0') or '0'
        regla.porcentaje = Decimal(porcentaje) if regla.tipo_regla == 'porcentaje' else Decimal('0')
        regla.importe_fijo = Decimal(importe_fijo) if regla.tipo_regla == 'fijo' else Decimal('0')
        fondo_id = request.POST.get('fondo_id')
        regla.fondo_id = int(fondo_id) if fondo_id else None
        usuario_id = request.POST.get('usuario_id')
        regla.usuario_id = int(usuario_id) if usuario_id else None
        regla.color = request.POST.get('color', '#a259ff')
        regla.save()
        messages.success(request, f"Regla '{regla.nombre}' actualizada.")

    return redirect('finanzas:vista_distribucion')


@login_required
def eliminar_regla(request, regla_id):
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        return redirect('dashboard')

    regla = get_object_or_404(ReglaReparto, id=regla_id, hogar=profile.hogar)
    nombre = regla.nombre
    regla.delete()
    messages.success(request, f"Regla '{nombre}' eliminada.")
    return redirect('finanzas:vista_distribucion')
