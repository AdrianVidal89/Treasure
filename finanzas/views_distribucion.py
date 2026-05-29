import datetime
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from decimal import Decimal

from .models import (
    ReglaReparto, FondoFamiliar, SubsobreFondo,
    FuenteIngreso, AjusteIngresoMensual, PartidaGasto,
)
from .distribucion import calcular_flujos, calcular_resumen_anual

MESES = [
    (1, 'Enero'), (2, 'Febrero'), (3, 'Marzo'), (4, 'Abril'),
    (5, 'Mayo'), (6, 'Junio'), (7, 'Julio'), (8, 'Agosto'),
    (9, 'Septiembre'), (10, 'Octubre'), (11, 'Noviembre'), (12, 'Diciembre'),
]


def _get_hogar_o_redirect(request):
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        messages.error(request, "Necesitas pertenecer a un hogar.")
        return None, None
    return profile, profile.hogar


# ---------------------------------------------------------------------------
# Vista principal
# ---------------------------------------------------------------------------

@login_required
def vista_distribucion(request):
    profile, hogar = _get_hogar_o_redirect(request)
    if not hogar:
        return redirect('dashboard')

    hoy = datetime.date.today()
    try:
        mes = int(request.GET.get('mes', hoy.month))
        anio = int(request.GET.get('anio', hoy.year))
        if not (1 <= mes <= 12):
            mes = hoy.month
    except (ValueError, TypeError):
        mes, anio = hoy.month, hoy.year

    datos = calcular_flujos(hogar, mes=mes, anio=anio)
    fondos = FondoFamiliar.objects.filter(hogar=hogar, activo=True)
    reglas = ReglaReparto.objects.filter(
        hogar=hogar, activo=True
    ).select_related('fondo', 'usuario').order_by('orden')
    miembros = hogar.miembros.select_related('user').all()

    # Gastos del hogar (sin responsable) para el modal de asignación
    gastos_hogar = PartidaGasto.objects.filter(
        hogar=hogar, activo=True, responsable__isnull=True
    ).select_related('categoria', 'fondo_asignado')

    anios = [anio - 1, anio, anio + 1]

    return render(request, 'finanzas/distribucion/vista.html', {
        'hogar': hogar,
        'd': datos,
        'fondos': fondos,
        'reglas': reglas,
        'miembros': miembros,
        'gastos_hogar': gastos_hogar,
        'profile': profile,
        'meses': MESES,
        'mes_actual': mes,
        'anio_actual': anio,
        'anios': anios,
    })


# ---------------------------------------------------------------------------
# Resumen anual
# ---------------------------------------------------------------------------

@login_required
def vista_resumen_anual(request):
    profile, hogar = _get_hogar_o_redirect(request)
    if not hogar:
        return redirect('dashboard')

    hoy = datetime.date.today()
    try:
        anio = int(request.GET.get('anio', hoy.year))
    except (ValueError, TypeError):
        anio = hoy.year

    resumen = calcular_resumen_anual(hogar, anio=anio)
    anios = [anio - 1, anio, anio + 1]

    return render(request, 'finanzas/distribucion/resumen_anual.html', {
        'hogar': hogar,
        'resumen': resumen,
        'anio_actual': anio,
        'anios': anios,
        'profile': profile,
    })


# ---------------------------------------------------------------------------
# Ajuste inline de ingreso variable
# ---------------------------------------------------------------------------

@login_required
def ajustar_ingreso_mes(request):
    profile, hogar = _get_hogar_o_redirect(request)
    if not hogar:
        return redirect('dashboard')

    mes = anio = 0
    if request.method == 'POST':
        try:
            fuente_id = int(request.POST.get('fuente_id', 0))
            mes = int(request.POST.get('mes', 0))
            anio = int(request.POST.get('anio', 0))
            importe_raw = request.POST.get('importe_real', '').strip()
        except (ValueError, TypeError):
            messages.error(request, "Datos inválidos.")
            return redirect('finanzas:vista_distribucion')

        fuente = get_object_or_404(FuenteIngreso, id=fuente_id, hogar=hogar)

        if not importe_raw:
            AjusteIngresoMensual.objects.filter(
                fuente=fuente, mes=mes, **{'año': anio}
            ).delete()
            messages.success(request, f"Ajuste de '{fuente.nombre}' eliminado.")
        else:
            try:
                importe_real = Decimal(importe_raw)
            except Exception:
                messages.error(request, "Importe inválido.")
                return redirect(f"/finanzas/distribucion/?mes={mes}&anio={anio}")

            nota = request.POST.get('nota', '').strip()
            _, created = AjusteIngresoMensual.objects.update_or_create(
                fuente=fuente, mes=mes, **{'año': anio},
                defaults={'importe_real': importe_real, 'nota': nota}
            )
            accion = 'creado' if created else 'actualizado'
            messages.success(request, f"Ingreso de '{fuente.nombre}' {accion}: €{importe_real}")

    return redirect(f"/finanzas/distribucion/?mes={mes}&anio={anio}")


