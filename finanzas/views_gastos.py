from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Count
from django.http import JsonResponse
from decimal import Decimal

from . import costes_activo
from .models import (
    CategoriaGasto, CategoriaPredefinidaDescartada, PartidaGasto, MESES_CHOICES,
    PERIODICIDAD_GASTO_CHOICES, COMPUTO_CHOICES, COMPUTO_NEUTRO, ETIQUETAS_TIPO,
    ORDEN_TIPOS,
    TIPO_GASTO_CHOICES, TIPOS_GASTO, computo_por_defecto,
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

NOMBRES_PREDEFINIDOS = {nombre for _, nombre in CATEGORIAS_PREDEFINIDAS}

CATEGORIA_TRASPASO = 'Traspaso entre cuentas'
CATEGORIA_DEVOLUCIONES = 'Devoluciones'
CATEGORIA_OTROS_INGRESOS = 'Otros ingresos'


def _crear_categorias_predefinidas(hogar):
    """Siembra las categorías de fábrica que le falten al hogar.

    Se llama al abrir media aplicación, y en el 99,9 % de las visitas no hay
    nada que crear. Por eso primero se lee de una vez lo que YA existe y solo
    se escribe lo que falta: antes hacía un get_or_create por categoría —treinta
    consultas en cada carga de Gastos, Categorías, Sin categorizar y
    Conciliación— para no crear nada.
    """
    # Las que el hogar ha eliminado no se vuelven a crear: sin esto, «eliminar»
    # una categoría de fábrica duraba hasta la siguiente visita.
    descartadas = set(
        CategoriaPredefinidaDescartada.objects
        .filter(hogar=hogar).values_list('nombre', flat=True)
    )
    # Ojo: se leen SIN filtrar por `activo`. Es deliberado: una predefinida que
    # el usuario ha archivado no debe resucitar en la siguiente visita.
    existentes = {
        c.nombre: c for c in CategoriaGasto.objects.filter(
            hogar=hogar,
            nombre__in=[n for _, n in CATEGORIAS_PREDEFINIDAS],
        )
    }

    faltan = [
        CategoriaGasto(hogar=hogar, nombre=nombre, tipo=tipo,
                       computo=computo_por_defecto(tipo), es_predefinida=True)
        for tipo, nombre in CATEGORIAS_PREDEFINIDAS
        if nombre not in descartadas and nombre not in existentes
    ]
    if faltan:
        # ignore_conflicts: dos pestañas abiertas a la vez pueden intentar
        # sembrar el mismo hogar; la segunda no debe reventar.
        CategoriaGasto.objects.bulk_create(faltan, ignore_conflicts=True)

    # Recolocar las predefinidas que cambiaron de bloque. Solo se tocan las que
    # siguen marcadas como predefinidas: si el usuario creó una propia con ese
    # nombre, su criterio manda.
    recolocar = [
        c for nombre, c in existentes.items()
        if c.es_predefinida and nombre in TIPOS_CORREGIDOS
        and c.tipo != TIPOS_CORREGIDOS[nombre]
    ]
    if recolocar:
        for c in recolocar:
            c.tipo = TIPOS_CORREGIDOS[c.nombre]
        CategoriaGasto.objects.bulk_update(recolocar, ['tipo'])


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
            partida = PartidaGasto(
                hogar=hogar,
                categoria=categoria,
                nombre=nombre,
                importe=Decimal(importe),
                periodicidad=periodicidad,
                mes_pago=int(mes_pago) if mes_pago else None,
                responsable_id=int(responsable_id) if responsable_id else None,
            )
            # Imputación a un vehículo o una propiedad: es lo que después
            # permite saber lo que cuesta mantenerlos.
            costes_activo.asignar(
                partida, costes_activo.resolver(hogar, request.POST.get('activo') or ''),
            )
            partida.save()
            messages.success(request, f"Gasto '{nombre}' creado.")
            return redirect(f'/finanzas/gastos/?open={categoria.id}')

    return render(request, 'finanzas/gastos/crear.html', {
        'categorias': categorias,
        'miembros': miembros,
        'hogar': hogar,
        'meses': MESES_CHOICES,
        'periodicidades': PERIODICIDAD_GASTO_CHOICES,
        'grupos_activos': costes_activo.opciones(hogar),
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
        costes_activo.asignar(
            partida, costes_activo.resolver(hogar, request.POST.get('activo') or ''),
        )
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
        'grupos_activos': costes_activo.opciones(hogar),
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


# ---------------------------------------------------------------------------
# Categorías
#
# Son la pieza común de gastos, ingresos y movimientos del extracto: el usuario
# las crea, las renombra, las mueve de bloque y decide su CÓMPUTO (si lo que cae
# en ellas resta, suma o no cuenta). Por eso tienen su propia pantalla y no un
# rincón dentro del presupuesto.
# ---------------------------------------------------------------------------

# Cómo llegan a la vista los tipos válidos para el desplegable, en el orden de
# presentación del presupuesto (el de TIPO_GASTO_CHOICES ya lo es).
TIPOS_CATEGORIA = [
    {'valor': valor, 'etiqueta': etiqueta} for valor, etiqueta in TIPO_GASTO_CHOICES
]


def _tipo_valido(tipo):
    return tipo in dict(TIPO_GASTO_CHOICES)


def _computo_valido(computo):
    return computo in dict(COMPUTO_CHOICES)


def _volver_a_categorias(request):
    """Vuelve a la pantalla desde la que se editó. Las categorías se tocan tanto
    desde su propia pantalla como desde el presupuesto, y devolver siempre al
    mismo sitio obligaba a rehacer el camino."""
    destino = request.POST.get('volver_a') or ''
    if destino.startswith('/') and not destino.startswith('//'):
        return redirect(destino)
    return redirect('finanzas:listar_categorias')


@login_required
def listar_categorias(request):
    """Pantalla de gestión: todas las categorías del hogar agrupadas por bloque,
    con lo que cuelga de cada una (partidas declaradas y movimientos ya
    clasificados), que es lo que hay que saber antes de borrarla."""
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        messages.error(request, "Necesitas pertenecer a un hogar.")
        return redirect('dashboard')

    hogar = profile.hogar
    _crear_categorias_predefinidas(hogar)

    categorias = (
        CategoriaGasto.objects.filter(hogar=hogar)
        .annotate(
            num_partidas=Count('partidas', distinct=True),
            num_movimientos=Count('movimientos_bancarios', distinct=True),
            num_reglas=Count('reglas_categorizacion', distinct=True),
        )
        .order_by('nombre')
    )

    por_tipo = {}
    for cat in categorias:
        por_tipo.setdefault(cat.tipo, []).append(cat)

    bloques = [
        {
            'tipo': tipo,
            'etiqueta': ETIQUETAS_TIPO.get(tipo, tipo),
            'categorias': por_tipo.get(tipo, []),
        }
        for tipo in ORDEN_TIPOS if por_tipo.get(tipo)
    ]
    # Un bloque desconocido (quedaría de un tipo retirado) no debe hacer
    # desaparecer sus categorías de la pantalla.
    for tipo, cats in por_tipo.items():
        if tipo not in ORDEN_TIPOS:
            bloques.append({'tipo': tipo, 'etiqueta': tipo, 'categorias': cats})

    activas = [c for c in categorias if c.activo]
    return render(request, 'finanzas/categorias/listar.html', {
        'bloques': bloques,
        'tipos': TIPOS_CATEGORIA,
        'computos': COMPUTO_CHOICES,
        'total_categorias': len(activas),
        'total_archivadas': len(categorias) - len(activas),
        'categorias_destino': sorted(activas, key=lambda c: c.nombre.lower()),
    })


@login_required
def crear_categoria(request):
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        return redirect('dashboard')

    if request.method != 'POST':
        return redirect('finanzas:listar_categorias')

    nombre = request.POST.get('nombre', '').strip()
    tipo = request.POST.get('tipo', 'variable')
    computo = request.POST.get('computo') or ''

    if not nombre:
        messages.error(request, "El nombre no puede estar vacío.")
        return _volver_a_categorias(request)
    if not _tipo_valido(tipo):
        messages.error(request, "El bloque indicado no existe.")
        return _volver_a_categorias(request)
    if not _computo_valido(computo):
        computo = computo_por_defecto(tipo)

    # Volver a crear una categoría que se había descartado la resucita de
    # verdad: la lápida solo existe para que no reaparezca sola.
    CategoriaPredefinidaDescartada.objects.filter(
        hogar=profile.hogar, nombre=nombre,
    ).delete()

    categoria, creada = CategoriaGasto.objects.get_or_create(
        hogar=profile.hogar, nombre=nombre,
        defaults={'tipo': tipo, 'computo': computo, 'es_predefinida': False},
    )
    if creada:
        messages.success(request, f"Categoría «{nombre}» creada.")
    elif not categoria.activo:
        # Volver a crear una que estaba archivada es, en la práctica,
        # reactivarla: si no, chocaría con la restricción de nombre único.
        categoria.activo = True
        categoria.tipo = tipo
        categoria.computo = computo
        categoria.save(update_fields=['activo', 'tipo', 'computo'])
        messages.success(request, f"Categoría «{nombre}» reactivada.")
    else:
        messages.warning(request, f"Ya existe una categoría llamada «{nombre}».")

    return _volver_a_categorias(request)


@login_required
def editar_categoria(request, categoria_id):
    """Renombra la categoría y/o cambia su bloque y su cómputo."""
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        return redirect('dashboard')

    categoria = get_object_or_404(CategoriaGasto, id=categoria_id, hogar=profile.hogar)
    if request.method != 'POST':
        return redirect('finanzas:listar_categorias')

    nombre = request.POST.get('nombre', '').strip()
    tipo = request.POST.get('tipo', categoria.tipo)
    computo = request.POST.get('computo', categoria.computo)

    if not nombre:
        messages.error(request, "El nombre no puede estar vacío.")
        return _volver_a_categorias(request)
    if not _tipo_valido(tipo):
        messages.error(request, "El bloque indicado no existe.")
        return _volver_a_categorias(request)
    if not _computo_valido(computo):
        messages.error(request, "El cómputo indicado no existe.")
        return _volver_a_categorias(request)

    # Una categoría con gasto presupuestado no puede ser neutra: se concilia
    # contra el presupuesto, y «no cuenta» dejaría esa partida comparándose
    # eternamente contra cero.
    if computo == COMPUTO_NEUTRO and categoria.partidas.filter(activo=True).exists():
        messages.error(
            request,
            f"«{categoria.nombre}» tiene gasto presupuestado, así que no puede ser "
            "neutra. Quita sus gastos declarados primero o elige otro cómputo.",
        )
        return _volver_a_categorias(request)

    choque = CategoriaGasto.objects.filter(
        hogar=profile.hogar, nombre=nombre,
    ).exclude(pk=categoria.pk).exists()
    if choque:
        messages.error(request, f"Ya existe otra categoría llamada «{nombre}».")
        return _volver_a_categorias(request)

    anterior = categoria.nombre
    categoria.nombre = nombre
    categoria.tipo = tipo
    categoria.computo = computo
    # Deja de ser «de fábrica» en cuanto el usuario la ajusta: así el
    # mantenimiento de las predefinidas no vuelve a moverla de bloque.
    categoria.es_predefinida = False
    categoria.save(update_fields=['nombre', 'tipo', 'computo', 'es_predefinida'])

    if anterior != nombre:
        messages.success(request, f"Categoría «{anterior}» renombrada a «{nombre}».")
    else:
        messages.success(request, f"Categoría «{nombre}» actualizada.")
    return _volver_a_categorias(request)


@login_required
def cambiar_bloque_categoria(request):
    """Mueve una categoría de pilar sin salir de donde estás.

    El bloque es lo que hace conciliable el gasto observado con el
    presupuesto, así que el sitio natural para decir «Salud va en variables» es
    la propia pantalla donde ves que está mal colocada, no un formulario
    aparte."""
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        return JsonResponse({'ok': False, 'error': 'sin_hogar'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'metodo'}, status=405)

    categoria = CategoriaGasto.objects.filter(
        hogar=profile.hogar, id=request.POST.get('categoria_id') or 0,
    ).first()
    tipo = request.POST.get('tipo') or ''
    if not categoria or not _tipo_valido(tipo):
        return JsonResponse({'ok': False, 'error': 'datos_invalidos'}, status=400)

    categoria.tipo = tipo
    # Deja de ser «de fábrica»: el mantenimiento de las predefinidas no debe
    # devolverla a su bloque original después de que el usuario la mueva.
    categoria.es_predefinida = False
    categoria.save(update_fields=['tipo', 'es_predefinida'])

    return JsonResponse({
        'ok': True, 'categoria': categoria.nombre,
        'tipo': categoria.tipo, 'etiqueta': ETIQUETAS_TIPO.get(tipo, tipo),
    })


@login_required
def archivar_categoria(request, categoria_id):
    """Activa o desactiva la categoría. Archivar la saca de los selectores sin
    tocar nada de lo ya clasificado, que es lo que suele querer quien deja de
    usar una categoría pero no quiere perder su historial."""
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        return redirect('dashboard')

    categoria = get_object_or_404(CategoriaGasto, id=categoria_id, hogar=profile.hogar)
    if request.method != 'POST':
        return redirect('finanzas:listar_categorias')

    categoria.activo = not categoria.activo
    categoria.save(update_fields=['activo'])
    messages.success(
        request,
        f"Categoría «{categoria.nombre}» {'reactivada' if categoria.activo else 'archivada'}.",
    )
    return _volver_a_categorias(request)


@login_required
def eliminar_categoria(request, categoria_id):
    """Borra la categoría, opcionalmente moviendo antes lo que cuelga de ella.

    Borrar arrastra en cascada las partidas declaradas y las reglas aprendidas,
    así que si hay gasto presupuestado se exige decir a dónde va: perder el
    presupuesto por borrar una categoría sería un destrozo silencioso."""
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        return redirect('dashboard')

    hogar = profile.hogar
    categoria = get_object_or_404(CategoriaGasto, id=categoria_id, hogar=hogar)
    if request.method != 'POST':
        return redirect('finanzas:listar_categorias')

    destino = None
    destino_id = request.POST.get('reasignar_a') or ''
    if destino_id:
        destino = CategoriaGasto.objects.filter(
            hogar=hogar, id=destino_id,
        ).exclude(pk=categoria.pk).first()
        if not destino:
            messages.error(request, "La categoría de destino no es válida.")
            return _volver_a_categorias(request)

    num_partidas = categoria.partidas.count()
    if num_partidas and not destino:
        messages.error(
            request,
            f"«{categoria.nombre}» tiene {num_partidas} gasto(s) declarado(s). "
            "Elige a qué categoría moverlos antes de eliminarla, o archívala.",
        )
        return _volver_a_categorias(request)

    nombre = categoria.nombre
    era_predefinida = categoria.es_predefinida or nombre in NOMBRES_PREDEFINIDOS
    movidos = 0
    if destino:
        movidos = categoria.partidas.update(categoria=destino)
        movidos += categoria.movimientos_bancarios.update(categoria=destino)
        # Las reglas aprendidas se mueven una a una: dos reglas con el mismo
        # patrón chocarían con la restricción (hogar, patrón), y en ese caso la
        # que ya apuntaba al destino es la buena.
        for regla in categoria.reglas_categorizacion.all():
            if type(regla).objects.filter(
                hogar=hogar, patron=regla.patron, categoria=destino,
            ).exists():
                regla.delete()
            else:
                regla.categoria = destino
                regla.save(update_fields=['categoria'])

    # Los movimientos que queden apuntando a la categoría se quedan sin
    # categorizar (la FK es SET_NULL); se avisa para que no sea una sorpresa.
    huerfanos = categoria.movimientos_bancarios.count()
    categoria.delete()

    if era_predefinida:
        CategoriaPredefinidaDescartada.objects.get_or_create(hogar=hogar, nombre=nombre)

    detalle = []
    if movidos:
        detalle.append(f"{movidos} elemento(s) movidos a «{destino.nombre}»")
    if huerfanos:
        detalle.append(f"{huerfanos} movimiento(s) quedaron sin categorizar")
    messages.success(
        request,
        f"Categoría «{nombre}» eliminada" + (f" · {', '.join(detalle)}." if detalle else "."),
    )
    return _volver_a_categorias(request)
