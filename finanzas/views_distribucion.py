"""
finanzas/views_distribucion.py v4
"""

import datetime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from .distribucion import calcular_flujos, neto_estimado_de_base, info_extras_usuario
from .models import (
    AjusteIngresoMensual,
    FondoFamiliar,
    FuenteIngreso,
    IngresoExtraordinario,
    PartidaGasto,
    ReglaReparto,
    SubsobreFondo,
)

MESES_NOMBRES = [
    '', 'Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio',
    'Julio', 'Agosto', 'Septiembre', 'Octubre', 'Noviembre', 'Diciembre',
]


def _get_hogar_or_redirect(request):
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        messages.error(request, "Necesitas pertenecer a un hogar.")
        return None, None
    return profile, profile.hogar


def _parse_año_mes(request):
    hoy = datetime.date.today()
    try:
        año = int(request.GET.get('año', request.POST.get('año', hoy.year)))
        mes = int(request.GET.get('mes', request.POST.get('mes', hoy.month)))
    except (ValueError, TypeError):
        año, mes = hoy.year, hoy.month
    return año, max(1, min(12, mes))


# ─────────────────────────────────────────────────────────────────────────────
# VISTA PRINCIPAL
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def vista_distribucion(request):
    profile, hogar = _get_hogar_or_redirect(request)
    if not hogar:
        return redirect('dashboard')

    año, mes = _parse_año_mes(request)
    datos = calcular_flujos(hogar, año=año, mes=mes)
    fondos = FondoFamiliar.objects.filter(hogar=hogar, activo=True)
    reglas = ReglaReparto.objects.filter(hogar=hogar, activo=True).select_related('fondo', 'usuario')
    miembros = hogar.miembros.select_related('user').all()
    meses_nav = [{'num': i, 'nombre': MESES_NOMBRES[i]} for i in range(1, 13)]

    hay_variables = FuenteIngreso.objects.filter(
        hogar=hogar, tipo='variable', activo=True
    ).exists()

    partidas_hogar = PartidaGasto.objects.filter(
        hogar=hogar, activo=True
    ).select_related('categoria').order_by('categoria__nombre', 'nombre')

    return render(request, 'finanzas/distribucion/vista.html', {
        'hogar': hogar,
        'd': datos,
        'fondos': fondos,
        'reglas': reglas,
        'miembros': miembros,
        'profile': profile,
        'año': año,
        'mes': mes,
        'mes_nombre': MESES_NOMBRES[mes],
        'meses_nav': meses_nav,
        'hay_variables': hay_variables,
        'partidas_hogar': partidas_hogar,
    })


# ─────────────────────────────────────────────────────────────────────────────
# AJAX: Info extras disponibles por usuario (para modal regla anual)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def ajax_extras_usuario(request):
    """
    GET ?usuario_id=X&año=2026
    Devuelve JSON con las pagas extras y disponible de ese usuario.
    """
    profile, hogar = _get_hogar_or_redirect(request)
    if not hogar:
        return JsonResponse({'error': 'Sin hogar'}, status=400)

    usuario_id = request.GET.get('usuario_id')
    año = int(request.GET.get('año', datetime.date.today().year))

    if not usuario_id:
        return JsonResponse({'extras': [], 'total': 0})

    from django.contrib.auth.models import User
    user = get_object_or_404(User, id=usuario_id)
    data = info_extras_usuario(hogar, user, año)

    return JsonResponse({
        'extras': data['detalle'],
        'total_anual': float(data['total_anual']),
        'total_asignado': float(data['total_asignado']),
        'disponible': float(data['disponible']),
    })


