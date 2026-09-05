"""Fondos: el ecosistema de cuentas del hogar.

Un fondo se define una vez y se toca poco, así que vive en Gestión, junto a
Ingresos y Gastos, y no dentro de Distribución. Aquí se define QUÉ fondos hay y
QUÉ gastos cubre cada uno; Distribución se ocupa de CÓMO se reparte el dinero
entre ellos cada mes.

La asignación de gastos se hace siempre desde el fondo ("este fondo cubre estos
gastos"), que es la dirección que eligió el usuario: un gasto lo paga un fondo,
y es el fondo el que se mira para saber si le llega.
"""

from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404

from .models import FondoFamiliar, PartidaGasto, Inversion


def _get_hogar_o_redirect(request):
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        messages.error(request, "Necesitas pertenecer a un hogar.")
        return None, None
    return profile, profile.hogar


def _propietario_de_post(request, hogar):
    """Devuelve el User propietario indicado en el POST, validando que sea
    miembro del hogar. Vacío = fondo compartido (None)."""
    pid = (request.POST.get('propietario_id') or '').strip()
    if not pid:
        return None
    return next(
        (m.user for m in hogar.miembros.select_related('user').all() if str(m.user_id) == pid),
        None,
    )


@login_required
def listar_fondos(request):
    profile, hogar = _get_hogar_o_redirect(request)
    if not hogar:
        return redirect('dashboard')

    fondos = list(
        FondoFamiliar.objects.filter(hogar=hogar, activo=True)
        .select_related('propietario')
        .order_by('orden', 'nombre')
    )

    # Gastos del hogar (los individuales los paga su responsable, no un fondo).
    gastos_hogar = list(
        PartidaGasto.objects.filter(hogar=hogar, activo=True, responsable__isnull=True)
        .select_related('categoria', 'fondo_asignado')
        .order_by('categoria__nombre', 'nombre')
    )
    gastos_por_fondo = {}
    for g in gastos_hogar:
        if g.fondo_asignado_id:
            gastos_por_fondo.setdefault(g.fondo_asignado_id, []).append(g)

    fondos_data = []
    for f in fondos:
        cubiertos = gastos_por_fondo.get(f.id, [])
        fondos_data.append({
            'fondo': f,
            'gastos': cubiertos,
            'total_gastos': sum((g.importe_mensual for g in cubiertos), Decimal('0')),
        })

    sin_asignar = [g for g in gastos_hogar if not g.fondo_asignado_id]

    # Depósitos: capital con valor automático. No son fondos, pero forman parte
    # del ecosistema y se vinculan a uno para recibir reparto.
    depositos_data = []
    depositos_total = Decimal('0')
    for dep in (Inversion.objects
                .filter(usuario__userprofile__hogar=hogar, tipo='DEPOSITO')
                .select_related('fondo')
                .prefetch_related('movimientos')
                .order_by('nombre')):
        estado = dep.deposito_estado()
        depositos_data.append({'obj': dep, **estado})
        depositos_total += estado['valor']

    return render(request, 'finanzas/fondos/listar.html', {
        'hogar': hogar,
        'fondos_data': fondos_data,
        'gastos_hogar': gastos_hogar,
        'gastos_sin_asignar': sin_asignar,
        'total_sin_asignar': sum((g.importe_mensual for g in sin_asignar), Decimal('0')),
        'miembros': hogar.miembros.select_related('user').all(),
        'depositos_data': depositos_data,
        'depositos_total': depositos_total,
    })


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
        propietario = _propietario_de_post(request, hogar)

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
                    'propietario': propietario,
                    'orden': max_orden,
                }
            )
            messages.success(request, f"Fondo '{nombre}' creado.")

    return redirect('finanzas:listar_fondos')


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
            fondo.propietario = _propietario_de_post(request, hogar)
            fondo.save()
            messages.success(request, f"Fondo '{nombre}' actualizado.")

    return redirect('finanzas:listar_fondos')


@login_required
def eliminar_fondo(request, fondo_id):
    profile, hogar = _get_hogar_o_redirect(request)
    if not hogar:
        return redirect('dashboard')

    fondo = get_object_or_404(FondoFamiliar, id=fondo_id, hogar=hogar)
    nombre = fondo.nombre
    fondo.delete()
    messages.success(request, f"Fondo '{nombre}' eliminado.")
    return redirect('finanzas:listar_fondos')


@login_required
def asignar_gastos_fondo(request, fondo_id):
    """Qué gastos del hogar cubre este fondo. La lista que llega es la
    definitiva: lo que no venga marcado se desasigna."""
    profile, hogar = _get_hogar_o_redirect(request)
    if not hogar:
        return redirect('dashboard')

    fondo = get_object_or_404(FondoFamiliar, id=fondo_id, hogar=hogar)

    if request.method == 'POST':
        ids_seleccionados = [
            int(x) for x in request.POST.getlist('partida_ids') if x.isdigit()
        ]

        PartidaGasto.objects.filter(
            hogar=hogar, fondo_asignado=fondo, activo=True, responsable__isnull=True
        ).exclude(id__in=ids_seleccionados).update(fondo_asignado=None)

        if ids_seleccionados:
            PartidaGasto.objects.filter(
                id__in=ids_seleccionados, hogar=hogar, activo=True, responsable__isnull=True
            ).update(fondo_asignado=fondo)

        messages.success(request, f"Gastos que cubre '{fondo.nombre}' actualizados.")

    return redirect('finanzas:listar_fondos')


@login_required
def desasignar_gasto_fondo(request, partida_id):
    profile, hogar = _get_hogar_o_redirect(request)
    if not hogar:
        return redirect('dashboard')

    partida = get_object_or_404(PartidaGasto, id=partida_id, hogar=hogar)
    partida.fondo_asignado = None
    partida.save(update_fields=['fondo_asignado'])
    messages.success(request, f"'{partida.nombre}' ya no lo cubre ningún fondo.")
    return redirect('finanzas:listar_fondos')
