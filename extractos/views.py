import difflib
from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from finanzas import costes_activo
from finanzas.models import CategoriaGasto, CuentaBancaria, PartidaGasto
from finanzas.parsing import leer_tabla
from finanzas.models import COMPUTO_NEUTRO, ETIQUETAS_TIPO, ORDEN_TIPOS, TIPOS_GASTO
from finanzas.views_gastos import CATEGORIA_TRASPASO, _crear_categorias_predefinidas

from .analisis import analizar_mes
from .categorizacion import categorizar_lote
from .models import Etiqueta, ExtractoBancario, MovimientoBancario, ReglaCategorizacion
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
        .select_related('categoria', 'partida_conciliada')
        .prefetch_related('etiquetas').order_by('-fecha')
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


def _analizar_pendientes(pendientes, mapeos_manuales=None, hogar=None):
    """Ejecuta analizar_extracto sobre cada archivo pendiente de la sesión.

    mapeos_manuales: {indice_archivo(str): {campo: valor}} con las
    correcciones de mapeo enviadas desde el formulario de revisión.

    Si se pasa `hogar`, cada movimiento se marca con `ya_existe` para poder
    enseñar en la revisión qué se va a omitir por duplicado ANTES de importar.
    """
    mapeos_manuales = mapeos_manuales or {}
    analizados = []
    for i, pend in enumerate(pendientes):
        mapeo = mapeos_manuales.get(str(i))
        resultado = analizar_extracto(pend['texto'], mapeo_manual=mapeo)
        analizados.append({'nombre': pend['nombre'], 'resultado': resultado})
    if hogar is not None:
        _marcar_duplicados(hogar, analizados)
    return analizados


def _marcar_duplicados(hogar, analizados):
    """Anota `hash` y `ya_existe` en cada movimiento analizado.

    Comprueba contra la base de datos y también dentro del propio lote, para que
    subir dos veces el mismo archivo (o dos exportaciones que se solapan) no
    cuente el mismo apunte como nuevo dos veces."""
    hashes = {}
    for item in analizados:
        for mov in item['resultado']['movimientos']:
            mov['hash'] = MovimientoBancario.calcular_hash(
                mov['fecha'], mov['concepto'], mov['importe'], mov['saldo'],
            )
            hashes.setdefault(mov['hash'], []).append(mov)

    if not hashes:
        return
    existentes = set(
        MovimientoBancario.objects.filter(
            hogar=hogar, hash_dedupe__in=list(hashes),
        ).values_list('hash_dedupe', flat=True)
    )
    for hash_mov, movs in hashes.items():
        ya_en_bd = hash_mov in existentes
        for pos, mov in enumerate(movs):
            # El primero del lote es nuevo salvo que ya estuviera en la BD; los
            # siguientes con el mismo hash son repeticiones dentro del lote.
            mov['ya_existe'] = ya_en_bd or pos > 0


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
    cat_traspaso = CategoriaGasto.objects.filter(
        hogar=hogar, nombre=CATEGORIA_TRASPASO, activo=True,
    ).first()

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

        # Los traspasos entre cuentas propias van a su propia categoría: el
        # mismo movimiento aparece en negativo en una cuenta y en positivo en la
        # otra, así que su neto es cero y no debe mezclarse ni con el gasto ni
        # con el ingreso.
        traspasos = [es_traspaso_interno(m['concepto'], nombres_hogar) for m in movimientos]

        # Se categoriza en bloque para cargar las reglas del hogar una sola vez.
        # Gasto e ingreso usan diccionarios distintos (el signo cambia el
        # significado del mismo texto).
        categorizables = [
            ('', False) if t else (m['concepto'], m['importe'] >= 0)
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
            if es_traspaso:
                categoria, origen = cat_traspaso, 'codigo'
                total_traspasos += 1
            if categoria:
                # Distinguir el acierto del diccionario del criterio propio del
                # usuario (regla aprendida), que antes se marcaban igual.
                estado = 'por_regla' if origen == 'regla' else 'por_codigo'
                total_categorizados += 1

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
        analizados = _analizar_pendientes(pendientes, mapeos_manuales, hogar=hogar)

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
        analizados = _analizar_pendientes(pendientes, hogar=hogar)

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
            'total_nuevos': sum(1 for m in r['movimientos'] if not m.get('ya_existe')),
            'total_duplicados': sum(1 for m in r['movimientos'] if m.get('ya_existe')),
            'total_error': len(r['filas_error']),
            'total_omitidas': len(r.get('filas_omitidas', [])),
            'preview': r['movimientos'][:15],
            'preview_restantes': max(0, len(r['movimientos']) - 15),
            'filas_error': r['filas_error'][:20],
            'filas_error_restantes': max(0, len(r['filas_error']) - 20),
        })

    total_ok = sum(a['total_ok'] for a in archivos_ctx)
    total_error = sum(a['total_error'] for a in archivos_ctx)
    total_nuevos = sum(a['total_nuevos'] for a in archivos_ctx)
    total_duplicados = sum(a['total_duplicados'] for a in archivos_ctx)

    return render(request, 'extractos/revisar.html', {
        'archivos': archivos_ctx,
        'cuentas': cuentas,
        'nombre_banco': meta.get('nombre_banco', ''),
        'cuenta_id': meta.get('cuenta_id'),
        'total_ok': total_ok,
        'total_error': total_error,
        'total_nuevos': total_nuevos,
        'total_duplicados': total_duplicados,
    })


# Color de cada bloque del presupuesto. Los tres primeros coinciden con los que
# ya usa la pantalla de Gastos, para que un bloque se reconozca en ambos sitios.
COLOR_TIPO = {
    'fijo': '#b7791f',
    'anual': '#2c5f7a',
    'variable': '#3DCD58',
    'discrecional': '#9d4edd',
    'ingreso': '#2d6a4f',
    'traspaso': '#5f8fb0',
    'sin': '#9aa5a0',
}

