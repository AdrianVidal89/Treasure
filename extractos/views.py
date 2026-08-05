import difflib
from collections import defaultdict
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from finanzas.models import CategoriaGasto, CuentaBancaria, PartidaGasto
from finanzas.parsing import leer_tabla
from finanzas.views_gastos import _crear_categorias_predefinidas

from .categorizacion import categorizar_lote
from .models import ExtractoBancario, MovimientoBancario, ReglaCategorizacion
from .normalizacion import (
    es_traspaso_interno, nombres_del_hogar, normalizar_comercio, normalizar_texto,
)
from .parser import CAMPO_LABELS, analizar_extracto

SESSION_KEY_PENDIENTES = 'extractos_pendientes'
SESSION_KEY_META = 'extractos_pendientes_meta'
CAMPOS_MAPEO = ('fecha', 'concepto', 'concepto_extra', 'importe', 'saldo',
                'debe', 'haber', 'estado')

# Umbral de parecido para sugerir plegar dos comercios distintos en una misma
# regla ('farmacia ronda' / 'farmacia rondo'). Solo se sugiere: aplica el usuario.
UMBRAL_SIMILITUD = 0.85

MESES_ES = [
    '', 'Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio',
    'Julio', 'Agosto', 'Septiembre', 'Octubre', 'Noviembre', 'Diciembre',
]


def _get_hogar(request):
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        return None, None
    return profile, profile.hogar


@login_required
def listar(request):
    profile, hogar = _get_hogar(request)
    if not hogar:
        messages.error(request, "Necesitas pertenecer a un hogar.")
        return redirect('dashboard')

    extractos = ExtractoBancario.objects.filter(hogar=hogar).select_related('cuenta')

    # Vista GLOBAL: el mismo panel de análisis del detalle, pero sobre TODOS
    # los movimientos del hogar (todos los extractos juntos).
    todos = list(
        MovimientoBancario.objects.filter(hogar=hogar)
        .select_related('categoria').order_by('-fecha')
    )
    panel = _panel_context(hogar, todos, request)

    return render(request, 'extractos/listar.html', {
        'panel': panel,
        'extractos': extractos,
        'total_extractos': extractos.count(),
        'total_movimientos': len(todos),
    })


@login_required
def subir(request):
    profile, hogar = _get_hogar(request)
    if not hogar:
        messages.error(request, "Necesitas pertenecer a un hogar.")
        return redirect('dashboard')

    # Las cuentas bancarias se asocian por usuario; tomamos las de los miembros del hogar.
    cuentas = CuentaBancaria.objects.filter(
        usuario__userprofile__hogar=hogar, activa=True,
    )

    if request.method != 'POST':
        return render(request, 'extractos/subir.html', {'cuentas': cuentas})

    archivos = request.FILES.getlist('archivos')
    if not archivos:
        messages.error(request, "No se subió ningún archivo.")
        return render(request, 'extractos/subir.html', {'cuentas': cuentas})

    # No importamos directamente: guardamos el texto crudo en sesión y lo
    # analizamos en la pantalla de revisión, donde el usuario puede corregir
    # el mapeo de columnas antes de confirmar. Así un formato de banco no
    # reconocido no falla en silencio.
    pendientes = []
    for archivo in archivos:
        try:
            texto = leer_tabla(archivo)
        except Exception:
            messages.warning(request, f"No se pudo leer «{archivo.name}». ¿Es un CSV o Excel válido?")
            continue
        pendientes.append({'nombre': archivo.name[:255], 'texto': texto})

    if not pendientes:
        return render(request, 'extractos/subir.html', {'cuentas': cuentas})

    request.session[SESSION_KEY_PENDIENTES] = pendientes
    request.session[SESSION_KEY_META] = {
        'nombre_banco': (request.POST.get('nombre_banco') or '').strip(),
        'cuenta_id': request.POST.get('cuenta') or None,
    }
    return redirect('extractos:revisar')