# ─────────────────────────────────────────────────────────────────────────────
# AJUSTE MENSUAL DE INGRESO VARIABLE
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def ajuste_ingreso_mensual(request):
    profile, hogar = _get_hogar_or_redirect(request)
    if not hogar:
        return redirect('dashboard')

    año, mes = _parse_año_mes(request)

    fuentes_variables = FuenteIngreso.objects.filter(
        hogar=hogar, tipo='variable', activo=True
    ).select_related('usuario').order_by('usuario__first_name', 'nombre')

    if request.method == 'POST':
        errores = []
        for fuente in fuentes_variables:
            campo_importe = f'importe_{fuente.id}'
            campo_nota = f'nota_{fuente.id}'
            valor_raw = request.POST.get(campo_importe, '').strip()
            nota = request.POST.get(campo_nota, '').strip()

            if not valor_raw:
                AjusteIngresoMensual.objects.filter(
                    fuente=fuente, año=año, mes=mes
                ).delete()
                continue

            try:
                importe = Decimal(valor_raw.replace(',', '.'))
                if importe < 0:
                    raise ValueError
            except (InvalidOperation, ValueError):
                errores.append(f"Importe inválido para {fuente.nombre}.")
                continue

            AjusteIngresoMensual.objects.update_or_create(
                fuente=fuente, año=año, mes=mes,
                defaults={'importe_real': importe, 'nota': nota},
            )

        if errores:
            for e in errores:
                messages.error(request, e)
        else:
            messages.success(request, f"Ajustes de {MESES_NOMBRES[mes]} {año} guardados.")

        return redirect(f"{reverse('finanzas:vista_distribucion')}?año={año}&mes={mes}")

    overrides = {
        aj.fuente_id: aj
        for aj in AjusteIngresoMensual.objects.filter(
            fuente__in=fuentes_variables, año=año, mes=mes
        )
    }

    fuentes_ctx = []
    for f in fuentes_variables:
        override = overrides.get(f.id)
        neto_base = neto_estimado_de_base(f)
        fuentes_ctx.append({
            'fuente': f,
            'override': override,
            'importe_actual': override.importe_real if override else None,
            'nota_actual': override.nota if override else '',
            'base_estimada_neto': neto_base,
            'base_bruta': f.importe_mensual_base,
            'nombre_usuario': f.usuario.first_name or f.usuario.username,
        })

    return render(request, 'finanzas/distribucion/ajuste_variable.html', {
        'fuentes': fuentes_ctx,
        'año': año, 'mes': mes,
        'mes_nombre': MESES_NOMBRES[mes],
        'hogar': hogar,
    })


# ─────────────────────────────────────────────────────────────────────────────
# INGRESO EXTRAORDINARIO
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def crear_ingreso_extraordinario(request):
    profile, hogar = _get_hogar_or_redirect(request)
    if not hogar:
        return redirect('dashboard')

    año, mes = _parse_año_mes(request)

    if request.method == 'POST':
        concepto = request.POST.get('concepto', '').strip()
        importe_raw = request.POST.get('importe', '').strip()
        usuario_id = request.POST.get('usuario_id')
        fondo_id = request.POST.get('fondo_destino_id')
        nota = request.POST.get('nota', '').strip()
        post_año = request.POST.get('año', año)
        post_mes = request.POST.get('mes', mes)

        if not concepto or not importe_raw:
            messages.error(request, "Concepto e importe son obligatorios.")
            return redirect(f"{reverse('finanzas:vista_distribucion')}?año={año}&mes={mes}")

        try:
            importe = Decimal(importe_raw.replace(',', '.'))
        except InvalidOperation:
            messages.error(request, "Importe inválido.")
            return redirect(f"{reverse('finanzas:vista_distribucion')}?año={año}&mes={mes}")

        IngresoExtraordinario.objects.create(
            hogar=hogar,
            usuario_id=int(usuario_id) if usuario_id else request.user.id,
            concepto=concepto,
            importe=importe,
            es_neto=True,
            año=int(post_año),
            mes=int(post_mes),
            fondo_destino_id=int(fondo_id) if fondo_id else None,
            nota=nota,
        )
        messages.success(request, f"Ingreso extraordinario '{concepto}' registrado.")

    return redirect(f"{reverse('finanzas:vista_distribucion')}?año={año}&mes={mes}")