# Paleta para el donut de categorías (verdes/tierra coherentes con la marca).
_PALETA = [
    '#2d6a4f', '#3DCD58', '#2c5f7a', '#b7791f', '#b4442e', '#1b4332',
    '#40916c', '#5f8fb0', '#d4a017', '#9d4edd', '#e07a5f', '#81b29a',
]


def _etiqueta_periodo(anio_sel, mes_sel):
    """«Julio 2026», «2026» o «Todo el histórico»: el resumen necesita decir de
    qué periodo habla, o las cifras no significan nada."""
    mes = _entero_o_none(mes_sel)
    anio = _entero_o_none(anio_sel)
    if mes is not None and 1 <= mes <= 12:
        nombre_mes = MESES_ES[mes]
        return f"{nombre_mes} {anio}" if anio else f"{nombre_mes} (todos los años)"
    if anio:
        return str(anio)
    return "Todo el histórico"


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
    # Bloque y etiqueta llegan desde el drill-down del análisis y de la
    # conciliación («enséñame los movimientos que hay detrás de esta cifra»).
    bloque_sel = request.GET.get('bloque', '')
    etiqueta_sel = request.GET.get('etiqueta', '')
    activo_sel = request.GET.get('activo', '')
    # Buscador libre: con cientos de apuntes, encontrar «ese recibo raro» a ojo
    # es lo que hace que la pantalla se sienta un muro de números.
    busqueda = (request.GET.get('q') or '').strip()
    busqueda_norm = normalizar_texto(busqueda)
    # Los movimientos neutros (traspasos entre cuentas propias y cualquier otra
    # categoría marcada como neutra) siguen apareciendo en el listado, pero se
    # pueden ocultar porque no aportan nada al análisis de gasto.
    ver_traspasos = request.GET.get('traspasos') != '0'

    def pasa_filtro(m):
        if m.es_neutro and not ver_traspasos:
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
        if bloque_sel:
            tipo = m.categoria.tipo if m.categoria else 'sin'
            if tipo != bloque_sel:
                return False
        if etiqueta_sel:
            if etiqueta_sel not in {str(e.id) for e in m.etiquetas.all()}:
                return False
        if activo_sel and m.clave_activo != activo_sel:
            return False
        if busqueda_norm:
            texto = f"{m.comercio or ''} {normalizar_texto(m.concepto)}"
            if busqueda_norm not in texto:
                return False
        return True

    movimientos = [m for m in todos if pasa_filtro(m)]

    # --- KPIs sobre el conjunto filtrado ---
    # Quién suma, quién resta y quién no cuenta lo dice el cómputo de la
    # categoría, no el signo del importe: los traspasos entre cuentas propias
    # (y cualquier categoría marcada como neutra) salen en negativo pero no son
    # gasto, y sumarlos inflaba el gasto del mes.
    reales = [m for m in movimientos if not m.es_neutro]
    ingresos = sum((m.importe for m in reales if m.cuenta_como_ingreso), Decimal('0'))
    gastos = sum((m.importe for m in reales if m.cuenta_como_gasto), Decimal('0'))
    sin_categorizar = sum(1 for m in movimientos if not m.categoria_id and not m.es_neutro)
    traspasos = [m for m in movimientos if m.es_neutro]
    traspaso_neto = sum((m.importe for m in traspasos), Decimal('0'))

    # --- Donut: gasto por categoría (valores absolutos) ---
    # Un abono dentro de una categoría de gasto (una devolución de Amazon) resta
    # de su propia categoría en vez de contarse como ingreso, así que el total
    # del bloque es el gasto neto de esa categoría.
    por_categoria = defaultdict(lambda: Decimal('0'))
    for m in reales:
        if m.cuenta_como_gasto:
            nombre = m.categoria.nombre if m.categoria else 'Sin categorizar'
            por_categoria[nombre] += -m.importe
    cat_ordenadas = sorted(
        ((nombre, importe) for nombre, importe in por_categoria.items() if importe > 0),
        key=lambda kv: kv[1], reverse=True,
    )
    total_gasto_abs = sum((v for _, v in cat_ordenadas), Decimal('0'))

    # --- Desglose por categoría superior (los bloques del presupuesto) ---
    por_tipo = defaultdict(lambda: Decimal('0'))
    for m in reales:
        if m.cuenta_como_gasto:
            tipo = m.categoria.tipo if m.categoria else 'sin'
            por_tipo[tipo] += -m.importe
    bloques = []
    for tipo in list(ORDEN_TIPOS) + ['sin']:
        if por_tipo.get(tipo, Decimal('0')) <= 0:
            continue
        importe = por_tipo[tipo]
        bloques.append({
            'tipo': tipo,
            'etiqueta': ETIQUETAS_TIPO.get(tipo, 'Sin categorizar'),
            'importe': importe,
            'pct': round(float(importe / total_gasto_abs * 100), 1) if total_gasto_abs else 0,
            'color': COLOR_TIPO.get(tipo, '#9aa5a0'),
        })

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
    grupos_mes = defaultdict(lambda: {
        'movimientos': [], 'ingresos': Decimal('0'),
        'gastos': Decimal('0'), 'neutro': Decimal('0'),
    })
    for m in movimientos:
        g = grupos_mes[(m.fecha.year, m.fecha.month)]
        g['movimientos'].append(m)
        # Mismo criterio que los KPIs: manda el cómputo de la categoría, para
        # que el neto del mes no cuente los traspasos como gasto.
        if m.es_neutro:
            g['neutro'] += m.importe
        elif m.cuenta_como_ingreso:
            g['ingresos'] += m.importe
        else:
            g['gastos'] += m.importe

    grupos = []
    for (anio, mes), datos in sorted(grupos_mes.items(), reverse=True):
        grupos.append({
            'etiqueta': f"{MESES_ES[mes]} {anio}",
            'ingresos': datos['ingresos'],
            'gastos': datos['gastos'],
            'neutro': datos['neutro'],
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
        'busqueda': busqueda,
        'bloque_sel': bloque_sel,
        'bloque_etiqueta': ETIQUETAS_TIPO.get(bloque_sel, 'Sin categorizar') if bloque_sel else '',
        'etiqueta_sel': etiqueta_sel,
        'etiquetas_hogar': Etiqueta.objects.filter(hogar=hogar),
        'activo_sel': activo_sel,
        'grupos_activos': costes_activo.opciones(hogar),
        'provisiones': PartidaGasto.objects.filter(
            hogar=hogar, activo=True,
        ).exclude(periodicidad='mensual').select_related('categoria'),
        'periodo_etiqueta': _etiqueta_periodo(anio_sel, mes_sel),
        'colores_tipo': COLOR_TIPO,
        'num_traspasos': sum(1 for m in todos if m.es_neutro),
        'kpi_traspaso_neto': traspaso_neto,
        'traspasos_cuadran': traspaso_neto == 0 and bool(traspasos),
        'bloques': bloques,
        'categorias_hogar': categorias_hogar,
        'bloques_categorias': _categorias_por_bloque(hogar),
        'hay_filtro': (
            anio_sel != 'all' or mes_sel != 'all' or cat_sel != 'all'
            or bool(busqueda) or bool(bloque_sel) or bool(etiqueta_sel)
            or bool(activo_sel)
        ),
    }


@login_required
def detalle(request, pk):
    profile, hogar = _get_hogar(request)
    if not hogar:
        messages.error(request, "Necesitas pertenecer a un hogar.")
        return redirect('dashboard')

    extracto = get_object_or_404(ExtractoBancario, pk=pk, hogar=hogar)
    todos = list(
        extracto.movimientos.select_related('categoria', 'partida_conciliada')
        .prefetch_related('etiquetas').all()
    )
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
    # movimientos del mismo comercio. Solo se ofrece: nada se aplica ni se
    # recuerda hasta que el usuario elige el alcance.
    if mov.categoria_id and mov.comercio:
        respuesta['sugerencia'] = _sugerencia_similares(hogar, mov)

    return JsonResponse(respuesta)


def _sugerencia_similares(hogar, mov):
    """Cuántos movimientos cambiarían al aplicar este criterio al comercio, en
    el mes del movimiento y en total.

    Se cuenta con el MISMO criterio con el que luego se aplica (`_encajan`), o
    los números del aviso no cuadrarían con lo que acaba pasando: «MERCADONA
    SEVILLA NERVION» también encaja en el patrón «mercadona sevilla».

    Se cuentan también los que ya tienen OTRA categoría: cuando se corrige un
    comercio, lo normal es querer corregir todo lo que se clasificó mal antes,
    no solo lo que quedó en blanco."""
    similares = [
        m for m in _encajan(hogar, mov.comercio, incluir_categorizados=True)
        if m.pk != mov.pk and m.categoria_id != mov.categoria_id
    ]

    n_total = len(similares)
    if not n_total:
        return None

    n_mes = sum(
        1 for m in similares
        if m.fecha.year == mov.fecha.year and m.fecha.month == mov.fecha.month
    )
    n_ya_clasificados = sum(1 for m in similares if m.categoria_id)

    return {
        'patron': mov.comercio,
        'categoria_id': mov.categoria_id,
        'categoria': mov.categoria.nombre if mov.categoria else '',
        'n_similares': n_total,
        'n_mes': n_mes,
        'n_ya_clasificados': n_ya_clasificados,
        'anio': mov.fecha.year,
        'mes': mov.fecha.month,
        'etiqueta_mes': f"{MESES_ES[mov.fecha.month]} {mov.fecha.year}",
        # Si ya existe la regla, no tiene sentido volver a ofrecer recordarla.
        'ya_hay_regla': ReglaCategorizacion.objects.filter(
            hogar=hogar, patron=mov.comercio, categoria=mov.categoria, activo=True,
        ).exists(),
    }


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


def _periodo_conciliacion(request, movimientos):
    """Periodo que se está conciliando, a partir de los filtros de la URL.

    Por defecto se abre en el ÚLTIMO MES CON DATOS: comparar el presupuesto
    contra una media de doce meses esconde justo lo que se quiere ver, que es
    si este mes se ha ido de madre. La media sigue disponible eligiendo «todos».
    """
    meses_con_datos = sorted({(m.fecha.year, m.fecha.month) for m in movimientos}, reverse=True)
    anios = sorted({anio for anio, _ in meses_con_datos}, reverse=True)

    anio_sel = request.GET.get('anio')
    mes_sel = request.GET.get('mes')
    if anio_sel is None and mes_sel is None and meses_con_datos:
        anio_sel, mes_sel = (str(v) for v in meses_con_datos[0])
    anio_sel = anio_sel or 'all'
    mes_sel = mes_sel or 'all'

    anio = _entero_o_none(anio_sel)
    mes = _entero_o_none(mes_sel)
    if mes is not None and not 1 <= mes <= 12:
        mes, mes_sel = None, 'all'

    def dentro(m):
        if anio is not None and m.fecha.year != anio:
            return False
        if mes is not None and m.fecha.month != mes:
            return False
        return True

    del_periodo = [m for m in movimientos if dentro(m)]
    meses_periodo = {(m.fecha.year, m.fecha.month) for m in del_periodo}
    # Un mes concreto se enseña tal cual; varios meses, en media mensual, que es
    # la única forma de compararlos con un presupuesto que es mensual.
    es_mes = anio is not None and mes is not None

    if es_mes:
        etiqueta = f"{MESES_ES[mes]} {anio}"
    elif anio is not None:
        etiqueta = f"{anio}"
    else:
        etiqueta = "Todo el histórico"

    return {
        'movimientos': del_periodo,
        'num_meses': 1 if es_mes else max(len(meses_periodo), 1),
        'es_mes': es_mes,
        'etiqueta': etiqueta,
        'anio_sel': anio_sel,
        'mes_sel': mes_sel,
        'anios_disponibles': anios,
        'meses_disponibles': [
            {'valor': str(n), 'etiqueta': MESES_ES[n]} for n in range(1, 13)
        ],
    }


@login_required
def conciliacion(request):
    """Cruza los movimientos observados (gasto) contra lo declarado en Gastos."""
    profile, hogar = _get_hogar(request)
    if not hogar:
        messages.error(request, "Necesitas pertenecer a un hogar.")
        return redirect('dashboard')

    _crear_categorias_predefinidas(hogar)

    # Se descartan los movimientos neutros (traspasos entre cuentas propias y
    # categorías marcadas como neutras): no son gasto ni ingreso, así que no
    # tienen nada contra lo que compararse en el presupuesto.
    todos = [
        m for m in MovimientoBancario.objects.filter(hogar=hogar)
        .select_related('categoria', 'partida_conciliada')
        if not m.es_neutro
    ]
    periodo = _periodo_conciliacion(request, todos)
    movimientos = periodo['movimientos']
    num_meses = periodo['num_meses']

    # Los gastos que no son mensuales se comparan en el bloque anual, no aquí:
    # se provisionan mes a mes y se pagan de golpe, así que dejar el pago del
    # IBI dentro de junio haría parecer un desastre ese mes y un dechado de
    # virtud los otros once. Se sacan sus pagos del observado Y su provisión
    # del declarado, o la comparación quedaría coja por un lado.
    #
    # Solo en la vista de UN MES: sobre doce meses ambos lados se promedian
    # bien y la comparación vuelve a tener sentido tal cual.
    solo_mes = periodo['es_mes']
    pagos_provision = [m for m in movimientos if m.es_pago_provision]
    gastos = [
        m for m in movimientos
        if m.cuenta_como_gasto and not (solo_mes and m.es_pago_provision)
    ]
    total_provisiones_periodo = sum((-m.importe for m in pagos_provision), Decimal('0'))

    # Observado por categoría (gasto absoluto, media mensual).
    observado = defaultdict(lambda: Decimal('0'))
    sin_cat = Decimal('0')
    for m in gastos:
        if m.categoria_id:
            observado[m.categoria_id] += -m.importe
        else:
            sin_cat += -m.importe

    # Declarado por categoría: suma de importe_mensual de sus partidas activas.
    # Se agrupa por BLOQUE del presupuesto (Fijos / Fijos anuales / Variables /
    # Discrecionales), que es la comparación que de verdad interesa: la
    # categoría concreta se conserva como detalle dentro de cada bloque.
    por_bloque = {}
    # Toda categoría con gasto DECLARADO entra en la comparación, sea cual sea
    # su bloque o su cómputo: si se ha presupuestado, se concilia. El filtro por
    # bloque solo decide qué categorías sin presupuesto se cuelan por tener
    # gasto observado.
    categorias = CategoriaGasto.objects.filter(hogar=hogar).filter(
        Q(partidas__activo=True)
        | Q(activo=True, tipo__in=TIPOS_GASTO) & ~Q(computo=COMPUTO_NEUTRO)
    ).distinct().prefetch_related('partidas')
    total_declarado = Decimal('0')
    total_observado = Decimal('0')

    for cat in categorias:
        suyas = cat.partidas.filter(activo=True)
        if solo_mes:
            suyas = suyas.filter(periodicidad='mensual')
        declarado = sum((p.importe_mensual for p in suyas), Decimal('0'))
        obs_mensual = (observado.get(cat.id, Decimal('0')) / num_meses)
        if declarado == 0 and obs_mensual == 0:
            continue
        bloque = por_bloque.setdefault(cat.tipo, {
            'tipo': cat.tipo,
            'etiqueta': ETIQUETAS_TIPO.get(cat.tipo, cat.tipo),
            'color': COLOR_TIPO.get(cat.tipo, '#9aa5a0'),
            'filas': [],
            'declarado': Decimal('0'),
            'observado': Decimal('0'),
        })
        bloque['filas'].append({
            'categoria': cat.nombre,
            'categoria_id': cat.id,
            'declarado': declarado,
            'observado': obs_mensual,
            'diferencia': obs_mensual - declarado,
            'pct': int(min(obs_mensual / declarado * 100, 999)) if declarado > 0 else 0,
        })
        bloque['declarado'] += declarado
        bloque['observado'] += obs_mensual
        total_declarado += declarado
        total_observado += obs_mensual

    bloques = []
    for tipo in ORDEN_TIPOS:
        bloque = por_bloque.get(tipo)
        if not bloque:
            continue
        bloque['filas'].sort(key=lambda f: f['observado'], reverse=True)
        bloque['diferencia'] = bloque['observado'] - bloque['declarado']
        bloque['pct'] = (
            int(min(bloque['observado'] / bloque['declarado'] * 100, 999))
            if bloque['declarado'] > 0 else 0
        )
        bloques.append(bloque)

    # --- Ingresos: observado en el banco frente a lo declarado en FuenteIngreso ---
    ingreso_observado = sum(
        (m.importe for m in movimientos if m.cuenta_como_ingreso), Decimal('0'),
    ) / num_meses
    ingreso_declarado = _ingreso_declarado_mensual(hogar)

    return render(request, 'extractos/conciliacion.html', {
        'bloques': bloques,
        'periodo': periodo,
        'pagos_provision': (
            sorted(pagos_provision, key=lambda m: m.fecha, reverse=True) if solo_mes else []
        ),
        'total_provisiones_periodo': total_provisiones_periodo,
        'provisiones': _provisiones_del_anio(hogar, periodo, todos),
        'num_meses': num_meses,
        'sin_categorizar_importe': sin_cat / num_meses if sin_cat else Decimal('0'),
        'total_declarado': total_declarado,
        'total_observado': total_observado,
        'total_diferencia': total_observado - total_declarado,
        'ingreso_declarado': ingreso_declarado,
        'ingreso_observado': ingreso_observado,
        'ingreso_diferencia': ingreso_observado - ingreso_declarado,
        'ahorro_declarado': ingreso_declarado - total_declarado,
        'ahorro_observado': ingreso_observado - total_observado,
        'hay_datos': bool(movimientos),
        'hay_movimientos': bool(todos),
    })


@login_required
def analisis(request):
    """En qué se ha ido el mes y qué lo explica.

    Es el destino del drill-down de la conciliación: se entra desde «Ocio se ha
    pasado 180 €» y aquí se ve contra qué se compara, qué comercios lo componen
    y cuánto de eso volverá a pasar el mes que viene."""
    profile, hogar = _get_hogar(request)
    if not hogar:
        messages.error(request, "Necesitas pertenecer a un hogar.")
        return redirect('dashboard')

    todos = list(
        MovimientoBancario.objects.filter(hogar=hogar)
        .select_related('categoria').prefetch_related('etiquetas')
    )

    anio, mes = _mes_analizado(request, todos)
    bloque = request.GET.get('bloque') or ''
    categoria_id = _entero_o_none(request.GET.get('categoria'))
    etiqueta_id = _entero_o_none(request.GET.get('etiqueta'))

    categoria = (
        CategoriaGasto.objects.filter(hogar=hogar, id=categoria_id).first()
        if categoria_id else None
    )
    etiqueta = (
        Etiqueta.objects.filter(hogar=hogar, id=etiqueta_id).first()
        if etiqueta_id else None
    )
    if bloque not in ETIQUETAS_TIPO and bloque != 'sin':
        bloque = ''

    datos = analizar_mes(
        todos, anio, mes,
        bloque=bloque or None,
        categoria_id=categoria.id if categoria else None,
        etiqueta_id=etiqueta.id if etiqueta else None,
    )

    meses_con_datos = sorted({(m.fecha.year, m.fecha.month) for m in todos}, reverse=True)
    return render(request, 'extractos/analisis.html', {
        'a': datos,
        'etiqueta_mes': f"{MESES_ES[mes]} {anio}",
        'bloque': bloque,
        'bloque_etiqueta': ETIQUETAS_TIPO.get(bloque, 'Sin categorizar') if bloque else '',
        'categoria': categoria,
        'etiqueta': etiqueta,
        'color_bloque': COLOR_TIPO.get(bloque, '#9aa5a0'),
        'colores_tipo': COLOR_TIPO,
        'hay_ambito': bool(bloque or categoria or etiqueta),
        'meses_disponibles': [
            {'anio': a, 'mes': m, 'etiqueta': f"{MESES_ES[m]} {a}"} for a, m in meses_con_datos
        ],
        'hay_movimientos': bool(todos),
        'etiquetas_hogar': Etiqueta.objects.filter(hogar=hogar),
    })


def _mes_analizado(request, movimientos):
    """Mes elegido en la URL o, si no viene, el último con datos."""
    anio = _entero_o_none(request.GET.get('anio'))
    mes = _entero_o_none(request.GET.get('mes'))
    if anio and mes and 1 <= mes <= 12:
        return anio, mes

    meses = sorted({(m.fecha.year, m.fecha.month) for m in movimientos}, reverse=True)
    if meses:
        return meses[0]
    hoy = date.today()
    return hoy.year, hoy.month


# ---------------------------------------------------------------------------
# Etiquetas
#
# Cruzan las categorías en vez de competir con ellas: una cena del viaje es
# «Restaurantes» Y «Vacaciones Lisboa». Sin esto, analizar un gasto puntual
# obliga a inventar categorías que acaban siendo un cajón de sastre.
# ---------------------------------------------------------------------------

@login_required
def etiquetar_movimiento(request, pk):
    """Añade o quita una etiqueta de un movimiento. Si el nombre es nuevo, se
    crea la etiqueta: obligar a crearla antes rompería el gesto."""
    profile, hogar = _get_hogar(request)
    if not hogar:
        return JsonResponse({'ok': False, 'error': 'sin_hogar'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'metodo'}, status=405)

    mov = get_object_or_404(MovimientoBancario, pk=pk, hogar=hogar)
    quitar = _entero_o_none(request.POST.get('quitar'))
    if quitar:
        mov.etiquetas.remove(quitar)
        return JsonResponse({'ok': True, 'etiquetas': _etiquetas_de(mov)})

    nombre = (request.POST.get('nombre') or '').strip()[:60]
    if not nombre:
        return JsonResponse({'ok': False, 'error': 'nombre_vacio'}, status=400)

    etiqueta = Etiqueta.objects.filter(hogar=hogar, nombre__iexact=nombre).first()
    if not etiqueta:
        etiqueta = Etiqueta.objects.create(
            hogar=hogar, nombre=nombre, color=Etiqueta.color_sugerido(hogar),
        )
    mov.etiquetas.add(etiqueta)

    # Al etiquetar a mano se ofrece aplicar la etiqueta a los movimientos del
    # mismo comercio, igual que con las categorías: un viaje no se etiqueta
    # apunte a apunte.
    similares = MovimientoBancario.objects.filter(
        hogar=hogar, comercio=mov.comercio,
    ).exclude(pk=mov.pk).exclude(etiquetas=etiqueta).count() if mov.comercio else 0

    return JsonResponse({
        'ok': True,
        'etiquetas': _etiquetas_de(mov),
        'sugerencia': {
            'etiqueta_id': etiqueta.id, 'nombre': etiqueta.nombre,
            'comercio': mov.comercio, 'n_similares': similares,
        } if similares else None,
    })


def _etiquetas_de(mov):
    return [
        {'id': e.id, 'nombre': e.nombre, 'color': e.color}
        for e in mov.etiquetas.all()
    ]


@login_required
def etiquetar_comercio(request):
    """Aplica una etiqueta a todos los movimientos de un comercio."""
    profile, hogar = _get_hogar(request)
    if not hogar:
        return JsonResponse({'ok': False, 'error': 'sin_hogar'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'metodo'}, status=405)

    etiqueta = get_object_or_404(
        Etiqueta, pk=request.POST.get('etiqueta_id') or 0, hogar=hogar,
    )
    comercio = (request.POST.get('comercio') or '').strip()
    if not comercio:
        return JsonResponse({'ok': False, 'error': 'comercio_vacio'}, status=400)

    movimientos = MovimientoBancario.objects.filter(hogar=hogar, comercio=comercio)
    aplicados = 0
    for mov in movimientos.exclude(etiquetas=etiqueta):
        mov.etiquetas.add(etiqueta)
        aplicados += 1
    return JsonResponse({'ok': True, 'aplicados': aplicados, 'etiqueta': etiqueta.nombre})


@login_required
def imputar_movimiento(request, pk):
    """Marca un movimiento como gasto de un vehículo o de una propiedad.

    Es la pata REAL de «cuánto me cuesta el coche»: sin esto, la ficha del
    vehículo solo sabría lo presupuestado."""
    profile, hogar = _get_hogar(request)
    if not hogar:
        return JsonResponse({'ok': False, 'error': 'sin_hogar'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'metodo'}, status=405)

    mov = get_object_or_404(MovimientoBancario, pk=pk, hogar=hogar)
    clave = request.POST.get('activo') or ''
    activo = costes_activo.resolver(hogar, clave)
    if clave and not activo:
        return JsonResponse({'ok': False, 'error': 'activo_invalido'}, status=400)

    costes_activo.asignar(mov, activo)
    mov.save(update_fields=['vehiculo', 'propiedad'])

    # Mismo patrón que categorías y etiquetas: los gastos de un coche vienen
    # casi siempre del mismo puñado de comercios, así que se ofrece aplicarlo a
    # todos de una vez en vez de apunte a apunte.
    similares = 0
    if activo and mov.comercio:
        campo = 'vehiculo' if clave.startswith('vehiculo') else 'propiedad'
        similares = MovimientoBancario.objects.filter(
            hogar=hogar, comercio=mov.comercio,
        ).exclude(pk=mov.pk).exclude(**{campo: activo}).count()

    return JsonResponse({
        'ok': True,
        'activo': str(activo) if activo else None,
        'clave': clave if activo else '',
        'sugerencia': {
            'clave': clave, 'nombre': str(activo),
            'comercio': mov.comercio, 'n_similares': similares,
        } if similares else None,
    })


@login_required
def marcar_provision(request, pk):
    """Marca un movimiento como uno de los pagos de un gasto no mensual.

    Es lo que evita que el mes en el que cae el IBI parezca un desastre y los
    otros once un dechado de virtud."""
    profile, hogar = _get_hogar(request)
    if not hogar:
        return JsonResponse({'ok': False, 'error': 'sin_hogar'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'metodo'}, status=405)

    mov = get_object_or_404(MovimientoBancario, pk=pk, hogar=hogar)
    partida_id = _entero_o_none(request.POST.get('partida_id'))
    partida = None
    if partida_id:
        partida = PartidaGasto.objects.filter(
            hogar=hogar, id=partida_id, activo=True,
        ).exclude(periodicidad='mensual').first()
        if not partida:
            return JsonResponse({'ok': False, 'error': 'partida_invalida'}, status=400)

    mov.partida_conciliada = partida
    # Un pago de provisión hereda la categoría del gasto declarado: si no, el
    # mismo apunte contaría en un sitio y en otro no.
    if partida and not mov.categoria_id:
        mov.categoria = partida.categoria
        mov.estado_categorizacion = 'manual'
    mov.save()

    return JsonResponse({
        'ok': True,
        'partida': partida.nombre if partida else None,
        'partida_id': partida.id if partida else None,
    })


@login_required
def imputar_comercio(request):
    """Imputa al mismo activo todos los movimientos de un comercio."""
    profile, hogar = _get_hogar(request)
    if not hogar:
        return JsonResponse({'ok': False, 'error': 'sin_hogar'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'metodo'}, status=405)

    activo = costes_activo.resolver(hogar, request.POST.get('activo') or '')
    comercio = (request.POST.get('comercio') or '').strip()
    if not activo or not comercio:
        return JsonResponse({'ok': False, 'error': 'datos_incompletos'}, status=400)

    aplicados = 0
    for mov in MovimientoBancario.objects.filter(hogar=hogar, comercio=comercio):
        costes_activo.asignar(mov, activo)
        mov.save(update_fields=['vehiculo', 'propiedad'])
        aplicados += 1

    return JsonResponse({'ok': True, 'aplicados': aplicados, 'activo': str(activo)})


@login_required
def etiquetas(request):
    """Gestión de etiquetas: renombrar, recolorear y borrar.

    Borrar una etiqueta no toca los movimientos: solo deja de cruzarlos."""
    profile, hogar = _get_hogar(request)
    if not hogar:
        messages.error(request, "Necesitas pertenecer a un hogar.")
        return redirect('dashboard')

    if request.method == 'POST':
        accion = request.POST.get('accion')
        if accion == 'crear':
            nombre = (request.POST.get('nombre') or '').strip()[:60]
            if not nombre:
                messages.error(request, "El nombre no puede estar vacío.")
            elif Etiqueta.objects.filter(hogar=hogar, nombre__iexact=nombre).exists():
                messages.warning(request, f"Ya existe la etiqueta «{nombre}».")
            else:
                Etiqueta.objects.create(
                    hogar=hogar, nombre=nombre,
                    color=request.POST.get('color') or Etiqueta.color_sugerido(hogar),
                )
                messages.success(request, f"Etiqueta «{nombre}» creada.")
            return redirect('extractos:etiquetas')

        etiqueta = get_object_or_404(Etiqueta, pk=request.POST.get('etiqueta_id'), hogar=hogar)
        if accion == 'eliminar':
            nombre = etiqueta.nombre
            etiqueta.delete()
            messages.success(request, f"Etiqueta «{nombre}» eliminada. Los movimientos no se tocan.")
        elif accion == 'editar':
            nombre = (request.POST.get('nombre') or '').strip()[:60]
            choque = Etiqueta.objects.filter(
                hogar=hogar, nombre__iexact=nombre,
            ).exclude(pk=etiqueta.pk).exists()
            if not nombre:
                messages.error(request, "El nombre no puede estar vacío.")
            elif choque:
                messages.error(request, f"Ya existe otra etiqueta llamada «{nombre}».")
            else:
                etiqueta.nombre = nombre
                etiqueta.color = request.POST.get('color') or etiqueta.color
                etiqueta.save(update_fields=['nombre', 'color'])
                messages.success(request, "Etiqueta actualizada.")
        return redirect('extractos:etiquetas')

    filas = []
    for etiqueta in Etiqueta.objects.filter(hogar=hogar).prefetch_related('movimientos'):
        movimientos = list(etiqueta.movimientos.all())
        gasto = sum((-m.importe for m in movimientos if m.importe < 0), Decimal('0'))
        filas.append({
            'etiqueta': etiqueta,
            'num': len(movimientos),
            'gasto': gasto,
            'desde': min((m.fecha for m in movimientos), default=None),
            'hasta': max((m.fecha for m in movimientos), default=None),
        })
    filas.sort(key=lambda f: f['gasto'], reverse=True)

    return render(request, 'extractos/etiquetas.html', {
        'filas': filas,
        'paleta': Etiqueta.PALETA,
        'color_sugerido': Etiqueta.color_sugerido(hogar),
    })


def _provisiones_del_anio(hogar, periodo, movimientos):
    """Los gastos no mensuales del hogar y cuánto llevas pagado de cada uno.

    La conciliación mensual no puede responder «¿ya he pagado el IBI?» porque
    su unidad es el mes; esta sí: el año entero, pago a pago.
    """
    anio = _entero_o_none(periodo['anio_sel'])
    partidas = (
        PartidaGasto.objects.filter(hogar=hogar, activo=True)
        .exclude(periodicidad='mensual').select_related('categoria')
    )
    if not partidas:
        return []

    pagos = defaultdict(list)
    for m in movimientos:
        if not m.es_pago_provision:
            continue
        if anio and m.fecha.year != anio:
            continue
        pagos[m.partida_conciliada_id].append(m)

    filas = []
    for p in partidas:
        suyos = pagos.get(p.id, [])
        pagado = sum((-m.importe for m in suyos), Decimal('0'))
        objetivo = p.importe_anual
        filas.append({
            'partida': p,
            'categoria': p.categoria.nombre if p.categoria else '—',
            'periodicidad': p.get_periodicidad_display(),
            'objetivo': objetivo,
            'pagado': pagado,
            'pendiente': max(objetivo - pagado, Decimal('0')),
            'num_pagos': len(suyos),
            'pagos': sorted(suyos, key=lambda m: m.fecha),
            'pct': int(min(pagado / objetivo * 100, 100)) if objetivo > 0 else 0,
            'completo': objetivo > 0 and pagado >= objetivo,
        })
    filas.sort(key=lambda f: (f['completo'], -float(f['objetivo'])))
    return filas


def _ingreso_declarado_mensual(hogar):
    """Ingreso neto mensual declarado por el hogar en sus FuenteIngreso.

    Reutiliza `_neto_fuente_base` del motor de distribución para no duplicar el
    cálculo de retenciones y reparto en pagas."""
    from finanzas.distribucion import _neto_fuente_base
    from finanzas.models import FuenteIngreso

    total = Decimal('0')
    for miembro in hogar.miembros.select_related('user').all():
        for fuente in FuenteIngreso.objects.filter(
            usuario=miembro.user, hogar=hogar, activo=True,
        ):
            base, _ = _neto_fuente_base(fuente)
            total += base
    return total


# ---------------------------------------------------------------------------
# Aprendizaje de comercios
#
# La idea: en vez de corregir el mismo comercio una y otra vez, el usuario lo
# nombra UNA vez y ese criterio se aplica a todos los movimientos parecidos (los
# ya importados y los que vengan) guardándolo como ReglaCategorizacion.
# ---------------------------------------------------------------------------

def _categorias_por_bloque(hogar):
    """Categorías del hogar agrupadas por su bloque del presupuesto, en el orden
    de presentación. Se usa para los selectores, de modo que al clasificar un
    movimiento se vea a qué bloque irá a parar."""
    grupos = defaultdict(list)
    for cat in CategoriaGasto.objects.filter(hogar=hogar, activo=True).order_by('nombre'):
        grupos[cat.tipo].append(cat)
    return [
        {'tipo': tipo, 'etiqueta': ETIQUETAS_TIPO.get(tipo, tipo), 'categorias': grupos[tipo]}
        for tipo in ORDEN_TIPOS if grupos.get(tipo)
    ]


def _encajan(hogar, patron, incluir_categorizados=False, anio=None, mes=None):
    """Movimientos del hogar cuyo comercio o concepto contiene `patron`.

    Es el criterio ÚNICO de «este movimiento es de ese comercio»: lo usan tanto
    el aviso que cuenta cuántos hay como la aplicación que los cambia, para que
    el número que se ofrece sea exactamente el que acaba cambiando.

    Por defecto solo mira lo que está sin categorizar, para que aprender una
    regla nueva no pise clasificaciones que el usuario ya había dado por buenas.
    Con `anio` y `mes` se acota a ese mes: el caso de quien revisa un mes
    concreto y no quiere tocar el histórico.
    """
    patron = normalizar_texto(patron)
    if not patron:
        return []

    candidatos = MovimientoBancario.objects.filter(hogar=hogar, es_traspaso=False)
    if not incluir_categorizados:
        candidatos = candidatos.filter(categoria__isnull=True)
    if anio and mes:
        candidatos = candidatos.filter(fecha__year=anio, fecha__month=mes)

    # El filtrado va en Python porque hay que comparar contra el texto
    # normalizado (sin acentos ni signos), que no es lo que hay en la columna.
    return [
        m for m in candidatos.only('id', 'comercio', 'concepto', 'fecha', 'categoria')
        if patron in (m.comercio or '') or patron in normalizar_texto(m.concepto)
    ]


def _aplicar_patron(hogar, patron, categoria, incluir_categorizados=False,
                    anio=None, mes=None):
    """Asigna `categoria` a los movimientos que encajan con `patron`.
    Devuelve cuántos ha actualizado."""
    ids = [
        m.id for m in _encajan(hogar, patron, incluir_categorizados, anio, mes)
        if m.categoria_id != categoria.id
    ]
    if not ids:
        return 0

    MovimientoBancario.objects.filter(id__in=ids).update(
        categoria=categoria, estado_categorizacion='por_regla',
    )
    return len(ids)


def _entero_o_none(valor):
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None


@login_required
def sin_categorizar(request):
    """Agrupa por comercio todo lo que quedó sin categorizar y permite asignar
    la categoría a un grupo entero de una vez, recordándolo para el futuro."""
    profile, hogar = _get_hogar(request)
    if not hogar:
        messages.error(request, "Necesitas pertenecer a un hogar.")
        return redirect('dashboard')

    _crear_categorias_predefinidas(hogar)

    # Incluye también los positivos: una nómina o una devolución sin clasificar
    # es tan invisible en el análisis como un gasto sin clasificar.
    pendientes = list(
        MovimientoBancario.objects.filter(
            hogar=hogar, categoria__isnull=True, es_traspaso=False,
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
        total = sum((m.importe for m in movs), Decimal('0'))
        grupos.append({
            'comercio': clave,
            'etiqueta': etiqueta,
            'num': len(movs),
            'total': total,
            'es_ingreso': total >= 0,
            'desde': min(m.fecha for m in movs),
            'hasta': max(m.fecha for m in movs),
            'conceptos': sorted({m.concepto for m in movs})[:5],
            'similares': [
                {'comercio': s, 'num': len(grupos_por_comercio[s])} for s in similares
            ],
        })

    # Primero el gasto y dentro de cada bloque lo que más pesa: así unas pocas
    # decisiones cubren la mayor parte de lo que falta por clasificar.
    grupos.sort(key=lambda g: (g['es_ingreso'], -abs(g['total'])))

    return render(request, 'extractos/sin_categorizar.html', {
        'grupos': grupos,
        'total_movimientos': len(pendientes),
        'total_importe': sum((m.importe for m in pendientes), Decimal('0')),
        'bloques_categorias': _categorias_por_bloque(hogar),
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

    # «Solo recordar»: crea la regla sin tocar ningún movimiento. Es el segundo
    # paso del aviso del listado — primero se aplica el cambio con el alcance
    # elegido y después se pregunta si además debe quedar así para siempre.
    if request.POST.get('accion') == 'solo_regla':
        for patron in patrones:
            ReglaCategorizacion.objects.update_or_create(
                hogar=hogar, patron=patron,
                defaults={'categoria': categoria, 'origen': 'manual', 'activo': True},
            )
        if es_ajax:
            return JsonResponse({
                'ok': True, 'aplicados': 0,
                'categoria': categoria.nombre, 'recordada': True,
            })
        messages.success(
            request,
            f"«{categoria.nombre}» se aplicará automáticamente a este comercio "
            "en las próximas importaciones.",
        )
        return redirect('extractos:sin_categorizar')

    # Alcance del cambio. 'mes' lo acota al mes que se está revisando; por
    # defecto se aplica a todo el histórico, que es lo que hacía siempre.
    solo_mes = request.POST.get('ambito') == 'mes'
    anio = _entero_o_none(request.POST.get('anio')) if solo_mes else None
    mes = _entero_o_none(request.POST.get('mes')) if solo_mes else None
    if solo_mes and not (anio and mes):
        if es_ajax:
            return JsonResponse({'ok': False, 'error': 'mes_invalido'}, status=400)
        messages.error(request, "No se ha podido identificar el mes a corregir.")
        return redirect('extractos:sin_categorizar')

    # Aplicar y recordar son decisiones distintas: el listado aplica primero y
    # pregunta después si además debe quedarse como regla. La pantalla de «Sin
    # categorizar», donde nombrar el comercio ES la acción, sigue recordando por
    # defecto.
    recordar = request.POST.get('recordar', '0' if solo_mes else '1') == '1'

    aplicados = 0
    for patron in patrones:
        if recordar:
            ReglaCategorizacion.objects.update_or_create(
                hogar=hogar, patron=patron,
                defaults={'categoria': categoria, 'origen': 'manual', 'activo': True},
            )
        aplicados += _aplicar_patron(hogar, patron, categoria, incluir, anio, mes)

    if es_ajax:
        return JsonResponse({
            'ok': True, 'aplicados': aplicados,
            'categoria': categoria.nombre, 'recordada': recordar,
        })

    messages.success(
        request,
        f"{aplicados} movimiento(s) categorizados como «{categoria.nombre}»."
        + (" Se aplicará automáticamente en las próximas importaciones." if recordar else "")
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
        'bloques_categorias': _categorias_por_bloque(hogar),
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