def _analizar_pendientes(pendientes, mapeos_manuales=None):
    """Ejecuta analizar_extracto sobre cada archivo pendiente de la sesión.

    mapeos_manuales: {indice_archivo(str): {campo: valor}} con las
    correcciones de mapeo enviadas desde el formulario de revisión.
    """
    mapeos_manuales = mapeos_manuales or {}
    analizados = []
    for i, pend in enumerate(pendientes):
        mapeo = mapeos_manuales.get(str(i))
        resultado = analizar_extracto(pend['texto'], mapeo_manual=mapeo)
        analizados.append({'nombre': pend['nombre'], 'resultado': resultado})
    return analizados


def _leer_mapeos_manuales(POST, num_archivos):
    """Reconstruye {indice_archivo: {campo: valor}} a partir de los campos
    'mapeo_<i>_<campo>' enviados por el formulario de revisión."""
    mapeos = {}
    for i in range(num_archivos):
        mapeo_archivo = {}
        for campo in CAMPOS_MAPEO:
            clave = f'mapeo_{i}_{campo}'
            if clave in POST:
                mapeo_archivo[campo] = POST.get(clave)
        if mapeo_archivo:
            mapeos[str(i)] = mapeo_archivo
    return mapeos


def _importar_analizados(hogar, usuario, nombre_banco, cuenta, analizados):
    """Escribe en BD los movimientos ya analizados (y revisados). Devuelve
    un dict con los totales para mostrar en los mensajes de resultado."""
    # El diccionario de comercios devuelve NOMBRES de categoría; si el hogar es
    # antiguo puede no tener aún las categorías predefinidas más recientes y la
    # categorización fallaría en silencio. Se crean aquí igual que hace la
    # sección de Gastos al entrar.
    _crear_categorias_predefinidas(hogar)
    nombres_hogar = nombres_del_hogar(hogar)

    total_creados = 0
    total_duplicados = 0
    total_categorizados = 0
    total_omitidos = 0
    total_no_firmes = 0
    total_traspasos = 0
    extractos_ok = 0

    for item in analizados:
        movimientos = item['resultado']['movimientos']
        total_omitidos += len(item['resultado']['filas_error'])
        total_no_firmes += len(item['resultado'].get('filas_omitidas', []))
        if not movimientos:
            continue

        extracto = ExtractoBancario.objects.create(
            hogar=hogar,
            usuario=usuario,
            nombre_banco=nombre_banco,
            cuenta=cuenta,
            archivo_nombre=item['nombre'],
        )

        # Los traspasos entre cuentas propias no son gasto: ni se categorizan ni
        # cuentan en los KPIs.
        traspasos = [es_traspaso_interno(m['concepto'], nombres_hogar) for m in movimientos]

        # Se categoriza en bloque para cargar las reglas del hogar una sola vez.
        # Solo el gasto (importe negativo) y solo lo que no sea un traspaso.
        categorizables = [
            m['concepto'] if (m['importe'] < 0 and not t) else ''
            for m, t in zip(movimientos, traspasos)
        ]
        sugerencias = categorizar_lote(categorizables, hogar)

        creados = 0
        fechas = []
        for mov, es_traspaso, (categoria, origen) in zip(movimientos, traspasos, sugerencias):
            hash_mov = MovimientoBancario.calcular_hash(
                mov['fecha'], mov['concepto'], mov['importe'], mov['saldo'],
            )
            if MovimientoBancario.objects.filter(hogar=hogar, hash_dedupe=hash_mov).exists():
                total_duplicados += 1
                continue

            estado = 'sin_categorizar'
            if categoria:
                # Distinguir el acierto del diccionario del criterio propio del
                # usuario (regla aprendida), que antes se marcaban igual.
                estado = 'por_regla' if origen == 'regla' else 'por_codigo'
                total_categorizados += 1
            if es_traspaso:
                total_traspasos += 1

            MovimientoBancario.objects.create(
                extracto=extracto,
                hogar=hogar,
                fecha=mov['fecha'],
                concepto=mov['concepto'][:300],
                concepto_raw=mov.get('concepto_raw') or mov['concepto'],
                comercio=normalizar_comercio(mov['concepto']),
                importe=mov['importe'],
                saldo=mov['saldo'],
                categoria=categoria,
                estado_categorizacion=estado,
                es_traspaso=es_traspaso,
                hash_dedupe=hash_mov,
            )
            creados += 1
            fechas.append(mov['fecha'])

        if creados == 0:
            # Todo eran duplicados: no dejamos un extracto vacío.
            extracto.delete()
            continue

        extracto.num_movimientos = creados
        if fechas:
            extracto.periodo_inicio = min(fechas)
            extracto.periodo_fin = max(fechas)
        extracto.save()
        total_creados += creados
        extractos_ok += 1

    return {
        'total_creados': total_creados,
        'total_duplicados': total_duplicados,
        'total_categorizados': total_categorizados,
        'total_omitidos': total_omitidos,
        'total_no_firmes': total_no_firmes,
        'total_traspasos': total_traspasos,
        'extractos_ok': extractos_ok,
    }


