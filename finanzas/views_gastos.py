from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from decimal import Decimal

from .models import CategoriaGasto, PartidaGasto, MESES_CHOICES, PERIODICIDAD_GASTO_CHOICES


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
    total_anuales_anual = Decimal('0')
    total_provision = Decimal('0')
    total_variables = Decimal('0')

    for cat in categorias:
        partidas = cat.partidas.filter(activo=True)
        if not partidas.exists():
            continue

        subtotal_mensual = sum(p.importe_mensual for p in partidas)
        subtotal_anual = sum(p.importe_anual for p in partidas)
        num_partidas = partidas.count()

        entrada = {
            'categoria': cat,
            'partidas': partidas,
            'subtotal_mensual': subtotal_mensual,
            'subtotal_anual': subtotal_anual,
            'num_partidas': num_partidas,
        }

        if cat.tipo == 'fijo':
            gastos_fijos.append(entrada)
            total_fijos += subtotal_mensual
        elif cat.tipo == 'anual':
            gastos_anuales.append(entrada)
            total_anuales_anual += subtotal_anual
            total_provision += subtotal_mensual
        elif cat.tipo == 'variable':
            gastos_variables.append(entrada)
            total_variables += subtotal_mensual

    total_mensual = total_fijos + total_provision + total_variables
    total_anual_todo = (total_fijos + total_variables) * 12 + total_anuales_anual

    # Cual card abrir automaticamente (tras editar/eliminar)
    open_cat = request.GET.get('open', '')

    return render(request, 'finanzas/gastos/listar.html', {
        'hogar': hogar,
        'gastos_fijos': gastos_fijos,
        'gastos_anuales': gastos_anuales,
        'gastos_variables': gastos_variables,
        'total_fijos': total_fijos,
        'total_anuales_anual': total_anuales_anual,
        'total_provision': total_provision,
        'total_variables': total_variables,
        'total_mensual': total_mensual,
        'total_anual_todo': total_anual_todo,
        'open_cat': open_cat,
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
        periodicidad = request.POST.get('periodicidad', 'mensual')
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
                periodicidad=periodicidad,
                mes_pago=int(mes_pago) if mes_pago else None,
            )
            messages.success(request, f"Gasto '{nombre}' creado.")
            return redirect(f'/finanzas/gastos/?open={categoria.id}')

    return render(request, 'finanzas/gastos/crear.html', {
        'categorias': categorias,
        'hogar': hogar,
        'meses': MESES_CHOICES,
        'periodicidades': PERIODICIDAD_GASTO_CHOICES,
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
        partida.periodicidad = request.POST.get('periodicidad', 'mensual')
        mes_pago = request.POST.get('mes_pago')
        partida.mes_pago = int(mes_pago) if mes_pago else None
        partida.save()
        messages.success(request, f"Gasto '{partida.nombre}' actualizado.")
        return redirect(f'/finanzas/gastos/?open={partida.categoria_id}')

    return render(request, 'finanzas/gastos/editar.html', {
        'partida': partida,
        'categorias': categorias,
        'hogar': hogar,
        'meses': MESES_CHOICES,
        'periodicidades': PERIODICIDAD_GASTO_CHOICES,
    })


@login_required
def eliminar_partida(request, partida_id):
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        return redirect('dashboard')

    partida = get_object_or_404(PartidaGasto, id=partida_id, hogar=profile.hogar)
    nombre = partida.nombre
    cat_id = partida.categoria_id
    partida.delete()
    messages.success(request, f"Gasto '{nombre}' eliminado.")
    return redirect(f'/finanzas/gastos/?open={cat_id}')


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
