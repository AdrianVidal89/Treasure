from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from decimal import Decimal

from .models import CategoriaGasto, PartidaGasto, MESES_CHOICES


CATEGORIAS_PREDEFINIDAS = [
    ('fijo', 'Hipoteca / Alquiler'),
    ('fijo', 'Comunidad'),
    ('fijo', 'Seguros'),
    ('fijo', 'Suscripciones'),
    ('fijo', 'Gimnasio'),
    ('anual', 'IBI'),
    ('anual', 'Seguro coche'),
    ('anual', 'Seguro hogar'),
    ('anual', 'ITV'),
    ('anual', 'Mantenimiento vehicular'),
    ('anual', 'Basura'),
    ('variable', 'Alimentacion'),
    ('variable', 'Gasolina'),
    ('variable', 'Luz'),
    ('variable', 'Agua'),
    ('variable', 'Gas'),
    ('variable', 'Internet / Telefono'),
    ('variable', 'Ocio'),
    ('variable', 'Ropa'),
    ('variable', 'Restaurantes'),
    ('variable', 'Transporte'),
]


def _crear_categorias_predefinidas(hogar):
    """Crea categorias predefinidas si no existen para el hogar."""
    for tipo, nombre in CATEGORIAS_PREDEFINIDAS:
        CategoriaGasto.objects.get_or_create(
            hogar=hogar, nombre=nombre,
            defaults={'tipo': tipo, 'es_predefinida': True}
        )


@login_required
def listar_gastos(request):
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        messages.error(request, "Necesitas pertenecer a un hogar.")
        return redirect('dashboard')

    hogar = profile.hogar
    _crear_categorias_predefinidas(hogar)

    categorias = CategoriaGasto.objects.filter(hogar=hogar, activo=True).prefetch_related('partidas')

    gastos_fijos = []
    gastos_anuales = []
    gastos_variables = []

    total_fijos = Decimal('0')
    total_anuales = Decimal('0')
    total_provision = Decimal('0')
    total_variables = Decimal('0')

    for cat in categorias:
        partidas = cat.partidas.filter(activo=True)
        subtotal = sum(p.importe for p in partidas)
        subtotal_mensual = sum(p.importe_mensual for p in partidas)

        entrada = {
            'categoria': cat,
            'partidas': partidas,
            'subtotal': subtotal,
            'subtotal_mensual': subtotal_mensual,
        }

        if cat.tipo == 'fijo':
            gastos_fijos.append(entrada)
            total_fijos += subtotal
        elif cat.tipo == 'anual':
            gastos_anuales.append(entrada)
            total_anuales += subtotal
            total_provision += subtotal_mensual
        elif cat.tipo == 'variable':
            gastos_variables.append(entrada)
            total_variables += subtotal

    total_mensual = total_fijos + total_provision + total_variables

    return render(request, 'finanzas/gastos/listar.html', {
        'hogar': hogar,
        'gastos_fijos': gastos_fijos,
        'gastos_anuales': gastos_anuales,
        'gastos_variables': gastos_variables,
        'total_fijos': total_fijos,
        'total_anuales': total_anuales,
        'total_provision': total_provision,
        'total_variables': total_variables,
        'total_mensual': total_mensual,
    })


@login_required
def crear_partida(request):
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        return redirect('dashboard')

    hogar = profile.hogar
    categorias = CategoriaGasto.objects.filter(hogar=hogar, activo=True).order_by('tipo', 'nombre')

    if request.method == 'POST':
        categoria_id = request.POST.get('categoria_id')
        nombre = request.POST.get('nombre', '').strip()
        importe = request.POST.get('importe', '0')
        mes_pago = request.POST.get('mes_pago') or None

        if not nombre or not importe:
            messages.error(request, "Nombre e importe son obligatorios.")
        else:
            categoria = get_object_or_404(CategoriaGasto, id=categoria_id, hogar=hogar)
            PartidaGasto.objects.create(
                hogar=hogar,
                categoria=categoria,
                nombre=nombre,
                importe=Decimal(importe),
                mes_pago=int(mes_pago) if mes_pago else None,
            )
            messages.success(request, f"Gasto '{nombre}' creado.")
            return redirect('finanzas:listar_gastos')

    return render(request, 'finanzas/gastos/crear.html', {
        'categorias': categorias,
        'hogar': hogar,
        'meses': MESES_CHOICES,
    })


@login_required
def editar_partida(request, partida_id):
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        return redirect('dashboard')

    hogar = profile.hogar
    partida = get_object_or_404(PartidaGasto, id=partida_id, hogar=hogar)
    categorias = CategoriaGasto.objects.filter(hogar=hogar, activo=True).order_by('tipo', 'nombre')

    if request.method == 'POST':
        partida.categoria_id = request.POST.get('categoria_id')
        partida.nombre = request.POST.get('nombre', '').strip()
        partida.importe = Decimal(request.POST.get('importe', '0'))
        mes_pago = request.POST.get('mes_pago')
        partida.mes_pago = int(mes_pago) if mes_pago else None
        partida.save()
        messages.success(request, f"Gasto '{partida.nombre}' actualizado.")
        return redirect('finanzas:listar_gastos')

    return render(request, 'finanzas/gastos/editar.html', {
        'partida': partida,
        'categorias': categorias,
        'hogar': hogar,
        'meses': MESES_CHOICES,
    })


@login_required
def eliminar_partida(request, partida_id):
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        return redirect('dashboard')

    partida = get_object_or_404(PartidaGasto, id=partida_id, hogar=profile.hogar)
    nombre = partida.nombre
    partida.delete()
    messages.success(request, f"Gasto '{nombre}' eliminado.")
    return redirect('finanzas:listar_gastos')


@login_required
def crear_categoria(request):
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        return redirect('dashboard')

    if request.method == 'POST':
        nombre = request.POST.get('nombre', '').strip()
        tipo = request.POST.get('tipo', 'variable')
        if nombre:
            CategoriaGasto.objects.get_or_create(
                hogar=profile.hogar, nombre=nombre,
                defaults={'tipo': tipo, 'es_predefinida': False}
            )
            messages.success(request, f"Categoria '{nombre}' creada.")
        else:
            messages.error(request, "El nombre no puede estar vacio.")

    return redirect('finanzas:listar_gastos')