@login_required
def revisar(request):
    """Paso previo de revisión: muestra qué columna se ha detectado para cada
    dato y una vista previa de los movimientos antes de importar de verdad.
    Permite corregir el mapeo (p. ej. si el banco usa un formato no
    reconocido) y volver a analizar sin perder el archivo subido."""
    profile, hogar = _get_hogar(request)
    if not hogar:
        messages.error(request, "Necesitas pertenecer a un hogar.")
        return redirect('dashboard')

    pendientes = request.session.get(SESSION_KEY_PENDIENTES)
    if not pendientes:
        messages.info(request, "No hay ningún archivo pendiente de revisión. Sube un CSV primero.")
        return redirect('extractos:subir')

    meta = request.session.get(SESSION_KEY_META, {})
    cuentas = CuentaBancaria.objects.filter(
        usuario__userprofile__hogar=hogar, activa=True,
    )

    if request.method == 'POST':
        accion = request.POST.get('accion')

        if accion == 'cancelar':
            request.session.pop(SESSION_KEY_PENDIENTES, None)
            request.session.pop(SESSION_KEY_META, None)
            messages.info(request, "Importación cancelada.")
            return redirect('extractos:subir')

        mapeos_manuales = _leer_mapeos_manuales(request.POST, len(pendientes))
        analizados = _analizar_pendientes(pendientes, mapeos_manuales)

        nombre_banco = request.POST.get('nombre_banco', meta.get('nombre_banco', '')).strip()
        cuenta_id = request.POST.get('cuenta') or meta.get('cuenta_id')
        cuenta = None
        if cuenta_id:
            cuenta = cuentas.filter(id=cuenta_id).first()

        if accion == 'confirmar':
            totales = _importar_analizados(hogar, request.user, nombre_banco, cuenta, analizados)
            request.session.pop(SESSION_KEY_PENDIENTES, None)
            request.session.pop(SESSION_KEY_META, None)

            if totales['total_creados']:
                messages.success(
                    request,
                    f"Importados {totales['total_creados']} movimientos en "
                    f"{totales['extractos_ok']} extracto(s). "
                    f"{totales['total_categorizados']} categorizados automáticamente."
                )
            if totales['total_traspasos']:
                messages.info(
                    request,
                    f"{totales['total_traspasos']} movimiento(s) detectados como traspaso "
                    "entre cuentas propias: no cuentan como gasto."
                )
            if totales['total_duplicados']:
                messages.info(request, f"{totales['total_duplicados']} movimientos duplicados ignorados.")
            if totales['total_no_firmes']:
                messages.info(
                    request,
                    f"{totales['total_no_firmes']} operación(es) pendientes o rechazadas "
                    "se han omitido; se importarán cuando el banco las consolide."
                )
            if totales['total_omitidos']:
                messages.warning(
                    request,
                    f"{totales['total_omitidos']} fila(s) no se pudieron interpretar y se omitieron."
                )
            if not totales['total_creados'] and not totales['total_duplicados']:
                messages.error(request, "No se pudo importar ningún movimiento. Revisa el mapeo de columnas.")
            return redirect('extractos:listar')

        # accion == 'reanalizar' (o cualquier otra cosa): recalcular la vista
        # previa con el mapeo corregido y mantenernos en la revisión.
        request.session[SESSION_KEY_META] = {'nombre_banco': nombre_banco, 'cuenta_id': cuenta_id}
        meta = request.session[SESSION_KEY_META]
    else:
        analizados = _analizar_pendientes(pendientes)

    archivos_ctx = []
    for i, item in enumerate(analizados):
        r = item['resultado']
        campos_ctx = []
        for campo in CAMPOS_MAPEO:
            campos_ctx.append({
                'campo': campo,
                'label': CAMPO_LABELS[campo],
                'seleccionado': r['mapa'].get(campo),
            })
        archivos_ctx.append({
            'indice': i,
            'nombre': item['nombre'],
            'cabecera': list(enumerate(r['cabecera'])),
            'campos': campos_ctx,
            'errores_generales': r['errores_generales'],
            'total_ok': len(r['movimientos']),
            'total_error': len(r['filas_error']),
            'total_omitidas': len(r.get('filas_omitidas', [])),
            'preview': r['movimientos'][:15],
            'preview_restantes': max(0, len(r['movimientos']) - 15),
            'filas_error': r['filas_error'][:20],
            'filas_error_restantes': max(0, len(r['filas_error']) - 20),
        })

    total_ok = sum(a['total_ok'] for a in archivos_ctx)
    total_error = sum(a['total_error'] for a in archivos_ctx)

    return render(request, 'extractos/revisar.html', {
        'archivos': archivos_ctx,
        'cuentas': cuentas,
        'nombre_banco': meta.get('nombre_banco', ''),
        'cuenta_id': meta.get('cuenta_id'),
        'total_ok': total_ok,
        'total_error': total_error,
    })


