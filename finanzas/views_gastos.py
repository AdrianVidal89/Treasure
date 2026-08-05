from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from decimal import Decimal

from .models import (
    CategoriaGasto, PartidaGasto, MESES_CHOICES, PERIODICIDAD_GASTO_CHOICES,
    TIPOS_GASTO,
)


CATEGORIAS_PREDEFINIDAS = [
    ('fijo', 'Hipoteca / Alquiler'),
    ('fijo', 'Comunidad'),
    ('fijo', 'Seguros'),
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
    ('variable', 'Transporte'),
    ('variable', 'Salud / Farmacia'),
    ('variable', 'Hogar / Bricolaje'),
    ('variable', 'Impuestos y comisiones'),
    # Discrecional: gasto prescindible. Se separa de Variables para poder ver
    # de un vistazo cuánto del mes es recortable.
    ('discrecional', 'Ocio'),
    ('discrecional', 'Restaurantes'),
    ('discrecional', 'Ropa'),
    ('discrecional', 'Suscripciones'),
    ('discrecional', 'Tecnologia / Software'),
    # Ingresos: no son gasto, pero necesitan categoría para que los movimientos
    # positivos del extracto no queden sueltos.
    ('ingreso', 'Nomina'),
    ('ingreso', 'Intereses'),
    ('ingreso', 'Devoluciones'),
    ('ingreso', 'Otros ingresos'),
    # Traspasos entre cuentas propias: el mismo movimiento aparece en negativo
    # en una cuenta y en positivo en la otra, así que su neto es cero y no debe
    # mezclarse ni con el gasto ni con el ingreso.
    ('traspaso', 'Traspaso entre cuentas'),
]

# Categorías cuyo bloque cambió al introducir Discrecionales e Ingresos. Los
# hogares creados antes las tienen con el tipo antiguo, así que hay que
# recolocarlas (lo hace la migración finanzas.0015 y, por si acaso, esta misma
# función en cada visita).
TIPOS_CORREGIDOS = {
    'Ocio': 'discrecional',
    'Restaurantes': 'discrecional',
    'Ropa': 'discrecional',
    'Suscripciones': 'discrecional',
    'Tecnologia / Software': 'discrecional',
}

CATEGORIA_TRASPASO = 'Traspaso entre cuentas'
CATEGORIA_DEVOLUCIONES = 'Devoluciones'
CATEGORIA_OTROS_INGRESOS = 'Otros ingresos'


def _crear_categorias_predefinidas(hogar):
    for tipo, nombre in CATEGORIAS_PREDEFINIDAS:
        categoria, creada = CategoriaGasto.objects.get_or_create(
            hogar=hogar, nombre=nombre,
            defaults={'tipo': tipo, 'es_predefinida': True}
        )
        # Recolocar las predefinidas que cambiaron de bloque. Solo se tocan las
        # que siguen marcadas como predefinidas: si el usuario creó una propia
        # con ese nombre, su criterio manda.
        if (not creada and categoria.es_predefinida
                and nombre in TIPOS_CORREGIDOS
                and categoria.tipo != TIPOS_CORREGIDOS[nombre]):
            categoria.tipo = TIPOS_CORREGIDOS[nombre]
            categoria.save(update_fields=['tipo'])


@login_required
def listar_gastos(request):
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        messages.error(request, "Necesitas pertenecer a un hogar.")
        return redirect('dashboard')

    hogar = profile.hogar
    _crear_categorias_predefinidas(hogar)

    # El presupuesto solo declara GASTO: las categorías de ingreso y de traspaso
    # existen para poder clasificar los movimientos del extracto, no para
    # presupuestarse aquí.
    categorias = CategoriaGasto.objects.filter(
        hogar=hogar, activo=True, tipo__in=TIPOS_GASTO,
    ).prefetch_related('partidas')

    gastos_fijos = []
    gastos_anuales = []
    gastos_variables = []
    gastos_discrecionales = []

    total_fijos = Decimal('0')
    total_anuales_anual = Decimal('0')
    total_provision = Decimal('0')
    total_variables = Decimal('0')
    total_discrecionales = Decimal('0')

    for cat in categorias:
        partidas = cat.partidas.filter(activo=True).select_related('responsable')
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
        elif cat.tipo == 'discrecional':
            gastos_discrecionales.append(entrada)
            total_discrecionales += subtotal_mensual

    total_mensual = total_fijos + total_provision + total_variables + total_discrecionales

    open_cat = request.GET.get('open', '')
    vista = request.GET.get('vista', 'categoria')

    # --- Agrupación alternativa: por miembro responsable ---
    # (común = partidas sin responsable). Permite ver qué cubre cada persona
    # y qué es compartido, además de la vista por categoría.
    grupos_miembro = _agrupar_por_miembro(hogar, categorias)

    return render(request, 'finanzas/gastos/listar.html', {
        'hogar': hogar,
        'gastos_fijos': gastos_fijos,
        'gastos_anuales': gastos_anuales,
        'gastos_variables': gastos_variables,
        'gastos_discrecionales': gastos_discrecionales,
        'total_fijos': total_fijos,
        'total_anuales_anual': total_anuales_anual,
        'total_provision': total_provision,
        'total_variables': total_variables,
        'total_discrecionales': total_discrecionales,
        'total_mensual': total_mensual,
        'open_cat': open_cat,
        'vista': vista,
        'grupos_miembro': grupos_miembro,
    })


