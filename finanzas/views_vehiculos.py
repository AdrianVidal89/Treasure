"""Vehículos: declararlos y saber lo que cuesta mantenerlos.

El módulo existe por una pregunta que el presupuesto por categorías no
responde: el seguro, la ITV, el taller y la gasolina caen en bloques distintos
y, con dos coches, ni siquiera se distinguen entre sí. Imputando el gasto al
vehículo, la pregunta «¿cuánto me cuesta el coche al mes?» tiene respuesta —y
dos veces: la declarada y la real.
"""

from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from . import costes_activo
from .parsing import parse_decimal
from .models import PartidaGasto, Vehiculo


def _get_hogar(request):
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        return None, None
    return profile, profile.hogar


def _anio_elegido(request, fichas_anios):
    """Año que se está mirando: el de la URL, o el último con datos, o el actual."""
    try:
        return int(request.GET.get('anio'))
    except (TypeError, ValueError):
        pass
    return fichas_anios[0] if fichas_anios else date.today().year


@login_required
def listar_vehiculos(request):
    profile, hogar = _get_hogar(request)
    if not hogar:
        messages.error(request, "Necesitas pertenecer a un hogar.")
        return redirect('dashboard')

    vehiculos = list(Vehiculo.objects.filter(hogar=hogar, activo=True))
    anios = costes_activo.costes(vehiculos[0], date.today().year)['anios_con_datos'] if vehiculos else []
    anio = _anio_elegido(request, anios or [date.today().year])
    datos = costes_activo.resumen(vehiculos, anio)

    # Años que ofrecer en el selector: los que tengan gasto imputado en algún
    # vehículo, más el actual.
    anios_disponibles = sorted(
        {a for f in datos['fichas'] for a in f['anios_con_datos']} | {date.today().year, anio},
        reverse=True,
    )

    return render(request, 'finanzas/vehiculos/listar.html', {
        'fichas': datos['fichas'],
        'total_teorico_mensual': datos['teorico_mensual'],
        'total_teorico_anual': datos['teorico_anual'],
        'total_real_anual': datos['real_anual'],
        'total_ritmo_mensual': datos['ritmo_mensual'],
        'meses_transcurridos': datos['meses_transcurridos'],
        'anio': anio,
        'anios_disponibles': anios_disponibles,
        'tipos': Vehiculo.TIPO_CHOICES,
        'archivados': Vehiculo.objects.filter(hogar=hogar, activo=False),
    })


@login_required
def detalle_vehiculo(request, vehiculo_id):
    profile, hogar = _get_hogar(request)
    if not hogar:
        return redirect('dashboard')

    vehiculo = get_object_or_404(Vehiculo, id=vehiculo_id, hogar=hogar)
    ficha = costes_activo.costes(vehiculo, _anio_elegido(request, []))
    anios_disponibles = sorted(
        set(ficha['anios_con_datos']) | {date.today().year, ficha['anio']}, reverse=True,
    )

    # La depreciación no pasa por el banco, pero es dinero: un coche de 60 €/mes
    # de mantenimiento que se deprecia 2.000 €/año no cuesta 60 €/mes.
    depreciacion = vehiculo.depreciacion_anual or Decimal('0')

    return render(request, 'finanzas/vehiculos/detalle.html', {
        'v': vehiculo,
        'f': ficha,
        'coste_con_depreciacion': ficha['real_anual'] + depreciacion,
        'anios_disponibles': anios_disponibles,
        'tipos': Vehiculo.TIPO_CHOICES,
        'sin_imputar': PartidaGasto.objects.filter(
            hogar=hogar, activo=True, vehiculo__isnull=True, propiedad__isnull=True,
        ).select_related('categoria'),
    })