# Paleta para el donut de categorías (verdes/tierra coherentes con la marca).
_PALETA = [
    '#2d6a4f', '#3DCD58', '#2c5f7a', '#b7791f', '#b4442e', '#1b4332',
    '#40916c', '#5f8fb0', '#d4a017', '#9d4edd', '#e07a5f', '#81b29a',
]


def _panel_context(hogar, todos, request):
    """Construye el panel de análisis de movimientos (KPIs, donut, ingresos vs
    gastos, filtros año/mes/categoría y listado agrupado por mes) que comparten
    el detalle de un extracto y la vista global de todos los extractos.

    `todos`: lista de MovimientoBancario (ya acotada al hogar y al ámbito que
    corresponda — un extracto o todos)."""
    # --- Filtros disponibles ---
    anios_disponibles = sorted({m.fecha.year for m in todos}, reverse=True)
    meses_disponibles = [{'valor': str(n), 'etiqueta': MESES_ES[n]} for n in range(1, 13)]

    # --- Filtros activos ---
    anio_sel = request.GET.get('anio', 'all')
    mes_sel = request.GET.get('mes', 'all')
    cat_sel = request.GET.get('categoria', 'all')
    # Los traspasos entre cuentas propias mueven dinero pero no son gasto ni
    # ingreso, así que por defecto quedan fuera del análisis.
    ver_traspasos = request.GET.get('traspasos') == '1'

    def pasa_filtro(m):
        if m.es_traspaso and not ver_traspasos:
            return False
        if anio_sel != 'all' and str(m.fecha.year) != anio_sel:
            return False
        if mes_sel != 'all' and str(m.fecha.month) != mes_sel:
            return False
        if cat_sel != 'all':
            if cat_sel == 'sin':
                if m.categoria_id is not None:
                    return False
            elif str(m.categoria_id) != cat_sel:
                return False
        return True

    movimientos = [m for m in todos if pasa_filtro(m)]

    # --- KPIs sobre el conjunto filtrado ---
    ingresos = sum((m.importe for m in movimientos if m.importe >= 0), Decimal('0'))
    gastos = sum((m.importe for m in movimientos if m.importe < 0), Decimal('0'))
    sin_categorizar = sum(1 for m in movimientos if m.importe < 0 and not m.categoria_id)

    # --- Donut: gasto por categoría (valores absolutos) ---
    por_categoria = defaultdict(lambda: Decimal('0'))
    for m in movimientos:
        if m.importe < 0:
            nombre = m.categoria.nombre if m.categoria else 'Sin categorizar'
            por_categoria[nombre] += -m.importe
    cat_ordenadas = sorted(por_categoria.items(), key=lambda kv: kv[1], reverse=True)
    total_gasto_abs = sum((v for _, v in cat_ordenadas), Decimal('0'))

    donut = []
    for i, (nombre, importe) in enumerate(cat_ordenadas):
        pct = float(importe / total_gasto_abs * 100) if total_gasto_abs else 0
        donut.append({
            'nombre': nombre,
            'importe': float(importe),
            'pct': round(pct, 1),
            'color': '#9aa5a0' if nombre == 'Sin categorizar' else _PALETA[i % len(_PALETA)],
        })

    # --- Agrupación por mes (para el listado) ---
    grupos_mes = defaultdict(lambda: {'movimientos': [], 'ingresos': Decimal('0'), 'gastos': Decimal('0')})
    for m in movimientos:
        g = grupos_mes[(m.fecha.year, m.fecha.month)]
        g['movimientos'].append(m)
        if m.importe >= 0:
            g['ingresos'] += m.importe
        else:
            g['gastos'] += m.importe

    grupos = []
    for (anio, mes), datos in sorted(grupos_mes.items(), reverse=True):
        grupos.append({
            'etiqueta': f"{MESES_ES[mes]} {anio}",
            'ingresos': datos['ingresos'],
            'gastos': datos['gastos'],
            'neto': datos['ingresos'] + datos['gastos'],
            'movimientos': datos['movimientos'],
        })

    categorias_hogar = CategoriaGasto.objects.filter(hogar=hogar, activo=True).order_by('tipo', 'nombre')

    return {
        'grupos': grupos,
        'donut': donut,
        'donut_total': float(total_gasto_abs),
        'kpi_ingresos': ingresos,
        'kpi_gastos': gastos,
        'kpi_neto': ingresos + gastos,
        'kpi_num': len(movimientos),
        'kpi_sin_categorizar': sin_categorizar,
        'anios_disponibles': anios_disponibles,
        'meses_disponibles': meses_disponibles,
        'anio_sel': anio_sel,
        'mes_sel': mes_sel,
        'cat_sel': cat_sel,
        'ver_traspasos': ver_traspasos,
        'num_traspasos': sum(1 for m in todos if m.es_traspaso),
        'categorias_hogar': categorias_hogar,
        'hay_filtro': anio_sel != 'all' or mes_sel != 'all' or cat_sel != 'all',
    }