def _agrupar_por_miembro(hogar, categorias):
    """Agrupa todas las partidas activas por miembro responsable (y un grupo
    'común' para las que no tienen responsable). Devuelve una lista ordenada:
    primero el común, luego cada miembro, cada uno con su total mensual, total
    anual y sus partidas (con la categoría anotada)."""
    grupos = {}  # clave: user_id o None → dict

    def _grupo(clave, nombre):
        if clave not in grupos:
            grupos[clave] = {
                'clave': clave, 'nombre': nombre, 'es_comun': clave is None,
                'partidas': [], 'total_mensual': Decimal('0'), 'total_anual': Decimal('0'),
            }
        return grupos[clave]

    # Asegura que cada miembro aparezca aunque no tenga partidas propias.
    _grupo(None, 'Común del hogar')
    for m in hogar.miembros.select_related('user').all():
        nombre = m.user.first_name or m.user.username
        _grupo(m.user_id, nombre)

    for cat in categorias:
        for p in cat.partidas.filter(activo=True).select_related('responsable', 'categoria'):
            clave = p.responsable_id
            if clave is not None and clave not in grupos:
                # Responsable que ya no es miembro: agrúpalo por su nombre igualmente.
                nombre = p.responsable.first_name or p.responsable.username if p.responsable else 'Otros'
                _grupo(clave, nombre)
            g = _grupo(clave, grupos.get(clave, {}).get('nombre', 'Común del hogar'))
            g['partidas'].append(p)
            g['total_mensual'] += p.importe_mensual
            g['total_anual'] += p.importe_anual

    # Común primero, luego miembros con gasto, luego los que no tienen nada.
    ordenados = sorted(
        grupos.values(),
        key=lambda g: (not g['es_comun'], -float(g['total_mensual']), g['nombre'].lower()),
    )
    return [g for g in ordenados if g['partidas'] or g['es_comun']]


@login_required
def crear_partida(request):
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        return redirect('dashboard')

    hogar = profile.hogar
    # Solo categorías de gasto: no se presupuesta un ingreso ni un traspaso.
    categorias = CategoriaGasto.objects.filter(
        hogar=hogar, activo=True, tipo__in=TIPOS_GASTO,
    ).order_by('tipo', 'nombre')
    miembros = hogar.miembros.select_related('user').all()

    if request.method == 'POST':
        categoria_id = request.POST.get('categoria_id')
        nombre = request.POST.get('nombre', '').strip()
        importe = request.POST.get('importe', '0')
        periodicidad = request.POST.get('periodicidad', 'mensual')
        mes_pago = request.POST.get('mes_pago') or None
        responsable_id = request.POST.get('responsable_id') or None

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
                responsable_id=int(responsable_id) if responsable_id else None,
            )
            messages.success(request, f"Gasto '{nombre}' creado.")
            return redirect(f'/finanzas/gastos/?open={categoria.id}')

    return render(request, 'finanzas/gastos/crear.html', {
        'categorias': categorias,
        'miembros': miembros,
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
    # Solo categorías de gasto: no se presupuesta un ingreso ni un traspaso.
    categorias = CategoriaGasto.objects.filter(
        hogar=hogar, activo=True, tipo__in=TIPOS_GASTO,
    ).order_by('tipo', 'nombre')
    miembros = hogar.miembros.select_related('user').all()

    if request.method == 'POST':
        partida.categoria_id = request.POST.get('categoria_id')
        partida.nombre = request.POST.get('nombre', '').strip()
        partida.importe = Decimal(request.POST.get('importe', '0'))
        partida.periodicidad = request.POST.get('periodicidad', 'mensual')
        mes_pago = request.POST.get('mes_pago')
        partida.mes_pago = int(mes_pago) if mes_pago else None
        responsable_id = request.POST.get('responsable_id')
        partida.responsable_id = int(responsable_id) if responsable_id else None
        partida.save()
        messages.success(request, f"Gasto '{partida.nombre}' actualizado.")
        return redirect(f'/finanzas/gastos/?open={partida.categoria_id}')

    return render(request, 'finanzas/gastos/editar.html', {
        'partida': partida,
        'categorias': categorias,
        'miembros': miembros,
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