@login_required
def guardar_vehiculo(request, vehiculo_id=None):
    """Alta y edición comparten formulario: los campos son los mismos y
    duplicar la validación solo garantiza que se desincronicen."""
    profile, hogar = _get_hogar(request)
    if not hogar:
        return redirect('dashboard')
    if request.method != 'POST':
        return redirect('finanzas:listar_vehiculos')

    vehiculo = (
        get_object_or_404(Vehiculo, id=vehiculo_id, hogar=hogar)
        if vehiculo_id else Vehiculo(hogar=hogar)
    )
    nombre = (request.POST.get('nombre') or '').strip()
    if not nombre:
        messages.error(request, "El nombre no puede estar vacío.")
        return redirect('finanzas:listar_vehiculos')

    vehiculo.nombre = nombre[:120]
    vehiculo.tipo = request.POST.get('tipo', 'coche')
    if vehiculo.tipo not in dict(Vehiculo.TIPO_CHOICES):
        vehiculo.tipo = 'coche'
    vehiculo.marca_modelo = (request.POST.get('marca_modelo') or '').strip()[:120]
    vehiculo.matricula = (request.POST.get('matricula') or '').strip()[:20]
    vehiculo.fecha_compra = request.POST.get('fecha_compra') or None
    vehiculo.precio_compra = parse_decimal(request.POST.get('precio_compra'))
    vehiculo.valor_actual = parse_decimal(request.POST.get('valor_actual'))
    vehiculo.color = (request.POST.get('color') or '#2c5f7a')[:7]
    vehiculo.notas = (request.POST.get('notas') or '').strip()[:300]
    vehiculo.save()

    messages.success(request, f"Vehículo «{vehiculo.nombre}» {'actualizado' if vehiculo_id else 'creado'}.")
    return redirect('finanzas:detalle_vehiculo', vehiculo_id=vehiculo.id)


@login_required
def archivar_vehiculo(request, vehiculo_id):
    """Archivar en vez de borrar: el histórico de gasto del coche que vendiste
    sigue siendo tu histórico."""
    profile, hogar = _get_hogar(request)
    if not hogar:
        return redirect('dashboard')

    vehiculo = get_object_or_404(Vehiculo, id=vehiculo_id, hogar=hogar)
    if request.method == 'POST':
        vehiculo.activo = not vehiculo.activo
        vehiculo.save(update_fields=['activo'])
        messages.success(
            request,
            f"«{vehiculo.nombre}» {'reactivado' if vehiculo.activo else 'archivado'}.",
        )
    return redirect('finanzas:listar_vehiculos')


@login_required
def eliminar_vehiculo(request, vehiculo_id):
    profile, hogar = _get_hogar(request)
    if not hogar:
        return redirect('dashboard')

    vehiculo = get_object_or_404(Vehiculo, id=vehiculo_id, hogar=hogar)
    if request.method == 'POST':
        nombre = vehiculo.nombre
        # Los gastos imputados no se borran: se quedan sin activo (SET_NULL).
        vehiculo.delete()
        messages.success(
            request,
            f"Vehículo «{nombre}» eliminado. Sus gastos siguen ahí, ya sin vehículo asignado.",
        )
    return redirect('finanzas:listar_vehiculos')


@login_required
def imputar_partida(request):
    """Asigna (o desasigna) un gasto declarado a un activo.

    Se hace desde la ficha del activo porque es donde uno se pregunta «¿qué
    gastos de los que tengo declarados son de este coche?»."""
    profile, hogar = _get_hogar(request)
    if not hogar:
        return redirect('dashboard')
    if request.method != 'POST':
        return redirect('finanzas:listar_vehiculos')

    partida = get_object_or_404(
        PartidaGasto, id=request.POST.get('partida_id'), hogar=hogar,
    )
    activo = costes_activo.resolver(hogar, request.POST.get('activo') or '')
    costes_activo.asignar(partida, activo)
    partida.save(update_fields=['vehiculo', 'propiedad'])

    if activo:
        messages.success(request, f"«{partida.nombre}» ahora cuenta como gasto de «{activo}».")
    else:
        messages.success(request, f"«{partida.nombre}» ya no está imputado a ningún activo.")

    destino = request.POST.get('volver_a') or ''
    if destino.startswith('/') and not destino.startswith('//'):
        return redirect(destino)
    return redirect('finanzas:listar_vehiculos')