@login_required
def detalle(request, pk):
    profile, hogar = _get_hogar(request)
    if not hogar:
        messages.error(request, "Necesitas pertenecer a un hogar.")
        return redirect('dashboard')

    extracto = get_object_or_404(ExtractoBancario, pk=pk, hogar=hogar)
    todos = list(extracto.movimientos.select_related('categoria').all())
    panel = _panel_context(hogar, todos, request)
    return render(request, 'extractos/detalle.html', {'extracto': extracto, 'panel': panel})


@login_required
def actualizar_movimiento(request, pk):
    """Edita en línea un movimiento: categoría, concepto y/o importe."""
    profile, hogar = _get_hogar(request)
    if not hogar:
        return JsonResponse({'ok': False, 'error': 'sin_hogar'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'metodo'}, status=405)

    mov = get_object_or_404(MovimientoBancario, pk=pk, hogar=hogar)

    if 'categoria_id' in request.POST:
        cat_raw = request.POST.get('categoria_id') or ''
        if cat_raw == '':
            mov.categoria = None
            mov.estado_categorizacion = 'sin_categorizar'
        else:
            cat = CategoriaGasto.objects.filter(hogar=hogar, id=cat_raw).first()
            if not cat:
                return JsonResponse({'ok': False, 'error': 'categoria_invalida'}, status=400)
            mov.categoria = cat
            mov.estado_categorizacion = 'manual'

    if 'concepto' in request.POST:
        concepto = (request.POST.get('concepto') or '').strip()
        if concepto:
            mov.concepto = concepto[:300]

    if 'importe' in request.POST:
        from decimal import InvalidOperation
        try:
            mov.importe = Decimal((request.POST.get('importe') or '').replace(',', '.'))
        except InvalidOperation:
            return JsonResponse({'ok': False, 'error': 'importe_invalido'}, status=400)

    if 'concepto' in request.POST:
        # El concepto ha cambiado: recalcular el comercio para que la agrupación
        # y el aprendizaje sigan el texto nuevo.
        mov.comercio = normalizar_comercio(mov.concepto)

    mov.save()

    respuesta = {
        'ok': True,
        'categoria': mov.categoria.nombre if mov.categoria else None,
        'categoria_id': mov.categoria_id,
        'concepto': mov.concepto,
        'importe': float(mov.importe),
        'estado': mov.estado_categorizacion,
        'sugerencia': None,
    }

    # Al asignar categoría a mano, ofrecer aplicar el mismo criterio a los
    # movimientos similares y recordarlo como regla. Solo se ofrece: la regla no
    # se crea hasta que el usuario lo confirma.
    if mov.categoria_id and mov.comercio:
        similares = MovimientoBancario.objects.filter(
            hogar=hogar, comercio=mov.comercio, categoria__isnull=True,
            es_traspaso=False,
        ).exclude(pk=mov.pk).count()
        ya_existe = ReglaCategorizacion.objects.filter(
            hogar=hogar, patron=mov.comercio, categoria=mov.categoria, activo=True,
        ).exists()
        if similares and not ya_existe:
            respuesta['sugerencia'] = {
                'patron': mov.comercio,
                'categoria_id': mov.categoria_id,
                'n_similares': similares,
            }

    return JsonResponse(respuesta)