# ---------------------------------------------------------------------------
# CRUD fondos
# ---------------------------------------------------------------------------

@login_required
def crear_fondo(request):
    profile, hogar = _get_hogar_o_redirect(request)
    if not hogar:
        return redirect('dashboard')

    if request.method == 'POST':
        nombre = request.POST.get('nombre', '').strip()
        tipo_fondo = request.POST.get('tipo_fondo', 'comun')
        color = request.POST.get('color', '#00ff88')
        cuenta = request.POST.get('cuenta_asociada', '').strip()

        if not nombre:
            messages.error(request, "El nombre es obligatorio.")
        else:
            max_orden = FondoFamiliar.objects.filter(hogar=hogar).count()
            FondoFamiliar.objects.get_or_create(
                hogar=hogar, nombre=nombre,
                defaults={
                    'tipo_fondo': tipo_fondo,
                    'color': color,
                    'cuenta_asociada': cuenta,
                    'orden': max_orden,
                }
            )
            messages.success(request, f"Fondo '{nombre}' creado.")

    return redirect('finanzas:vista_distribucion')


@login_required
def editar_fondo(request, fondo_id):
    profile, hogar = _get_hogar_o_redirect(request)
    if not hogar:
        return redirect('dashboard')

    fondo = get_object_or_404(FondoFamiliar, id=fondo_id, hogar=hogar)

    if request.method == 'POST':
        nombre = request.POST.get('nombre', '').strip()
        tipo_fondo = request.POST.get('tipo_fondo', 'comun')
        color = request.POST.get('color', '#00ff88')
        cuenta = request.POST.get('cuenta_asociada', '').strip()

        if not nombre:
            messages.error(request, "El nombre es obligatorio.")
        else:
            fondo.nombre = nombre
            fondo.tipo_fondo = tipo_fondo
            fondo.color = color
            fondo.cuenta_asociada = cuenta
            fondo.save()
            messages.success(request, f"Fondo '{nombre}' actualizado.")

    return redirect('finanzas:vista_distribucion')


@login_required
def eliminar_fondo(request, fondo_id):
    profile, hogar = _get_hogar_o_redirect(request)
    if not hogar:
        return redirect('dashboard')

    fondo = get_object_or_404(FondoFamiliar, id=fondo_id, hogar=hogar)
    nombre = fondo.nombre
    fondo.delete()
    messages.success(request, f"Fondo '{nombre}' eliminado.")
    return redirect('finanzas:vista_distribucion')


# ---------------------------------------------------------------------------
# Asignar gastos del hogar a un fondo común
# ---------------------------------------------------------------------------

@login_required
def asignar_gastos_fondo(request, fondo_id):
    """
    POST con lista de partida_ids → se asignan a este fondo.
    Las partidas no incluidas se desasignan de este fondo.
    """
    profile, hogar = _get_hogar_o_redirect(request)
    if not hogar:
        return redirect('dashboard')

    fondo = get_object_or_404(FondoFamiliar, id=fondo_id, hogar=hogar)

    if request.method == 'POST':
        ids_seleccionados = request.POST.getlist('partida_ids')
        ids_seleccionados = [int(x) for x in ids_seleccionados if x.isdigit()]

        # Desasignar las que ya no están seleccionadas
        PartidaGasto.objects.filter(
            hogar=hogar, fondo_asignado=fondo, activo=True, responsable__isnull=True
        ).exclude(id__in=ids_seleccionados).update(fondo_asignado=None)

        # Asignar las seleccionadas
        if ids_seleccionados:
            PartidaGasto.objects.filter(
                id__in=ids_seleccionados, hogar=hogar, activo=True, responsable__isnull=True
            ).update(fondo_asignado=fondo)

        messages.success(request, f"Gastos asignados a '{fondo.nombre}' actualizados.")

    return redirect('finanzas:vista_distribucion')


# ---------------------------------------------------------------------------
# CRUD reglas
# ---------------------------------------------------------------------------

