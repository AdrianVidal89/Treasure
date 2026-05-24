from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from decimal import Decimal

from .models import ReglaReparto
from .distribucion import calcular_distribucion


COLORES_PREDEFINIDOS = [
    '#00ff88',  # verde neon
    '#00d1ff',  # cyan
    '#a259ff',  # morado
    '#ffaa00',  # naranja
    '#ff4d4d',  # rojo
    '#ff00cc',  # rosa
    '#4dff4d',  # lima
    '#ffd700',  # oro
]


@login_required
def vista_distribucion(request):
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        messages.error(request, "Necesitas pertenecer a un hogar.")
        return redirect('dashboard')

    hogar = profile.hogar
    datos = calcular_distribucion(hogar)

    return render(request, 'finanzas/distribucion/vista.html', {
        'hogar': hogar,
        'd': datos,
        'profile': profile,
    })


@login_required
def crear_regla(request):
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        return redirect('dashboard')

    hogar = profile.hogar

    if request.method == 'POST':
        nombre = request.POST.get('nombre', '').strip()
        porcentaje = request.POST.get('porcentaje', '0')
        color = request.POST.get('color', '#a259ff')

        if not nombre:
            messages.error(request, "El nombre es obligatorio.")
        else:
            # Calcular orden automatico
            max_orden = ReglaReparto.objects.filter(hogar=hogar).count()
            ReglaReparto.objects.create(
                hogar=hogar,
                nombre=nombre,
                porcentaje=Decimal(porcentaje),
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
        regla.porcentaje = Decimal(request.POST.get('porcentaje', '0'))
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