@login_required
def eliminar_movimiento(request, pk):
    """Elimina un único movimiento del extracto."""
    profile, hogar = _get_hogar(request)
    if not hogar:
        return JsonResponse({'ok': False, 'error': 'sin_hogar'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'metodo'}, status=405)

    mov = get_object_or_404(MovimientoBancario, pk=pk, hogar=hogar)
    extracto = mov.extracto
    mov.delete()
    # Recontar movimientos del extracto.
    extracto.num_movimientos = extracto.movimientos.count()
    extracto.save(update_fields=['num_movimientos'])
    return JsonResponse({'ok': True})


@login_required
def conciliacion(request):
    """Cruza los movimientos observados (gasto) contra lo declarado en Gastos."""
    profile, hogar = _get_hogar(request)
    if not hogar:
        messages.error(request, "Necesitas pertenecer a un hogar.")
        return redirect('dashboard')

    movimientos = MovimientoBancario.objects.filter(
        hogar=hogar, importe__lt=0, es_traspaso=False,
    ).select_related('categoria')

    # Nº de meses distintos con datos, para pasar el gasto observado a media mensual.
    meses = {(m.fecha.year, m.fecha.month) for m in movimientos}
    num_meses = max(len(meses), 1)

    # Observado por categoría (gasto absoluto, media mensual).
    observado = defaultdict(lambda: Decimal('0'))
    sin_cat = Decimal('0')
    for m in movimientos:
        if m.categoria_id:
            observado[m.categoria_id] += -m.importe
        else:
            sin_cat += -m.importe

    # Declarado por categoría: suma de importe_mensual de sus partidas activas.
    filas = []
    categorias = CategoriaGasto.objects.filter(hogar=hogar, activo=True).prefetch_related('partidas')
    total_declarado = Decimal('0')
    total_observado = Decimal('0')
    for cat in categorias:
        declarado = sum((p.importe_mensual for p in cat.partidas.filter(activo=True)), Decimal('0'))
        obs_mensual = (observado.get(cat.id, Decimal('0')) / num_meses)
        if declarado == 0 and obs_mensual == 0:
            continue
        diferencia = obs_mensual - declarado
        pct = int(min(obs_mensual / declarado * 100, 999)) if declarado > 0 else 0
        filas.append({
            'categoria': cat.nombre,
            'tipo': cat.get_tipo_display(),
            'declarado': declarado,
            'observado': obs_mensual,
            'diferencia': diferencia,
            'pct': pct,
        })
        total_declarado += declarado
        total_observado += obs_mensual

    filas.sort(key=lambda f: f['observado'], reverse=True)

    return render(request, 'extractos/conciliacion.html', {
        'filas': filas,
        'num_meses': num_meses,
        'sin_categorizar_importe': sin_cat / num_meses if sin_cat else Decimal('0'),
        'total_declarado': total_declarado,
        'total_observado': total_observado,
        'total_diferencia': total_observado - total_declarado,
        'hay_datos': movimientos.exists(),
    })