@login_required
def crear_regla(request):
    profile, hogar = _get_hogar_o_redirect(request)
    if not hogar:
        return redirect('dashboard')

    if request.method == 'POST':
        nombre = request.POST.get('nombre', '').strip()
        tipo = request.POST.get('tipo_regla', 'porcentaje')
        fondo_id = request.POST.get('fondo_id') or None
        usuario_id = request.POST.get('usuario_id') or None
        color = request.POST.get('color', '#00ff88')

        try:
            porcentaje = Decimal(request.POST.get('porcentaje', '0') or '0')
            importe_fijo = Decimal(request.POST.get('importe_fijo', '0') or '0')
        except Exception:
            messages.error(request, "Importe inválido.")
            return redirect('finanzas:vista_distribucion')

        if not nombre:
            messages.error(request, "El nombre es obligatorio.")
        else:
            fondo = FondoFamiliar.objects.filter(id=fondo_id, hogar=hogar).first() if fondo_id else None
            max_orden = ReglaReparto.objects.filter(hogar=hogar).count()
            ReglaReparto.objects.create(
                hogar=hogar, nombre=nombre, tipo_regla=tipo,
                fondo=fondo, usuario_id=usuario_id,
                porcentaje=porcentaje, importe_fijo=importe_fijo,
                color=color, orden=max_orden,
            )
            messages.success(request, f"Regla '{nombre}' creada.")

    return redirect('finanzas:vista_distribucion')


@login_required
def editar_regla(request, regla_id):
    profile, hogar = _get_hogar_o_redirect(request)
    if not hogar:
        return redirect('dashboard')

    regla = get_object_or_404(ReglaReparto, id=regla_id, hogar=hogar)

    if request.method == 'POST':
        nombre = request.POST.get('nombre', '').strip()
        tipo = request.POST.get('tipo_regla', 'porcentaje')
        fondo_id = request.POST.get('fondo_id') or None
        usuario_id = request.POST.get('usuario_id') or None
        color = request.POST.get('color', '#00ff88')

        try:
            porcentaje = Decimal(request.POST.get('porcentaje', '0') or '0')
            importe_fijo = Decimal(request.POST.get('importe_fijo', '0') or '0')
        except Exception:
            messages.error(request, "Importe inválido.")
            return redirect('finanzas:vista_distribucion')

        if not nombre:
            messages.error(request, "El nombre es obligatorio.")
        else:
            regla.nombre = nombre
            regla.tipo_regla = tipo
            regla.fondo = FondoFamiliar.objects.filter(id=fondo_id, hogar=hogar).first() if fondo_id else None
            regla.usuario_id = usuario_id if usuario_id else None
            regla.porcentaje = porcentaje
            regla.importe_fijo = importe_fijo
            regla.color = color
            regla.save()
            messages.success(request, f"Regla '{nombre}' actualizada.")

    return redirect('finanzas:vista_distribucion')


@login_required
def eliminar_regla(request, regla_id):
    profile, hogar = _get_hogar_o_redirect(request)
    if not hogar:
        return redirect('dashboard')

    regla = get_object_or_404(ReglaReparto, id=regla_id, hogar=hogar)
    nombre = regla.nombre
    regla.delete()
    messages.success(request, f"Regla '{nombre}' eliminada.")
    return redirect('finanzas:vista_distribucion')


# ---------------------------------------------------------------------------
# CRUD subsobres
# ---------------------------------------------------------------------------

@login_required
def crear_subsobres(request, fondo_id):
    profile, hogar = _get_hogar_o_redirect(request)
    if not hogar:
        return redirect('dashboard')

    fondo = get_object_or_404(FondoFamiliar, id=fondo_id, hogar=hogar)

    if request.method == 'POST':
        nombre = request.POST.get('nombre', '').strip()
        tipo = request.POST.get('tipo', 'libre')
        fondo_destino_id = request.POST.get('fondo_destino_id') or None

        try:
            importe_manual = Decimal(request.POST.get('importe_manual', '0') or '0')
        except Exception:
            messages.error(request, "Importe inválido.")
            return redirect('finanzas:vista_distribucion')

        if not nombre:
            messages.error(request, "El nombre es obligatorio.")
        else:
            fondo_destino = FondoFamiliar.objects.filter(
                id=fondo_destino_id, hogar=hogar
            ).first() if fondo_destino_id else None

            max_orden = SubsobreFondo.objects.filter(fondo=fondo).count()
            SubsobreFondo.objects.create(
                fondo=fondo, nombre=nombre, tipo=tipo,
                importe_manual=importe_manual if importe_manual > 0 else None,
                fondo_destino=fondo_destino, orden=max_orden,
            )
            messages.success(request, f"Distribución interna '{nombre}' añadida.")

    return redirect('finanzas:vista_distribucion')


@login_required
def eliminar_subsobres(request, subsobres_id):
    profile, hogar = _get_hogar_o_redirect(request)
    if not hogar:
        return redirect('dashboard')

    ss = get_object_or_404(SubsobreFondo, id=subsobres_id, fondo__hogar=hogar)
    nombre = ss.nombre
    ss.delete()
    messages.success(request, f"Distribución interna '{nombre}' eliminada.")
    return redirect('finanzas:vista_distribucion')