@login_required
def eliminar_ingreso_extraordinario(request, extra_id):
    profile, hogar = _get_hogar_or_redirect(request)
    if not hogar:
        return redirect('dashboard')

    extra = get_object_or_404(IngresoExtraordinario, id=extra_id, hogar=hogar)
    año, mes = extra.año, extra.mes
    extra.delete()
    messages.success(request, "Ingreso extraordinario eliminado.")
    return redirect(f"{reverse('finanzas:vista_distribucion')}?año={año}&mes={mes}")


# ─────────────────────────────────────────────────────────────────────────────
# FONDOS
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def crear_fondo(request):
    profile, hogar = _get_hogar_or_redirect(request)
    if not hogar:
        return redirect('dashboard')

    if request.method == 'POST':
        nombre = request.POST.get('nombre', '').strip()
        modo = request.POST.get('modo_aportacion', 'proporcional')
        tipo_fondo = request.POST.get('tipo_fondo', 'comun')
        color = request.POST.get('color', '#a259ff')
        cuenta = request.POST.get('cuenta_asociada', '').strip()

        if not nombre:
            messages.error(request, "El nombre es obligatorio.")
        else:
            max_orden = FondoFamiliar.objects.filter(hogar=hogar).count()
            FondoFamiliar.objects.get_or_create(
                hogar=hogar, nombre=nombre,
                defaults={
                    'modo_aportacion': modo,
                    'tipo_fondo': tipo_fondo,
                    'color': color,
                    'cuenta_asociada': cuenta,
                    'orden': max_orden,
                },
            )
            messages.success(request, f"Fondo '{nombre}' creado.")

    return redirect('finanzas:vista_distribucion')


@login_required
def eliminar_fondo(request, fondo_id):
    profile, hogar = _get_hogar_or_redirect(request)
    if not hogar:
        return redirect('dashboard')

    fondo = get_object_or_404(FondoFamiliar, id=fondo_id, hogar=hogar)
    nombre = fondo.nombre
    fondo.delete()
    messages.success(request, f"Fondo '{nombre}' eliminado.")
    return redirect('finanzas:vista_distribucion')


# ─────────────────────────────────────────────────────────────────────────────
# SUBSOBRES
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def crear_subsobre(request, fondo_id):
    profile, hogar = _get_hogar_or_redirect(request)
    if not hogar:
        return redirect('dashboard')

    fondo = get_object_or_404(FondoFamiliar, id=fondo_id, hogar=hogar)

    if request.method == 'POST':
        nombre = request.POST.get('nombre', '').strip()
        tipo = request.POST.get('tipo', 'discrecional')
        importe_raw = request.POST.get('importe_manual', '').strip()
        partidas_ids = request.POST.getlist('partidas_ids')
        fondo_destino_id = request.POST.get('fondo_destino_id') or None

        if not nombre:
            messages.error(request, "El nombre del sobre es obligatorio.")
            return redirect('finanzas:vista_distribucion')

        importe_manual = None
        if importe_raw:
            try:
                importe_manual = Decimal(importe_raw.replace(',', '.'))
            except InvalidOperation:
                messages.error(request, "Importe inválido.")
                return redirect('finanzas:vista_distribucion')

        max_orden = SubsobreFondo.objects.filter(fondo=fondo).count()
        subsobre = SubsobreFondo.objects.create(
            fondo=fondo,
            nombre=nombre,
            tipo=tipo,
            importe_manual=importe_manual,
            fondo_destino_id=int(fondo_destino_id) if fondo_destino_id else None,
            orden=max_orden,
        )

        if partidas_ids and not fondo_destino_id:
            partidas = PartidaGasto.objects.filter(id__in=partidas_ids, hogar=hogar)
            subsobre.partidas_vinculadas.set(partidas)

        messages.success(request, f"Sobre '{nombre}' añadido a '{fondo.nombre}'.")

    return redirect('finanzas:vista_distribucion')