# ---------------------------------------------------------------------------
# Aprendizaje de comercios
#
# La idea: en vez de corregir el mismo comercio una y otra vez, el usuario lo
# nombra UNA vez y ese criterio se aplica a todos los movimientos parecidos (los
# ya importados y los que vengan) guardándolo como ReglaCategorizacion.
# ---------------------------------------------------------------------------

def _aplicar_patron(hogar, patron, categoria, incluir_categorizados=False):
    """Asigna `categoria` a los movimientos del hogar cuyo comercio o concepto
    contenga `patron`. Devuelve cuántos ha actualizado.

    Por defecto solo toca lo que está sin categorizar, para que aprender una
    regla nueva no pise clasificaciones que el usuario ya había dado por buenas.
    """
    patron = normalizar_texto(patron)
    if not patron:
        return 0

    candidatos = MovimientoBancario.objects.filter(hogar=hogar, es_traspaso=False)
    if not incluir_categorizados:
        candidatos = candidatos.filter(categoria__isnull=True)

    # El filtrado va en Python porque hay que comparar contra el texto
    # normalizado (sin acentos ni signos), que no es lo que hay en la columna.
    ids = [
        m.id for m in candidatos.only('id', 'comercio', 'concepto')
        if patron in (m.comercio or '') or patron in normalizar_texto(m.concepto)
    ]
    if not ids:
        return 0

    MovimientoBancario.objects.filter(id__in=ids).update(
        categoria=categoria, estado_categorizacion='por_regla',
    )
    return len(ids)


@login_required
def sin_categorizar(request):
    """Agrupa por comercio todo lo que quedó sin categorizar y permite asignar
    la categoría a un grupo entero de una vez, recordándolo para el futuro."""
    profile, hogar = _get_hogar(request)
    if not hogar:
        messages.error(request, "Necesitas pertenecer a un hogar.")
        return redirect('dashboard')

    _crear_categorias_predefinidas(hogar)

    pendientes = list(
        MovimientoBancario.objects.filter(
            hogar=hogar, categoria__isnull=True, importe__lt=0, es_traspaso=False,
        ).order_by('-fecha')
    )

    grupos_por_comercio = defaultdict(list)
    for mov in pendientes:
        grupos_por_comercio[mov.comercio or normalizar_comercio(mov.concepto)].append(mov)

    claves = list(grupos_por_comercio.keys())
    grupos = []
    for clave, movs in grupos_por_comercio.items():
        # El nombre que se enseña es el concepto más repetido del grupo: es el
        # que el usuario reconoce, no la clave normalizada.
        conteo = defaultdict(int)
        for m in movs:
            conteo[m.concepto] += 1
        etiqueta = max(conteo.items(), key=lambda kv: kv[1])[0]

        similares = [
            otra for otra in difflib.get_close_matches(clave, claves, n=5, cutoff=UMBRAL_SIMILITUD)
            if otra != clave
        ]
        grupos.append({
            'comercio': clave,
            'etiqueta': etiqueta,
            'num': len(movs),
            'total': sum((m.importe for m in movs), Decimal('0')),
            'desde': min(m.fecha for m in movs),
            'hasta': max(m.fecha for m in movs),
            'conceptos': sorted({m.concepto for m in movs})[:5],
            'similares': [
                {'comercio': s, 'num': len(grupos_por_comercio[s])} for s in similares
            ],
        })

    # Lo que más pesa primero: así unas pocas decisiones cubren la mayor parte
    # del gasto sin clasificar.
    grupos.sort(key=lambda g: g['total'])

    return render(request, 'extractos/sin_categorizar.html', {
        'grupos': grupos,
        'total_movimientos': len(pendientes),
        'total_importe': sum((m.importe for m in pendientes), Decimal('0')),
        'categorias_hogar': CategoriaGasto.objects.filter(
            hogar=hogar, activo=True,
        ).order_by('tipo', 'nombre'),
        'num_reglas': ReglaCategorizacion.objects.filter(hogar=hogar, activo=True).count(),
    })