@login_required
def eliminar_subsobre(request, subsobre_id):
    profile, hogar = _get_hogar_or_redirect(request)
    if not hogar:
        return redirect('dashboard')

    subsobre = get_object_or_404(SubsobreFondo, id=subsobre_id, fondo__hogar=hogar)
    nombre = subsobre.nombre
    subsobre.delete()
    messages.success(request, f"Sobre '{nombre}' eliminado.")
    return redirect('finanzas:vista_distribucion')


# ─────────────────────────────────────────────────────────────────────────────
# REGLAS DE REPARTO
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def crear_regla(request):
    profile, hogar = _get_hogar_or_redirect(request)
    if not hogar:
        return redirect('dashboard')

    if request.method == 'POST':
        nombre = request.POST.get('nombre', '').strip()
        usuario_id = request.POST.get('usuario_id') or None
        tipo_regla = request.POST.get('tipo_regla', 'porcentaje')
        periodicidad_regla = request.POST.get('periodicidad_regla', 'mensual')
        porcentaje_raw = request.POST.get('porcentaje', '0')
        importe_fijo_raw = request.POST.get('importe_fijo', '0')
        fondo_id = request.POST.get('fondo_id') or None
        color = request.POST.get('color', '#a259ff')

        try:
            porcentaje = Decimal(porcentaje_raw) if tipo_regla == 'porcentaje' else Decimal('0')
            importe_fijo = Decimal(importe_fijo_raw) if tipo_regla == 'fijo' else Decimal('0')
        except InvalidOperation:
            messages.error(request, "Importe o porcentaje inválido.")
            return redirect('finanzas:vista_distribucion')

        ReglaReparto.objects.create(
            hogar=hogar,
            nombre=nombre,
            usuario_id=int(usuario_id) if usuario_id else None,
            tipo_regla=tipo_regla,
            periodicidad_regla=periodicidad_regla,
            porcentaje=porcentaje,
            importe_fijo=importe_fijo,
            fondo_id=int(fondo_id) if fondo_id else None,
            color=color,
        )
        messages.success(request, f"Regla '{nombre}' creada.")

    return redirect('finanzas:vista_distribucion')


@login_required
def editar_regla(request, regla_id):
    profile, hogar = _get_hogar_or_redirect(request)
    if not hogar:
        return redirect('dashboard')

    regla = get_object_or_404(ReglaReparto, id=regla_id, hogar=hogar)

    if request.method == 'POST':
        regla.nombre = request.POST.get('nombre', regla.nombre).strip()
        usuario_id = request.POST.get('usuario_id') or None
        regla.usuario_id = int(usuario_id) if usuario_id else None
        regla.tipo_regla = request.POST.get('tipo_regla', regla.tipo_regla)
        regla.periodicidad_regla = request.POST.get('periodicidad_regla', regla.periodicidad_regla)
        fondo_id = request.POST.get('fondo_id') or None
        regla.fondo_id = int(fondo_id) if fondo_id else None
        regla.color = request.POST.get('color', regla.color)

        try:
            if regla.tipo_regla == 'porcentaje':
                regla.porcentaje = Decimal(request.POST.get('porcentaje', '0'))
            else:
                regla.importe_fijo = Decimal(request.POST.get('importe_fijo', '0'))
        except InvalidOperation:
            messages.error(request, "Importe inválido.")
            return redirect('finanzas:vista_distribucion')

        regla.save()
        messages.success(request, f"Regla '{regla.nombre}' actualizada.")

    return redirect('finanzas:vista_distribucion')


@login_required
def eliminar_regla(request, regla_id):
    profile, hogar = _get_hogar_or_redirect(request)
    if not hogar:
        return redirect('dashboard')

    regla = get_object_or_404(ReglaReparto, id=regla_id, hogar=hogar)
    nombre = regla.nombre
    regla.delete()
    messages.success(request, f"Regla '{nombre}' eliminada.")
    return redirect('finanzas:vista_distribucion')