@login_required
def aprender_regla(request):
    """Crea (o actualiza) una regla y la aplica a los movimientos que encajen.

    Es el punto único que usan tanto la pantalla «Sin categorizar» como el aviso
    que sale al cambiar la categoría en el listado."""
    profile, hogar = _get_hogar(request)
    if not hogar:
        return JsonResponse({'ok': False, 'error': 'sin_hogar'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'metodo'}, status=405)

    es_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    patrones = [normalizar_texto(p) for p in request.POST.getlist('patron') if normalizar_texto(p)]
    categoria = CategoriaGasto.objects.filter(
        hogar=hogar, id=request.POST.get('categoria_id') or 0,
    ).first()

    if not patrones or not categoria:
        if es_ajax:
            return JsonResponse({'ok': False, 'error': 'datos_incompletos'}, status=400)
        messages.error(request, "Indica un patrón y una categoría.")
        return redirect('extractos:sin_categorizar')

    incluir = request.POST.get('incluir_categorizados') == '1'
    aplicados = 0
    for patron in patrones:
        ReglaCategorizacion.objects.update_or_create(
            hogar=hogar, patron=patron,
            defaults={'categoria': categoria, 'origen': 'manual', 'activo': True},
        )
        aplicados += _aplicar_patron(hogar, patron, categoria, incluir)

    if es_ajax:
        return JsonResponse({'ok': True, 'aplicados': aplicados, 'categoria': categoria.nombre})

    messages.success(
        request,
        f"{aplicados} movimiento(s) categorizados como «{categoria.nombre}». "
        "Se aplicará automáticamente en las próximas importaciones."
    )
    return redirect('extractos:sin_categorizar')


@login_required
def reglas(request):
    """Gestión de las reglas aprendidas: sin esto no habría forma de deshacer
    una regla mal aprendida."""
    profile, hogar = _get_hogar(request)
    if not hogar:
        messages.error(request, "Necesitas pertenecer a un hogar.")
        return redirect('dashboard')

    if request.method == 'POST':
        accion = request.POST.get('accion')
        regla = get_object_or_404(
            ReglaCategorizacion, pk=request.POST.get('regla_id'), hogar=hogar,
        )
        if accion == 'eliminar':
            regla.delete()
            messages.success(request, "Regla eliminada. Los movimientos ya categorizados no cambian.")
        elif accion == 'alternar':
            regla.activo = not regla.activo
            regla.save(update_fields=['activo'])
            messages.success(
                request,
                f"Regla «{regla.patron}» {'activada' if regla.activo else 'desactivada'}.",
            )
        elif accion == 'recategorizar':
            categoria = CategoriaGasto.objects.filter(
                hogar=hogar, id=request.POST.get('categoria_id') or 0,
            ).first()
            if categoria:
                regla.categoria = categoria
                regla.save(update_fields=['categoria'])
                aplicados = _aplicar_patron(hogar, regla.patron, categoria, incluir_categorizados=True)
                messages.success(
                    request,
                    f"Regla actualizada; {aplicados} movimiento(s) recategorizados como «{categoria.nombre}».",
                )
        return redirect('extractos:reglas')

    return render(request, 'extractos/reglas.html', {
        'reglas': ReglaCategorizacion.objects.filter(hogar=hogar).select_related('categoria'),
        'categorias_hogar': CategoriaGasto.objects.filter(
            hogar=hogar, activo=True,
        ).order_by('tipo', 'nombre'),
    })


@login_required
def eliminar(request, pk):
    profile, hogar = _get_hogar(request)
    if not hogar:
        return redirect('dashboard')
    extracto = get_object_or_404(ExtractoBancario, pk=pk, hogar=hogar)
    if request.method == 'POST':
        extracto.delete()
        messages.success(request, "Extracto eliminado.")
    return redirect('extractos:listar')
