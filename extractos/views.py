import calendar
import difflib
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.dateparse import parse_date

from finanzas import costes_activo, presupuesto
from finanzas.models import CategoriaGasto, CuentaBancaria, PartidaGasto
from finanzas.parsing import leer_tabla
from finanzas.models import COMPUTO_NEUTRO, ETIQUETAS_TIPO, ORDEN_TIPOS, TIPOS_GASTO
from finanzas.views_gastos import CATEGORIA_TRASPASO, _crear_categorias_predefinidas

from . import reparto
from .anuales import analizar_anuales, guardar_calendario
from .analisis import MINIMO_MESES_REFERENCIA, UMBRAL_RECURRENTE, analizar_mes
from .categorizacion import categorizar, categorizar_lote
from .models import (
    Etiqueta, ExtractoBancario, MovimientoBancario, ReglaCategorizacion, ReglaDivision,
)
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

# A partir de cuántos movimientos filtrados se cargan los meses al desplegarlos
# en vez de pintarlos todos de golpe. Trescientos es aproximadamente un trimestre
# de una cuenta normal: por debajo, la página entera sigue siendo pequeña.
UMBRAL_DIFERIR_MESES = 300

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
        .select_related('categoria', 'partida_conciliada', 'cubre', 'reembolsa', 'dividido_de')
        .prefetch_related('etiquetas', 'partes', 'coberturas',
                          'dividido_de__partes', 'dividido_de__coberturas', 'reembolsos', 'dividido_de__reembolsos').order_by('-fecha')
    )
    # Al entrar sin periodo en la URL, la pantalla se abre en el MES EN CURSO.
    destino = _abrir_en_el_mes_en_curso(request, todos)
    if destino:
        return redirect(destino)

    panel = _panel_context(hogar, todos, request)

    return render(request, 'extractos/listar.html', {
        'panel': panel,
        'extractos': extractos,
        'total_extractos': extractos.count(),
        'total_movimientos_en_extractos': sum(1 for m in todos if m.extracto_id),
        'total_movimientos': len(todos),
    })


def _abrir_en_el_mes_en_curso(request, movimientos):
    """URL a la que saltar cuando se entra en Movimientos sin periodo elegido.

    Lo que uno viene a mirar al abrir Extractos es el mes en curso, no los tres
    años importados: sin periodo la pantalla arrancaba en «todo el histórico» y
    lo primero que había que hacer siempre era elegir año y mes a mano.

    Se redirige en vez de rellenarlo por dentro para que el periodo quede
    ESCRITO en la URL: las pestañas, el modal de una categoría y el despliegue
    de un mes lo leen de ahí, y si no estuviera puesto cada uno miraría un
    periodo distinto del que enseña la cabecera.

    Si el mes en curso no tiene ni un apunte se abre en el último que sí lo
    tenga: una pantalla vacía no dice nada y obliga justo al trasteo del que
    esto viene a librar.

    El resto de filtros que vinieran en la URL se conservan, para que un enlace
    como «?categoria=sin» siga significando lo que dice.
    """
    if 'anio' in request.GET or 'mes' in request.GET:
        return None
    hoy = date.today()
    periodo = (hoy.year, hoy.month)
    if not any((m.fecha.year, m.fecha.month) == periodo for m in movimientos):
        con_datos = {(m.fecha.year, m.fecha.month) for m in movimientos}
        if not con_datos:
            return None
        periodo = max(con_datos)
    parametros = request.GET.copy()
    parametros['anio'], parametros['mes'] = str(periodo[0]), str(periodo[1])
    return f'{request.path}?{parametros.urlencode()}'


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
    total_divididos = 0
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
        # Y lo que sea de varias partidas se parte solo. Va DESPUÉS de crear el
        # extracto para no contar las partes como movimientos importados: no
        # vienen del banco, salen de un criterio que puso el usuario.
        total_divididos += _aplicar_divisiones(hogar, extracto)

    return {
        'total_creados': total_creados,
        'total_duplicados': total_duplicados,
        'total_categorizados': total_categorizados,
        'total_omitidos': total_omitidos,
        'total_no_firmes': total_no_firmes,
        'total_traspasos': total_traspasos,
        'total_divididos': total_divididos,
        'extractos_ok': extractos_ok,
    }


def _aplicar_divisiones(hogar, extracto):
    """Parte los movimientos recién importados que encajen en una división
    aprendida. Devuelve cuántos ha partido.

    El total no cambia nunca —las partes suman el cobro— así que lo peor que
    puede pasar es que la forma del reparto no acierte. Se ve en la lista con
    sus partes debajo y se deshace de un clic, igual que una categoría puesta
    por una regla que no era.
    """
    from django.db.models import F

    reglas = list(
        ReglaDivision.objects.filter(hogar=hogar, activo=True)
        .prefetch_related('partes__categoria', 'partes__vehiculo', 'partes__propiedad')
    )
    if not reglas:
        return 0

    divididos = 0
    usos = defaultdict(int)
    for mov in extracto.movimientos.all():
        regla = _mejor_division(mov, reglas)
        if regla and reparto.aplicar_regla(regla, mov):
            usos[regla.pk] += 1
            divididos += 1

    for pk, veces in usos.items():
        ReglaDivision.objects.filter(pk=pk).update(
            veces_aplicada=F('veces_aplicada') + veces,
        )
    return divididos


def _mejor_division(movimiento, reglas):
    """La división aprendida que le toca a un movimiento, o None.

    Dos criterios, en este orden:

    1. El patrón más específico, igual que en las reglas de categoría: si hay
       uno para «norauto» y otro para «norauto sevilla», manda el segundo.
    2. Dentro de ese patrón, la VERSIÓN que regía el día del recibo. Elegir el
       patrón primero y la versión después —y no al revés— es lo que hace que
       fechar un reparto no cambie a qué comercio pertenece el recibo.
    """
    texto = normalizar_texto(movimiento.concepto)
    por_patron = defaultdict(list)
    for regla in reglas:
        patron = normalizar_texto(regla.patron)
        if patron and patron in texto:
            por_patron[patron].append(regla)
    if not por_patron:
        return None

    mejor = max(por_patron, key=len)
    return reparto.version_vigente(por_patron[mejor], movimiento.fecha)


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
            if totales['total_divididos']:
                messages.info(
                    request,
                    f"{totales['total_divididos']} recibo(s) se han repartido solos con la "
                    "misma forma que les diste. El total no cambia; si el reparto no "
                    "encaja esta vez, en su fila puedes deshacerlo o corregirlo."
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


def _leer_filtros(request, **overrides):
    """Los filtros activos de la pantalla de movimientos, en un solo sitio.

    Los lee una vez y los devuelve como diccionario para que el panel, el
    desglose de una categoría y cualquier cálculo derivado usen EXACTAMENTE el
    mismo criterio: en cuanto hay dos copias de esta lectura, la media de una
    tarjeta deja de cuadrar con la lista que tiene debajo.

    `overrides` permite acotar sin tocar la URL: así el modal de una categoría
    reutiliza los filtros de la pantalla y solo cambia la categoría.
    """
    filtros = {
        'anio': request.GET.get('anio', 'all'),
        'mes': request.GET.get('mes', 'all'),
        'categoria': request.GET.get('categoria', 'all'),
        # Bloque y etiqueta llegan desde el drill-down de la conciliación
        # («enséñame los movimientos que hay detrás de esta cifra»).
        'bloque': request.GET.get('bloque', ''),
        'etiqueta': request.GET.get('etiqueta', ''),
        'activo': request.GET.get('activo', ''),
        # Buscador libre: con cientos de apuntes, encontrar «ese recibo raro» a
        # ojo es lo que hace que la pantalla se sienta un muro de números.
        'busqueda': (request.GET.get('q') or '').strip(),
        # Los movimientos neutros (traspasos entre cuentas propias y cualquier
        # otra categoría marcada como neutra) siguen apareciendo en el listado,
        # pero se pueden ocultar porque no aportan nada al análisis de gasto.
        'ver_traspasos': request.GET.get('traspasos') != '0',
    }
    filtros.update(overrides)
    filtros['busqueda_norm'] = normalizar_texto(filtros['busqueda'])
    return filtros


def _pasa_periodo(m, f):
    """Solo el corte temporal. Se usa aparte para contar los meses sobre los que
    se promedia: si Gasolina no se repostó en marzo, marzo sigue siendo un mes
    del periodo y tiene que contar como cero en la media."""
    # Se compara como texto porque los filtros llegan de la URL; `str()` a los
    # dos lados deja además que un override pase el año como número.
    if f['anio'] != 'all' and str(m.fecha.year) != str(f['anio']):
        return False
    if f['mes'] != 'all' and str(m.fecha.month) != str(f['mes']):
        return False
    return True


def _pasa_filtro(m, f):
    # Un reembolso es neutro —no es ingreso— pero no es ruido como un traspaso:
    # es la mitad de la historia de un gasto compartido y tiene que seguir a la
    # vista aunque se oculten los neutros.
    if m.es_neutro and not m.es_reembolso and not f['ver_traspasos']:
        return False
    if not _pasa_periodo(m, f):
        return False
    cat = f['categoria']
    if cat != 'all':
        if cat == 'sin':
            # Un cobro REPARTIDO se queda sin categoría a propósito: el que
            # cuenta es cada parte, y son ellas las que llevan la suya. Ponerle
            # una al padre sumaría el mismo dinero dos veces, así que la única
            # salida honesta es que desaparezca de aquí: si sale en «sin
            # categorizar», la lista pide clasificar algo que ya está resuelto
            # y nunca se vacía.
            if m.categoria_id is not None or m.esta_dividido:
                return False
        elif str(m.categoria_id) != str(cat):
            return False
    if f['bloque']:
        tipo = m.categoria.tipo if m.categoria else 'sin'
        if tipo != f['bloque']:
            return False
    if f['etiqueta']:
        if str(f['etiqueta']) not in {str(e.id) for e in m.etiquetas.all()}:
            return False
    if f['activo'] and m.clave_activo != f['activo']:
        return False
    if f['busqueda_norm']:
        texto = f"{m.comercio or ''} {normalizar_texto(m.concepto)}"
        if f['busqueda_norm'] not in texto:
            return False
    return True


def _contexto_edicion(hogar):
    """Lo que necesita una fila de movimiento para poder editarse: categorías,
    colores, provisiones y activos.

    Va aparte porque las filas se pintan en dos sitios —el listado y el modal de
    una categoría— y tienen que ofrecer los mismos desplegables en los dos."""
    return {
        'colores_tipo': COLOR_TIPO,
        'bloques_categorias': _categorias_por_bloque(hogar),
        'provisiones': PartidaGasto.objects.filter(
            hogar=hogar, activo=True,
        ).exclude(periodicidad='mensual').select_related('categoria'),
        'grupos_activos': costes_activo.opciones(hogar),
        'etiquetas_hogar': Etiqueta.objects.filter(hogar=hogar),
        # Para que el alta a mano venga con la fecha de hoy puesta: el caso de
        # uso es apuntar lo que acabas de pagar en efectivo.
        'fecha_hoy': date.today(),
    }


def _comercios_del_periodo(movimientos, meses_con_datos, tope=14, vista_de_mes=False):
    """Ranking de comercios de lo que se está mirando: cuánto, cuántas veces y
    de cuánto cada vez.

    El ticket medio es la mitad del diagnóstico: ocho pedidos de 24 € y una cena
    de 190 € pesan parecido en el total y no son el mismo problema. Se calcula
    sobre los movimientos YA FILTRADOS, así que respeta el año, el mes, la
    categoría y el buscador que haya puestos.

    `vista_de_mes` pesa los pagos por lo que la reserva no cubrió, para que este
    ranking sume lo mismo que el resto de la pantalla.
    """
    grupos = defaultdict(list)
    for m in movimientos:
        if not m.cuenta_como_gasto or m.es_neutro:
            continue
        grupos[m.comercio or 'otros'].append(m)

    filas = []
    for comercio, movs in grupos.items():
        total = sum(
            ((m.impacto_real if vista_de_mes else -m.importe_neto) for m in movs), Decimal('0'),
        )
        if total <= 0:
            continue
        conceptos = defaultdict(int)
        for m in movs:
            conceptos[m.concepto] += 1
        meses_visto = len({(m.fecha.year, m.fecha.month) for m in movs})
        filas.append({
            'comercio': comercio,
            # El concepto más repetido es el que el usuario reconoce, no la
            # clave normalizada con la que se agrupa.
            'etiqueta': max(conceptos.items(), key=lambda kv: kv[1])[0],
            'categoria': movs[0].categoria.nombre if movs[0].categoria else 'Sin categorizar',
            'num': len(movs),
            'total': total,
            'ticket_medio': total / len(movs),
            'meses_visto': meses_visto,
            # Sin al menos dos meses a la vista no se moja: llamar «puntual» a
            # algo de lo que solo se ve un mes sería mentir con seguridad.
            'recurrente': (
                meses_con_datos >= MINIMO_MESES_REFERENCIA
                and meses_visto >= UMBRAL_RECURRENTE * meses_con_datos
            ),
        })

    filas.sort(key=lambda f: f['total'], reverse=True)
    resto = filas[tope:]
    filas = filas[:tope]
    tope_importe = max((f['total'] for f in filas), default=Decimal('0'))
    for f in filas:
        f['pct'] = float(f['total'] / tope_importe * 100) if tope_importe else 0
    return {
        'filas': filas,
        'num_resto': len(resto),
        'total_resto': sum((f['total'] for f in resto), Decimal('0')),
    }


def _contra_el_limite(neto, bruto, cubierto, limite, es_anual,
                      prorrateado=Decimal('0'), anterior=Decimal('0')):
    """Cómo se lee una fila del reparto frente a su límite.

    Tres cosas que `presupuesto.estado` por sí solo no puede decidir:

    * QUÉ se compara. En los bloques normales, lo que pesó en el mes (ya
      descontada la reserva) MÁS la parte que le toca de los gastos anuales que
      ya has pagado: el límite del mes lleva dentro la provisión de ese seguro,
      así que si el pago no aparece por ningún lado la fila miente por los dos
      lados. En los ANUALES, el pago entero: lo que consume la provisión del
      año es el recibo completo, lo pagues con la hucha o no.
    * CÓMO se pinta. La barra se parte en tres: lo que carga el mes, con su
      color de dentro/fuera; a continuación —en discontinuo— la doceava parte
      del gasto anual ya pagado, que no es dinero de este mes pero sí ocupa su
      presupuesto; y —rayado— lo que puso la reserva. Así se ve de un vistazo
      que el bloque no está casi vacío porque no se gastara, sino porque el
      gasto ya estaba pagado de antes.
    * CUÁNTO del año. Un anual mirado en un mes se compara con lo que llevas
      pagado en el año HASTA ese mes (`anterior` son los meses de antes): con
      solo el mes, septiembre decía «1.202 de 3.130» cuando el año ya iba por
      2.400, y parecía que quedaba provisión que ya se había gastado. Lo de
      meses anteriores va primero en la barra, en gris.
    """
    if not es_anual:
        anterior = Decimal('0')
    medido = bruto + anterior if es_anual else neto + prorrateado
    estado = presupuesto.estado(medido, limite)
    pct_anterior = 0

    if limite and limite > 0:
        pct_anterior = min(float(anterior / limite * 100), 100) if anterior > 0 else 0
        pct_neto = (
            min(float(neto / limite * 100), 100 - pct_anterior) if neto > 0 else 0
        )
        hueco = max(0.0, 100 - pct_anterior - pct_neto)
        pct_prorrateado = (
            min(float(prorrateado / limite * 100), hueco) if prorrateado > 0 else 0
        )
        hueco = max(0.0, hueco - pct_prorrateado)
        pct_cubierto = min(float(cubierto / limite * 100), hueco) if cubierto > 0 else 0
    elif cubierto > 0 and bruto > 0:
        # Sin límite no hay contra qué medir, pero la barra sigue teniendo algo
        # que contar: qué parte del pago puso la reserva. Repartida sobre el
        # pago, la barra se llena entera y el rayado se ve. Antes esto daba un
        # rayado del 0%: la fila decía en texto que la reserva había puesto 904 €
        # y en la gráfica no aparecía por ningún lado.
        pct_cubierto = min(float(cubierto / bruto * 100), 100)
        pct_neto = 100 - pct_cubierto
        pct_prorrateado = 0
    else:
        # Sin límite y sin reserva la barra se llena entera: dejarla a medias
        # sugeriría un techo que nadie ha puesto.
        pct_neto = 100 if neto > 0 else 0
        pct_cubierto = 0
        # Sin nada gastado este mes, lo prorrateado es lo único que hay que
        # enseñar: si no, la fila del seguro ya pagado saldría con la barra a
        # cero y parecería que no le pesa nada al mes.
        pct_prorrateado = 100 if (not pct_neto and prorrateado > 0) else 0

    estado['pct_barra'] = round(pct_neto, 1)
    estado['pct_anterior'] = round(pct_anterior, 1)
    estado['anterior'] = anterior
    estado['pct_prorrateado'] = round(pct_prorrateado, 1)
    estado['pct_cubierto'] = round(pct_cubierto, 1)
    estado['medido'] = medido
    estado['prorrateado'] = prorrateado
    return estado


def _pct_transcurrido(f, meses_periodo):
    """Qué parte del periodo que se está mirando ya ha pasado, o None.

    Es la marca sobre la barra del presupuesto: llevar gastado el 60 % el día 5
    y llevarlo el día 28 son dos noticias distintas, y sin la referencia la
    barra no distingue una de otra.

    Solo se moja con un MES concreto, que es la vista con la que se abre la
    pantalla. Sobre varios meses el tope se escala a los meses con datos y no
    hay una fracción de tiempo que le corresponda; poner una sería inventarse
    una referencia.
    """
    anio, mes = _entero_o_none(f['anio']), _entero_o_none(f['mes'])
    if anio is None or mes is None or not 1 <= mes <= 12:
        return None
    hoy = date.today()
    if (anio, mes) < (hoy.year, hoy.month):
        return 100
    if (anio, mes) > (hoy.year, hoy.month):
        return 0
    return round(hoy.day / calendar.monthrange(hoy.year, hoy.month)[1] * 100, 1)


def _anuales_prorrateados(todos, f):
    """Lo que los gastos ANUALES ya pagados le pesan al mes que se está mirando.

    El seguro del coche se paga de una vez en enero, pero se presupuesta a
    razón de una doceava parte cada mes: el límite de «Seguro coche» de
    septiembre ya lleva esa provisión dentro. Y el pago no aparece por ningún
    lado —en los once meses restantes porque no cae ahí, y en enero porque se
    saca de los totales para no decir que enero fue un desastre—, así que la
    fila enseñaba 60 € de 142 € y parecía que sobraban ochenta euros que en
    realidad ya estaban gastados.

    Aquí el pago se reparte entre los doce meses del año y cada mes carga su
    parte, marcada aparte para poder pintarla en discontinuo: no es dinero que
    saliera este mes, es dinero que ya salió y que ocupa el presupuesto de
    este mes igual.

    Solo tiene sentido mirando UN MES de UN AÑO concreto. Con «todos los años»
    o con el año entero a la vista los pagos ya cuentan enteros y prorratearlos
    los contaría dos veces.

    Se reparte lo que SE SACÓ del mes en el que cayó: un pago del que dijiste
    cuánto puso la reserva se queda entero en su mes, con el peso que le
    corresponda, y prorratearlo además sería contarlo dos veces.
    """
    vacio = {'por_categoria': {}, 'por_bloque': {}, 'total': Decimal('0'),
             'pagos': [], 'meses': []}
    anio = _entero_o_none(f['anio'])
    if anio is None or f['mes'] == 'all':
        return vacio

    # Mismos filtros de la pantalla, pero sobre el AÑO entero: el pago que se
    # busca es justo el que no está en el mes que se está mirando.
    del_anio = dict(f, mes='all')

    por_categoria, por_bloque = {}, defaultdict(lambda: Decimal('0'))
    pagos, total = [], Decimal('0')
    for m in todos:
        # Solo lo que se SACÓ de su mes. Un pago que dijiste que salió del
        # bolsillo —«sin reserva»— ya cuenta entero donde cayó.
        if not m.se_saca_del_mes or m.es_neutro:
            continue
        if not _pasa_filtro(m, del_anio):
            continue
        cuota = (-m.importe_neto / 12).quantize(Decimal('0.01'))
        if cuota <= 0:
            continue
        tipo = m.categoria.tipo if m.categoria else 'sin'
        nombre = m.categoria.nombre if m.categoria else 'Sin categorizar'
        fila = por_categoria.setdefault(m.categoria_id, {
            'id': m.categoria_id, 'nombre': nombre, 'tipo': tipo,
            'importe': Decimal('0'), 'pagado': Decimal('0'), 'meses': set(),
        })
        fila['importe'] += cuota
        fila['pagado'] += -m.importe_neto
        fila['meses'].add(m.fecha.month)
        por_bloque[tipo] += cuota
        total += cuota
        pagos.append(m)

    for fila in por_categoria.values():
        fila['meses'] = sorted(fila['meses'])

    return {
        'por_categoria': por_categoria,
        'por_bloque': dict(por_bloque),
        'total': total,
        'pagos': sorted(pagos, key=lambda m: m.fecha),
        'meses': sorted({m.fecha.month for m in pagos}),
    }


def _anuales_meses_anteriores(todos, f):
    """Lo pagado en FIJOS ANUALES en el mismo año, antes del mes que se mira.

    Por categoría (clave: su nombre, como en el reparto). Recibos enteros, con
    o sin reserva: lo que consume la provisión del año es el pago completo.
    Vacío si no se está mirando un mes concreto de un año concreto: con el año
    entero a la vista esos pagos ya están dentro.
    """
    anio, mes = _entero_o_none(f['anio']), _entero_o_none(f['mes'])
    if anio is None or mes is None:
        return {}
    del_anio = dict(f, mes='all')
    por_categoria = {}
    for m in todos:
        if m.fecha.year != anio or m.fecha.month >= mes:
            continue
        if not m.categoria or m.categoria.tipo != 'anual':
            continue
        if not m.cuenta_como_gasto or m.es_neutro or not _pasa_filtro(m, del_anio):
            continue
        fila = por_categoria.setdefault(
            m.categoria.nombre, {'id': m.categoria_id, 'importe': Decimal('0')},
        )
        fila['importe'] += -m.importe_neto
    return por_categoria


def _fuera_de_presupuesto(bloques):
    """Lo que se ha salido del presupuesto, a partir de los bloques ya
    calculados: el bloque que se pasa y, dentro, qué categoría lo explica.

    En DISCRECIONALES solo se avisa del bloque. Ahí no hay —ni tiene sentido
    que haya— un presupuesto por categoría: el usuario sabe que tiene un tope
    para sus caprichos y quiere ver en qué se le ha ido, no que le riñan por
    gastar 40 € en una categoría para la que nunca declaró un límite.
    """
    excedidos, explican, sin_limite = [], [], []
    for b in bloques:
        if b['dentro'] is False:
            excedidos.append({
                'tipo': b['tipo'], 'nombre': b['etiqueta'], 'color': b['color'],
                # `medido` y no `importe`: en los anuales lo que se compara con
                # el límite es el pago entero, así que enseñar el neto al lado
                # del exceso daría dos cifras que no se restan entre sí.
                'importe': b.get('medido', b['importe']), 'limite': b['limite'],
                'exceso': b['exceso'], 'num_categorias': b['num_categorias'],
                'categorias': b['categorias'][:5],
            })
        # Ni en discrecionales ni en ningún bloque cuyo límite se declare
        # entero: ahí el presupuesto es del bloque, y avisar por categoría sería
        # reprochar haberse pasado de un límite que nadie puso.
        if b['tipo'] == 'discrecional' or b.get('techo_propio'):
            continue
        for c in b['categorias']:
            fila = dict(
                c, bloque=b['etiqueta'], color=b['color'], tipo=b['tipo'],
                importe=c.get('medido', c['importe']),
            )
            if c['dentro'] is False:
                explican.append(fila)
            elif c['dentro'] is None and c['importe'] > 0 and c['id']:
                sin_limite.append(fila)

    excedidos.sort(key=lambda f: f['exceso'], reverse=True)
    explican.sort(key=lambda f: f['exceso'], reverse=True)
    sin_limite.sort(key=lambda f: f['importe'], reverse=True)

    # Las pasadas y las que no tienen límite van en UNA lista y a la misma
    # escala. Antes las segundas vivían plegadas debajo, en letra pequeña: 504 €
    # de imprevistos sin presupuesto se veían menos que 1 € de exceso en el
    # seguro del hogar. Se distinguen por el color y la etiqueta, no por
    # esconder unas. La escala es el gasto (o el límite) mayor de la lista, para
    # que el real y la marca del límite se lean uno contra otro.
    for f in explican:
        f['estado'] = 'pasada'
    for f in sin_limite:
        f['estado'] = 'sin_limite'
        f['exceso'] = Decimal('0')
    # Se ordenan por lo que queda FUERA de lo previsto: en una pasada, lo que
    # excede su límite; en una sin límite, todo, porque nada de ese gasto estaba
    # en el plan. Es la misma pregunta para las dos —cuánto dinero no contaba
    # con gastar— y así 504 € de imprevistos van delante de 1 € de exceso.
    for f in explican + sin_limite:
        f['no_previsto'] = f['exceso'] if f['estado'] == 'pasada' else f['importe']
    revisar = sorted(explican + sin_limite, key=lambda f: f['no_previsto'], reverse=True)
    tope = max(
        (max(f['importe'], f.get('limite') or Decimal('0')) for f in revisar),
        default=Decimal('0'),
    )
    for f in revisar:
        f['pct_real'] = float(f['importe'] / tope * 100) if tope else 0
        # En una pasada, lo que cabía y lo que sobra, para pintarlos en dos
        # tramos: el exceso es lo que tiene que saltar a la vista.
        limite = f.get('limite') or Decimal('0')
        f['pct_limite'] = float(limite / tope * 100) if tope and f['estado'] == 'pasada' else 0
        f['pct_exceso'] = max(f['pct_real'] - f['pct_limite'], 0) if f['estado'] == 'pasada' else 0

    return {
        'bloques': excedidos,
        'categorias': explican,
        'sin_presupuesto': sin_limite,
        'revisar': revisar,
        'total_sin_presupuesto': sum((f['importe'] for f in sin_limite), Decimal('0')),
        'exceso_categorias': sum((f['exceso'] for f in explican), Decimal('0')),
        'no_previsto': sum((f['no_previsto'] for f in revisar), Decimal('0')),
        # El exceso total es el de los BLOQUES: sumar además el de cada
        # categoría contaría dos veces el mismo euro.
        'exceso_total': sum((f['exceso'] for f in excedidos), Decimal('0')),
        # La tarjeta también aparece cuando no hay excesos pero sí categorías
        # gastando sin límite declarado: es la lista desde la que se declaran, y
        # esconderla las deja invisibles para siempre.
        'hay_algo': bool(excedidos or explican or sin_limite),
        'hay_exceso': bool(excedidos or explican),
    }


def _con_las_partes_debajo(movimientos):
    """Reordena para que las partes de un cobro repartido salgan pegadas a él.

    Ordenados solo por fecha, un Norauto repartido en neumáticos y revisión
    aparece con una parte tres filas más arriba, el cobro en medio y la otra
    más abajo, con dos apuntes ajenos entre medias. Parecen tres gastos
    distintos de la misma tienda —y la pregunta inmediata es si se están
    contando tres veces—, cuando son un cobro y su desglose.
    """
    por_padre = defaultdict(list)
    for m in movimientos:
        if m.es_parte:
            por_padre[m.dividido_de_id].append(m)
    if not por_padre:
        return movimientos
    for partes in por_padre.values():
        partes.sort(key=lambda m: m.orden_parte)

    ordenados = []
    for m in movimientos:
        if m.es_parte and m.dividido_de_id in {x.pk for x in movimientos}:
            # Se emite junto a su cobro, no aquí.
            continue
        ordenados.append(m)
        ordenados.extend(por_padre.get(m.pk, ()))
    return ordenados


def _panel_context(hogar, todos, request, filtros=None):
    """Construye el panel de análisis de movimientos (KPIs, donut, ingresos vs
    gastos, filtros año/mes/categoría y listado agrupado por mes) que comparten
    el detalle de un extracto y la vista global de todos los extractos.

    `todos`: lista de MovimientoBancario (ya acotada al hogar y al ámbito que
    corresponda — un extracto o todos).

    `filtros`: los de `_leer_filtros`, cuando no salen de la URL. El Dashboard
    pide así el resumen del mes sin inventarse una petición con ?anio=&mes=."""
    # --- Filtros disponibles ---
    anios_disponibles = sorted({m.fecha.year for m in todos}, reverse=True)
    meses_disponibles = [{'valor': str(n), 'etiqueta': MESES_ES[n]} for n in range(1, 13)]

    f = filtros or _leer_filtros(request)
    anio_sel, mes_sel, cat_sel = f['anio'], f['mes'], f['categoria']
    bloque_sel, etiqueta_sel, activo_sel = f['bloque'], f['etiqueta'], f['activo']
    busqueda, ver_traspasos = f['busqueda'], f['ver_traspasos']

    movimientos = _con_las_partes_debajo([m for m in todos if _pasa_filtro(m, f)])

    # --- KPIs sobre el conjunto filtrado ---
    # Quién suma, quién resta y quién no cuenta lo dice el cómputo de la
    # categoría, no el signo del importe: los traspasos entre cuentas propias
    # (y cualquier categoría marcada como neutra) salen en negativo pero no son
    # gasto, y sumarlos inflaba el gasto del mes.
    # Mirando UN mes, los pagos de gastos no mensuales se sacan de la
    # comparación: el IBI o la revisión del coche se provisionan todo el año y
    # se pagan de golpe, así que dejarlos dentro del mes en el que caen dice que
    # te has pasado mil euros cuando lo que has hecho es pagar lo que tenías
    # provisionado. Se saca el PAGO del gasto observado; el límite se queda,
    # porque la provisión de ese mes sigue siendo el presupuesto de ese mes.
    # Sobre varios meses el pago se promedia bien y no hace falta sacarlo.
    vista_de_mes = mes_sel != 'all'
    # `cuenta_como_gasto` deja fuera los cobros repartidos: el que cuenta es
    # cada parte, y sumar también el original metía el dinero dos veces en la
    # cifra de «esto es de gastos anuales» de la cabecera.
    pagos_provision = [
        m for m in movimientos if m.es_pago_provision and m.cuenta_como_gasto
    ]
    if vista_de_mes and pagos_provision:
        # Salvo que hayas dicho de dónde salió el dinero. Si emparejaste 928 €
        # de la reserva con una revisión de 1.200, lo que sabemos es que 928
        # estaban provisionados y 272 no: esos 272 sí se pasaron del
        # presupuesto de este mes y tienen que verse. Sin emparejar no se sabe,
        # y el pago se sigue sacando entero —puede que la hucha lo cubriera y
        # simplemente no lo apuntaste, o que la recargues el mes que viene—.
        # Y lo mismo si dijiste que se pagó SIN la reserva: la hucha estaba
        # vacía y el dinero salió del bolsillo este mes, así que cuenta entero.
        provisiones_sacadas = [m for m in pagos_provision if m.se_saca_del_mes]
        fuera = {m.pk for m in provisiones_sacadas}
    else:
        provisiones_sacadas, fuera = [], set()

    # Sale de los TOTALES, no de la pantalla. Quitar también la fila dejaba la
    # revisión del coche invisible en septiembre: justo el movimiento sobre el
    # que hay que decir que la pagó la hucha, y no había nada que pulsar porque
    # no estaba. La lista es el registro de lo que pasó; lo que se ajusta es la
    # comparación con el presupuesto. La fila lleva su chapa «anual» para que se
    # entienda por qué no suma.
    contables = [m for m in movimientos if m.pk not in fuera] if fuera else movimientos

    # La reserva es un asunto de CUÁNDO, no de cuánto: mueve dinero de los meses
    # en los que ahorraste al mes en el que pagas. En un mes suelto hay que
    # descontarla, porque el golpe fue solo lo que no cubrió. Sobre un periodo
    # largo se compensa sola —el ahorro salió de meses que están dentro— y lo
    # que cuesta la revisión del coche al año son 1.200 €, no 272.
    def peso_de(m):
        return m.impacto_real if vista_de_mes else -m.importe_neto

    reales = [m for m in contables if not m.es_neutro]
    ingresos = sum((m.importe for m in reales if m.cuenta_como_ingreso), Decimal('0'))
    gastos = -sum((peso_de(m) for m in reales if m.cuenta_como_gasto), Decimal('0'))
    cubierto_reserva = sum(
        (m.cubierto_por_reserva for m in reales if m.cuenta_como_gasto), Decimal('0'),
    ) if vista_de_mes else Decimal('0')
    # Los cobros REPARTIDOS no cuentan: están sin categoría porque la llevan sus
    # partes, no porque falte clasificarlos. Con ellos dentro, el aviso pedía
    # clasificar algo que ya estaba hecho y el contador no bajaba nunca.
    sin_categorizar = sum(
        1 for m in movimientos
        if not m.categoria_id and not m.es_neutro and not m.esta_dividido
    )
    # Los reembolsos van aparte de los traspasos: también son neutros, pero no
    # son dinero tuyo cambiando de cuenta, así que no pueden «cuadrar» con nada
    # y mezclados solo harían que la cifra de los neutros dejara de decir cero.
    traspasos = [m for m in contables if m.es_neutro and not m.es_reembolso]
    traspaso_neto = sum((m.importe for m in traspasos), Decimal('0'))

    # --- Gastos compartidos ---
    # Lo que te devolvieron de los gastos del periodo. Se mide desde el GASTO,
    # no desde los Bizum: el de la cena del 30 de septiembre que llega el 2 de
    # octubre rebaja septiembre, que es cuando se gastó. Así esta cifra es
    # exactamente lo que se ha descontado del gasto de arriba.
    compartidos = [m for m in reales if m.cuenta_como_gasto and m.reembolsado]
    reembolsado_periodo = sum((m.reembolsado for m in compartidos), Decimal('0'))
    compartido_total = sum((-m.importe for m in compartidos), Decimal('0'))
    # Y los Bizum que siguen contando como ingreso: si alguno era la parte de
    # un amigo, el mes está inflado por los dos lados. Solo se avisa; decidir
    # cuál era de qué gasto es cosa del usuario.
    bizums_sueltos = [
        m for m in reales
        if m.cuenta_como_ingreso and 'bizum' in normalizar_texto(m.concepto)
    ]

    # --- El gasto, por los CUATRO PILARES del presupuesto ---
    # Es la vista principal, no un añadido: el presupuesto se declara en fijos,
    # anuales, variables y discrecionales, así que lo observado tiene que
    # leerse en esos mismos términos o no hay forma de conciliar uno con otro.
    # Las categorías quedan dentro de su bloque, para abrir y ver el reparto.
    #
    # Un abono dentro de una categoría de gasto (una devolución) resta de su
    # propia categoría, así que el total del bloque es su gasto neto.
    por_bloque = defaultdict(
        lambda: {'importe': Decimal('0'), 'bruto': Decimal('0'),
                 'cubierto': Decimal('0'), 'prorrateado': Decimal('0'),
                 'repartido': Decimal('0'), 'anterior': Decimal('0'),
                 'categorias': {}},
    )
    for m in reales:
        if not m.cuenta_como_gasto:
            continue
        tipo = m.categoria.tipo if m.categoria else 'sin'
        nombre = m.categoria.nombre if m.categoria else 'Sin categorizar'
        datos = por_bloque[tipo]
        # Lo que pesó de verdad: si sacaste 928 € de la reserva para pagar una
        # revisión de 1.200, el golpe del mes fueron 272, no 1.200. El coste del
        # coche sigue siendo 1.200 —eso lo ve su ficha—, pero el presupuesto del
        # mes solo sufre lo que no cubriste.
        peso = peso_de(m)
        # Y el pago ENTERO se guarda al lado. Sin él la fila decía «1 €» sin más
        # y no había forma de saber de dónde salía: parecía que el mes se había
        # comido novecientos euros. Un bloque solo se entiende si se ve el pago,
        # lo que puso la reserva y la diferencia que queda.
        bruto = -m.importe_neto
        cubierto = m.cubierto_por_reserva if vista_de_mes else Decimal('0')
        datos['importe'] += peso
        datos['bruto'] += bruto
        datos['cubierto'] += cubierto
        cat = datos['categorias'].setdefault(
            nombre, {'id': m.categoria_id, 'nombre': nombre,
                     'importe': Decimal('0'), 'bruto': Decimal('0'),
                     'cubierto': Decimal('0'), 'prorrateado': Decimal('0'),
                     'pagado_anual': Decimal('0'), 'meses_pago': [], 'num': 0,
                     'repartido': Decimal('0'), 'anterior': Decimal('0')},
        )
        cat['importe'] += peso
        cat['bruto'] += bruto
        cat['cubierto'] += cubierto
        cat['num'] += 1

    # --- Y la parte que le toca a este mes de los ANUALES ya pagados ---
    # Entra en el reparto aunque la categoría no tenga ni un apunte este mes:
    # justamente por eso hay que enseñarla, porque su provisión sí está dentro
    # del límite del mes y sin la fila el presupuesto parecía sin tocar.
    #
    # El bloque de los anuales queda fuera: ese ya se mide contra el año
    # entero, donde el pago cuenta completo, y prorratearlo además lo contaría
    # dos veces.
    prorrateo = _anuales_prorrateados(todos, f)
    for fila in prorrateo['por_categoria'].values():
        if fila['tipo'] == 'anual':
            continue
        datos = por_bloque[fila['tipo']]
        datos['prorrateado'] += fila['importe']
        cat = datos['categorias'].setdefault(
            fila['nombre'], {'id': fila['id'], 'nombre': fila['nombre'],
                             'importe': Decimal('0'), 'bruto': Decimal('0'),
                             'cubierto': Decimal('0'), 'prorrateado': Decimal('0'),
                             'pagado_anual': Decimal('0'), 'meses_pago': [], 'num': 0,
                             'repartido': Decimal('0'), 'anterior': Decimal('0')},
        )
        cat['prorrateado'] += fila['importe']
        cat['pagado_anual'] += fila['pagado']
        cat['meses_pago'] = [MESES_ES[n].lower() for n in fila['meses']]

    # --- Los pagos anuales que se sacaron del mes, dentro de SU bloque ---
    # El bloque de los anuales se mide contra la provisión del AÑO, y lo que la
    # consume es el recibo entero, venga el dinero de donde venga. Sacar el pago
    # del mes lo quitaba también de ahí: una revisión de 273 € sin emparejar
    # desaparecía del bloque, que decía «has pagado 929 €» cuando habían sido
    # 1.202, y el modal de su categoría —que sí la contaba— no cuadraba con la
    # fila desde la que se abría. Aquí entra en el pagado y en el recuento, sin
    # peso en el mes: eso lo decide `se_saca_del_mes`.
    for m in provisiones_sacadas:
        tipo = m.categoria.tipo if m.categoria else 'sin'
        if tipo != 'anual':
            continue
        nombre = m.categoria.nombre if m.categoria else 'Sin categorizar'
        datos = por_bloque[tipo]
        datos['bruto'] += -m.importe_neto
        datos['repartido'] += -m.importe_neto
        cat = datos['categorias'].setdefault(
            nombre, {'id': m.categoria_id, 'nombre': nombre,
                     'importe': Decimal('0'), 'bruto': Decimal('0'),
                     'cubierto': Decimal('0'), 'prorrateado': Decimal('0'),
                     'pagado_anual': Decimal('0'), 'meses_pago': [], 'num': 0,
                     'repartido': Decimal('0'), 'anterior': Decimal('0')},
        )
        cat['bruto'] += -m.importe_neto
        cat['repartido'] += -m.importe_neto
        cat['num'] += 1

    # --- Lo que ya llevan pagado los ANUALES en los meses de antes ---
    # Se juzgan contra la provisión del año, así que mirando septiembre la
    # pregunta es cuánto llevas consumido del año hasta septiembre, no cuánto
    # pagaste en septiembre. Entra aunque este mes no haya ni un pago: en
    # octubre el bloque tiene que seguir diciendo cuánto queda del año.
    anterior = _anuales_meses_anteriores(todos, f)
    for nombre, fila in anterior.items():
        datos = por_bloque['anual']
        datos['anterior'] += fila['importe']
        cat = datos['categorias'].setdefault(
            nombre, {'id': fila['id'], 'nombre': nombre,
                     'importe': Decimal('0'), 'bruto': Decimal('0'),
                     'cubierto': Decimal('0'), 'prorrateado': Decimal('0'),
                     'pagado_anual': Decimal('0'), 'meses_pago': [], 'num': 0,
                     'repartido': Decimal('0'), 'anterior': Decimal('0')},
        )
        cat['anterior'] += fila['importe']

    total_gasto_abs = sum(
        (d['importe'] for d in por_bloque.values() if d['importe'] > 0), Decimal('0'),
    )

    # Meses del periodo con extracto importado. Es el divisor de todas las
    # medias y de los límites: el presupuesto es mensual y lo que se está
    # mirando puede ser un año entero. Se cuentan los meses con DATOS, no los
    # meses con gasto de lo filtrado, para que un mes sin repostar cuente como
    # cero en la media de Gasolina en vez de desaparecer del divisor.
    meses_periodo = max(
        len({(m.fecha.year, m.fecha.month) for m in todos if _pasa_periodo(m, f)}), 1,
    )
    # Los FIJOS ANUALES no se miden por meses sino por años: lo que declaras es
    # lo que te va a costar el año, y el pago llega de golpe cuando toca. Su
    # divisor es el número de años que hay a la vista, no el de meses.
    anios_periodo = max(
        len({m.fecha.year for m in todos if _pasa_periodo(m, f)}), 1,
    )
    # El límite SIEMPRE incluye todas las partidas prorrateadas, también las no
    # mensuales: los 43 €/mes que reservas para el IBI son el presupuesto de ese
    # mes aunque el recibo llegue en junio. Antes se quitaban junto con el pago
    # y el bloque de los anuales se quedaba «sin límite» en la vista mensual,
    # cuando tiene uno perfectamente definido: lo que apartas cada mes.
    limite_bloque = presupuesto.por_bloque(hogar)
    limite_categoria = presupuesto.por_categoria(hogar)
    # Y los mismos límites en su unidad anual, que es contra la que se juzga el
    # bloque de los anuales. No es el mensual por doce: `importe_mensual` viene
    # redondeado, y multiplicarlo convertía un IBI de 520 € en uno de 519,96.
    limite_bloque_anual = presupuesto.por_bloque(hogar, anual=True)
    limite_categoria_anual = presupuesto.por_categoria(hogar, anual=True)
    # Los bloques cuyo límite se declara entero: dentro no se espera presupuesto
    # por categoría, así que las suyas se enseñan con su peso y no con un «de X»
    # que no existe.
    techos_propios = presupuesto.techo_de_bloque(hogar)

    bloques = []
    for tipo in list(ORDEN_TIPOS) + ['sin']:
        datos = por_bloque.get(tipo)
        # Un bloque sin gasto este mes pero con un anual ya pagado dentro sigue
        # teniendo algo que contar: su presupuesto está ocupado.
        if not datos or (datos['importe'] <= 0 and datos['prorrateado'] <= 0
                         and datos['repartido'] <= 0 and datos['anterior'] <= 0):
            continue
        importe = datos['importe']
        # El bloque de los anuales se juzga contra el AÑO. Un límite mensual ahí
        # no significa nada: los 253 €/mes que apartas para el IBI, la revisión
        # y los seguros no son un tope de septiembre, son la doceava parte de lo
        # que te vas a gastar en el año. Comparar el pago de la revisión contra
        # esos 253 € solo podía decir que te habías pasado. Contra los 3.036 €
        # del año dice lo que de verdad interesa: cuánto de la provisión llevas
        # consumido.
        es_anual = tipo == 'anual'
        escala = anios_periodo if es_anual else meses_periodo
        de_bloque = limite_bloque_anual if es_anual else limite_bloque
        de_categoria = limite_categoria_anual if es_anual else limite_categoria
        categorias = sorted(
            (c for c in datos['categorias'].values()
             if c['importe'] > 0 or c['prorrateado'] > 0 or c['repartido'] > 0
             or c['anterior'] > 0),
            key=lambda c: (c['bruto'] + c['anterior']) if es_anual
            else c['importe'] + c['prorrateado'],
            reverse=True,
        )
        for c in categorias:
            c['pct_bloque'] = round(float(c['importe'] / importe * 100), 1) if importe else 0
            c['pct_total'] = round(float(c['importe'] / total_gasto_abs * 100), 1) if total_gasto_abs else 0
            c['media_mes'] = c['importe'] / meses_periodo
            c['es_anual'] = es_anual
            c.update(_contra_el_limite(
                c['importe'], c['bruto'], c['cubierto'],
                de_categoria.get(c['id'], Decimal('0')) * escala, es_anual,
                prorrateado=c['repartido'] if es_anual else c['prorrateado'],
                anterior=c['anterior'],
            ))
        limite = de_bloque.get(tipo, Decimal('0')) * escala
        bloques.append({
            'tipo': tipo,
            'techo_propio': tipo in techos_propios,
            'etiqueta': ETIQUETAS_TIPO.get(tipo, 'Sin categorizar'),
            'importe': importe,
            'bruto': datos['bruto'],
            'cubierto': datos['cubierto'],
            'repartido': datos['repartido'],
            'es_anual': es_anual,
            # Lo que queda de la provisión del año, que es la pregunta que
            # contesta un anual: cuánto me queda por pagar sin pasarme.
            'queda_anual': (limite - datos['bruto'] - datos['anterior']) if es_anual else None,
            'anios_periodo': anios_periodo,
            'pct': round(float(importe / total_gasto_abs * 100), 1) if total_gasto_abs else 0,
            'color': COLOR_TIPO.get(tipo, '#9aa5a0'),
            'categorias': categorias,
            'num_categorias': len(categorias),
            # En los anuales el tramo en discontinuo de la barra es lo pagado
            # que se reparte en el año: con él, neto + repartido + reserva suman
            # el pagado que se compara con la provisión anual.
            **_contra_el_limite(importe, datos['bruto'], datos['cubierto'], limite,
                                es_anual,
                                prorrateado=datos['repartido'] if es_anual else datos['prorrateado'],
                                anterior=datos['anterior']),
        })

    # El donut se pinta por BLOQUE, no por categoría: con quince categorías era
    # una rueda de colores ilegible que no coincidía con ninguna otra cifra de
    # la pantalla.
    donut = [
        {
            'nombre': b['etiqueta'],
            'importe': float(b['importe']),
            'pct': b['pct'],
            'color': b['color'],
        }
        for b in bloques
    ]

    # --- Agrupación por mes (para el listado) ---
    grupos_mes = defaultdict(lambda: {
        'movimientos': [], 'ingresos': Decimal('0'),
        'gastos': Decimal('0'), 'neutro': Decimal('0'),
        'provisiones': Decimal('0'),
    })
    for m in movimientos:
        g = grupos_mes[(m.fecha.year, m.fecha.month)]
        g['movimientos'].append(m)
        # Mismo criterio que los KPIs: manda el cómputo de la categoría, para
        # que el neto del mes no cuente los traspasos como gasto. Y un pago que
        # se ha sacado de la comparación se pinta pero no suma, o la cabecera
        # diría un número y la tarjeta de arriba otro.
        #
        # El último caso es `cuenta_como_gasto` y NO un `else`: con el else, un
        # cobro repartido sumaba su total además del de sus partes. La fila
        # decía «repartido en 4 · no cuenta» y aparecía tachada, y aun así el
        # mes cargaba 924,81 € donde la suma real eran 740,08.
        if m.pk in fuera:
            g['provisiones'] -= -m.importe_neto
        elif m.es_neutro:
            g['neutro'] += m.importe
        elif m.cuenta_como_ingreso:
            g['ingresos'] += m.importe
        elif m.cuenta_como_gasto:
            g['gastos'] -= peso_de(m)

    # Con el histórico entero a la vista, pintar los apuntes de los treinta y
    # seis meses eran veinte megas de HTML y cuatro segundos de render para ver
    # el mes de arriba. Los meses plegados se mandan SIN sus filas y se piden al
    # desplegarlos; la cabecera con sus totales sí viaja siempre, que es lo que
    # se lee de un vistazo. Por debajo del umbral no se difiere nada: pedir por
    # red lo que cabe de sobra en la respuesta solo añade latencia.
    diferir = len(movimientos) > UMBRAL_DIFERIR_MESES
    grupos = []
    for indice, ((anio, mes), datos) in enumerate(sorted(grupos_mes.items(), reverse=True)):
        # El primero viene siempre pintado: es el que se ve al abrir.
        pendiente = diferir and indice > 0
        grupos.append({
            'anio': anio,
            'mes': mes,
            'etiqueta': f"{MESES_ES[mes]} {anio}",
            'ingresos': datos['ingresos'],
            'gastos': datos['gastos'],
            'neutro': datos['neutro'],
            'provisiones': datos['provisiones'],
            'neto': datos['ingresos'] + datos['gastos'],
            'num': len(datos['movimientos']),
            'pendiente': pendiente,
            'movimientos': [] if pendiente else datos['movimientos'],
        })

    categorias_hogar = CategoriaGasto.objects.filter(hogar=hogar, activo=True).order_by('tipo', 'nombre')
    categoria_activa = next(
        (c for c in categorias_hogar if str(c.id) == str(cat_sel)), None,
    )

    # --- La media mensual de lo que se está mirando ---
    # «¿Cuánto me cuesta la gasolina al mes?» no se responde con el total del
    # filtro: se responde dividiéndolo entre los meses que abarca. Es la cifra
    # que hace comparables un año entero y un trimestre.
    media = {
        'meses': meses_periodo,
        'gasto': abs(gastos) / meses_periodo,
        'ingreso': ingresos / meses_periodo,
        'neto': (ingresos + gastos) / meses_periodo,
        'num': len(movimientos) / meses_periodo,
        'ambito': _ambito_filtrado(f, categoria_activa, hogar),
        # Con un solo mes a la vista la media ES el mes: repetir la cifra solo
        # añade ruido.
        'mostrar': meses_periodo > 1,
        'limite': (
            limite_categoria.get(categoria_activa.id, Decimal('0'))
            if categoria_activa else Decimal('0')
        ),
    }

    # --- Cuánto queda del presupuesto ---
    # Saber que se han ido 3.851 € no dice si el mes va bien: la pregunta es
    # cuánto queda hasta el tope que uno mismo se puso, y esa resta no estaba
    # en ninguna parte de la pantalla. El tope es el del periodo entero: si hay
    # tres meses a la vista, tres veces el presupuesto mensual.
    #
    # Lo gastado se mide contra ese tope INCLUYENDO la parte prorrateada de los
    # anuales que ya has pagado: su provisión está dentro del límite, así que
    # dejarla fuera del gasto regalaría un margen que no existe.
    tope_periodo = sum(limite_bloque.values(), Decimal('0')) * meses_periodo
    gastado_periodo = -gastos + prorrateo['total']
    restante_periodo = tope_periodo - gastado_periodo
    presupuesto_periodo = {
        'hay': tope_periodo > 0,
        'limite': tope_periodo,
        'gastado': gastado_periodo,
        'restante': restante_periodo,
        'prorrateado': prorrateo['total'],
        'meses': meses_periodo,
        'pasado': restante_periodo < 0,
        'exceso': max(-restante_periodo, Decimal('0')),
        'pct': (
            round(min(float(gastado_periodo / tope_periodo * 100), 100), 1)
            if tope_periodo > 0 else 0
        ),
        # La barra se parte igual que las de los bloques: lo que ha salido de
        # la cuenta y, en discontinuo, lo que ya estaba pagado de los anuales.
        'pct_real': (
            round(min(float(-gastos / tope_periodo * 100), 100), 1)
            if tope_periodo > 0 else 0
        ),
        'pct_prorrateado': (
            round(min(float(prorrateo['total'] / tope_periodo * 100),
                      max(0.0, 100 - float(-gastos / tope_periodo * 100))), 1)
            if tope_periodo > 0 and prorrateo['total'] > 0 else 0
        ),
        # Lo que va de mes o de año, para poder decir si ese 60% gastado es ir
        # sobrado o ir camino de pasarse. Sin la marca, un 60% el día 5 y un
        # 60% el día 28 se leen igual y no son lo mismo.
        'pct_transcurrido': _pct_transcurrido(f, meses_periodo),
    }

    # --- Lo que explica el periodo (antes, la pestaña «Análisis») ---
    fuera_presupuesto = _fuera_de_presupuesto(bloques)
    comercios = _comercios_del_periodo(contables, meses_periodo, vista_de_mes=vista_de_mes)

    # La comparación contra la media de los meses anteriores necesita UN mes
    # concreto —es su unidad— y los meses previos, que por definición quedan
    # fuera del filtro. Con el buscador o un activo puestos no se ofrece: el
    # motor no los conoce y la comparación diría algo distinto de la lista que
    # tiene debajo.
    comparativa = None
    if anio_sel != 'all' and mes_sel != 'all' and not busqueda and not activo_sel:
        comparativa = analizar_mes(
            todos, int(anio_sel), int(mes_sel),
            bloque=bloque_sel or None,
            categoria_id=categoria_activa.id if categoria_activa else None,
            etiqueta_id=_entero_o_none(etiqueta_sel),
            limite_categoria=limite_categoria,
            limite_bloque=limite_bloque,
        )

    return {
        'grupos': grupos,
        'donut': donut,
        'donut_total': float(total_gasto_abs),
        'kpi_ingresos': ingresos,
        'kpi_gastos': gastos,
        # El mismo gasto que la cabecera, en positivo. «En qué se va» enseñaba
        # su propia suma redondeada a euros (1.409 €) al lado de unos gastos de
        # -1.409,13 € y de un balance de -1.390,89 €: tres cifras que parecían
        # tres cosas distintas cuando son dos, y una repetida.
        'kpi_gasto_abs': -gastos,
        'kpi_neto': ingresos + gastos,
        'kpi_num': len(movimientos),
        'kpi_sin_categorizar': sin_categorizar,
        'anios_disponibles': anios_disponibles,
        'meses_disponibles': meses_disponibles,
        'anio_sel': anio_sel,
        'mes_sel': mes_sel,
        'cat_sel': cat_sel,
        'categoria_activa': categoria_activa,
        'ver_traspasos': ver_traspasos,
        'busqueda': busqueda,
        'bloque_sel': bloque_sel,
        'bloque_etiqueta': ETIQUETAS_TIPO.get(bloque_sel, 'Sin categorizar') if bloque_sel else '',
        'etiqueta_sel': etiqueta_sel,
        'activo_sel': activo_sel,
        'periodo_etiqueta': _etiqueta_periodo(anio_sel, mes_sel),
        'meses_periodo': meses_periodo,
        'media': media,
        'presupuesto': presupuesto_periodo,
        'prorrateo': prorrateo,
        'cubierto_reserva': cubierto_reserva,
        # El aviso habla de lo que se ha SACADO del mes, así que lista solo eso:
        # un pago que se queda —porque dijiste cuánto puso la reserva— ya se ve
        # en su bloque con el peso que le corresponde.
        'pagos_provision': provisiones_sacadas,
        'total_provisiones': sum((-m.importe_neto for m in provisiones_sacadas), Decimal('0')),
        'fuera_presupuesto': fuera_presupuesto,
        'comercios': comercios,
        'comparativa': comparativa,
        'tipos_bloque': [
            {'valor': t, 'etiqueta': ETIQUETAS_TIPO.get(t, t)} for t in ORDEN_TIPOS
        ],
        'num_traspasos': sum(1 for m in todos if m.es_neutro and not m.es_reembolso),
        'reembolsado': reembolsado_periodo,
        'compartido_total': compartido_total,
        'compartido_tu_parte': compartido_total - reembolsado_periodo,
        'num_compartidos': len(compartidos),
        'pct_compartido_tuyo': (
            round(float((compartido_total - reembolsado_periodo) / compartido_total * 100), 1)
            if compartido_total > 0 else 0
        ),
        'num_bizums_sueltos': len(bizums_sueltos),
        'total_bizums_sueltos': sum((m.importe for m in bizums_sueltos), Decimal('0')),
        'kpi_traspaso_neto': traspaso_neto,
        'traspasos_cuadran': traspaso_neto == 0 and bool(traspasos),
        'bloques': bloques,
        'categorias_hogar': categorias_hogar,
        'hay_filtro': (
            anio_sel != 'all' or mes_sel != 'all' or cat_sel != 'all'
            or bool(busqueda) or bool(bloque_sel) or bool(etiqueta_sel)
            or bool(activo_sel)
        ),
        **_volver_a(request, anio_sel, mes_sel),
        **_contexto_edicion(hogar),
    }


def _volver_a(request, anio_sel, mes_sel):
    """De dónde se venía, para poder volver sin perder el mes.

    Entrar desde «Ocio se ha pasado 180 €» y no tener forma de regresar era el
    corte de navegación más molesto de la pantalla. Es una LISTA BLANCA de
    destinos conocidos y no una URL libre: un «volver» que acepte cualquier
    dirección es un redirector abierto de manual.
    """
    destinos = {
        'movimientos': ('extractos:listar', 'Movimientos'),
    }
    volver = request.GET.get('volver') or ''
    if volver not in destinos:
        return {'volver_url': '', 'volver_nombre': ''}

    ruta, nombre = destinos[volver]
    periodo = '&'.join(
        f'{clave}={valor}' for clave, valor in (('anio', anio_sel), ('mes', mes_sel))
        if valor and valor != 'all'
    )
    return {
        'volver_url': f'{reverse(ruta)}?{periodo}' if periodo else reverse(ruta),
        'volver_nombre': nombre,
    }


def _ambito_filtrado(f, categoria_activa, hogar):
    """Cómo se llama en una línea lo que hay filtrado. Una media sin decir de
    qué es una media es un número suelto."""
    if categoria_activa:
        return categoria_activa.nombre
    if f['categoria'] == 'sin':
        return 'lo que está sin categorizar'
    if f['bloque']:
        return ETIQUETAS_TIPO.get(f['bloque'], 'Sin categorizar')
    if f['etiqueta']:
        etiqueta = Etiqueta.objects.filter(hogar=hogar, id=f['etiqueta']).first()
        if etiqueta:
            return etiqueta.nombre
    if f['activo']:
        activo = costes_activo.resolver(hogar, f['activo'])
        if activo:
            return activo.nombre
    if f['busqueda']:
        return f'«{f["busqueda"]}»'
    return 'todo el gasto'


@login_required
def detalle(request, pk):
    profile, hogar = _get_hogar(request)
    if not hogar:
        messages.error(request, "Necesitas pertenecer a un hogar.")
        return redirect('dashboard')

    extracto = get_object_or_404(ExtractoBancario, pk=pk, hogar=hogar)
    todos = list(
        extracto.movimientos.select_related('categoria', 'partida_conciliada', 'cubre', 'reembolsa', 'dividido_de')
        .prefetch_related('etiquetas', 'partes', 'coberturas',
                          'dividido_de__partes', 'dividido_de__coberturas', 'reembolsos', 'dividido_de__reembolsos').all()
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
            # Elegir a mano una categoría que SÍ cuenta desmarca el traspaso: si
            # no, la fila se quedaría con la chapa «traspaso» al lado de una
            # categoría de ingreso, diciendo dos cosas distintas del mismo
            # apunte.
            if cat.computo != COMPUTO_NEUTRO:
                mov.es_traspaso = False

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
def marcar_traspaso(request, pk):
    """Dice si un movimiento es —o no— un traspaso entre cuentas propias.

    La importación lo deduce del texto: que hable de transferencia y mencione a
    alguien del hogar. Acierta casi siempre, pero «Transferencia de ADRIAN VIDAL
    RODRIGUEZ» puede ser tu primo, que se llama igual, devolviéndote la cena. Y
    al revés: un traspaso tuyo desde un banco que no pone tu nombre entra como
    ingreso e infla el mes.

    Hasta ahora esa deducción no se podía tocar desde ningún sitio: el campo solo
    se escribía al importar.
    """
    profile, hogar = _get_hogar(request)
    if not hogar:
        return JsonResponse({'ok': False, 'error': 'sin_hogar'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'metodo'}, status=405)

    mov = get_object_or_404(MovimientoBancario, pk=pk, hogar=hogar)
    mov.es_traspaso = request.POST.get('es_traspaso') == '1'
    campos = ['es_traspaso']

    # Quitar la marca a algo que sigue en la categoría de traspasos no serviría
    # de nada: la categoría manda, y el movimiento se quedaría neutro igual.
    if not mov.es_traspaso and mov.categoria and mov.categoria.computo == COMPUTO_NEUTRO:
        mov.categoria = None
        mov.estado_categorizacion = 'sin_categorizar'
        campos += ['categoria', 'estado_categorizacion']

    mov.save(update_fields=campos)
    return JsonResponse({
        'ok': True,
        'es_traspaso': mov.es_traspaso,
        'categoria': mov.categoria.nombre if mov.categoria else None,
        'categoria_id': mov.categoria_id,
    })


@login_required
def crear_movimiento(request):
    """Mete un apunte a mano: el bar, el mercadillo, lo que se pagó en efectivo.

    Lo que no pasa por el banco no está en ningún extracto, y sin esto el mes
    dice que te has gastado menos de lo que te has gastado. La cifra deja de ser
    «lo que movió la cuenta» para ser «lo que gastaste», que es la que se quiere.

    Se pide el importe en positivo y aparte si es gasto o ingreso: escribir el
    signo a mano es la forma más fácil de meter un ingreso de 40 € donde iba un
    gasto, y el error no se ve hasta que los totales no cuadran.
    """
    profile, hogar = _get_hogar(request)
    if not hogar:
        messages.error(request, "Necesitas pertenecer a un hogar.")
        return redirect('dashboard')

    volver = request.POST.get('volver_a') or reverse('extractos:listar')
    if request.method != 'POST':
        return redirect(volver)

    fecha = parse_date(request.POST.get('fecha') or '')
    concepto = (request.POST.get('concepto') or '').strip()
    try:
        importe = Decimal((request.POST.get('importe') or '').replace(',', '.'))
    except InvalidOperation:
        importe = None

    if not fecha or not concepto or importe is None:
        messages.error(request, "Hace falta una fecha, un concepto y un importe.")
        return redirect(volver)
    if importe <= 0:
        messages.error(request, "El importe va en positivo; di aparte si es gasto o ingreso.")
        return redirect(volver)

    es_ingreso = request.POST.get('tipo') == 'ingreso'
    categoria = CategoriaGasto.objects.filter(
        hogar=hogar, id=request.POST.get('categoria_id') or 0,
    ).first()
    estado = 'manual' if categoria else 'sin_categorizar'

    # Sin categoría elegida se prueba con lo que ya sabe la aplicación: el mismo
    # criterio que al importar, para que meter «Mercadona» a mano acabe donde
    # acaban los demás Mercadona.
    if not categoria:
        categoria, origen = categorizar(concepto, hogar, es_ingreso=es_ingreso)
        if categoria:
            estado = 'por_regla' if origen == 'regla' else 'por_codigo'

    mov = MovimientoBancario(
        hogar=hogar, extracto=None, manual=True,
        fecha=fecha, concepto=concepto[:300], concepto_raw=concepto,
        importe=importe if es_ingreso else -importe,
        saldo=None, categoria=categoria, estado_categorizacion=estado,
    )
    costes_activo.asignar(mov, costes_activo.resolver(hogar, request.POST.get('activo') or ''))
    mov.save()

    messages.success(
        request,
        f"Apuntado: {concepto} · {mov.importe} €"
        + (f" · {categoria.nombre}" if categoria else " · sin categorizar"),
    )
    return redirect(volver)


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
    # Un apunte metido a mano no cuelga de ningún extracto: no hay nada que
    # recontar y pedirle `num_movimientos` a un None reventaba el borrado.
    if extracto:
        extracto.num_movimientos = extracto.movimientos.count()
        extracto.save(update_fields=['num_movimientos'])
    return JsonResponse({'ok': True})


@login_required
def cubrir_con_reserva(request, pk):
    """Dice que un movimiento aporta dinero de la reserva a un pago concreto.

    Ahorras todo el año para la revisión del coche y, cuando llega, metes esa
    reserva en la cuenta. El golpe real del mes no son los 1.200 € del recibo:
    son los 272 € que la reserva no cubrió. Emparejarlo con el pago —y no
    limitarse a mirar el saldo de un fondo— es lo que permite decir exactamente
    cuánto puso el ahorro y cuánto el bolsillo.
    """
    profile, hogar = _get_hogar(request)
    if not hogar:
        return JsonResponse({'ok': False, 'error': 'sin_hogar'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'metodo'}, status=405)

    mov = get_object_or_404(MovimientoBancario, pk=pk, hogar=hogar)

    crudo = request.POST.get('cubre') or ''
    if not crudo:
        mov.cubre = None
        mov.save(update_fields=['cubre'])
        return JsonResponse({'ok': True, 'cubre': None})

    pago = MovimientoBancario.objects.filter(hogar=hogar, pk=crudo or 0).first()
    if not pago:
        return JsonResponse({'ok': False, 'error': 'pago_invalido'}, status=400)
    if pago.pk == mov.pk:
        return JsonResponse({'ok': False, 'error': 'a_si_mismo'}, status=400)
    # Un reembolso de un gasto compartido ya rebaja su gasto: si además
    # cubriera otro, el mismo dinero descontaría dos veces.
    if mov.es_reembolso:
        return JsonResponse({'ok': False, 'error': 'es_reembolso'}, status=400)
    # Una reserva cubre GASTOS. Dejar que cubra otra reposición encadenaría
    # coberturas y el impacto real dejaría de significar nada.
    if pago.es_cobertura:
        return JsonResponse({'ok': False, 'error': 'ya_es_cobertura'}, status=400)
    if pago.importe >= 0:
        return JsonResponse({'ok': False, 'error': 'no_es_un_gasto'}, status=400)

    mov.cubre = pago
    mov.save(update_fields=['cubre'])
    return JsonResponse({
        'ok': True,
        'cubre': pago.pk,
        'concepto': pago.concepto,
        'cubierto': float(pago.cubierto_por_reserva),
        'impacto': float(pago.impacto_real),
    })


@login_required
def marcar_sin_reserva(request, pk):
    """Dice que un pago anual NO salió de la reserva: cuenta entero en su mes.

    Sin emparejar con la hucha la pantalla supone que el dinero estaba
    apartado, lo saca del mes y le carga su doceava parte. Cuando la hucha ya
    estaba vacía eso es justo lo contrario de lo que pasó: la revisión de 273 €
    salió del bolsillo en septiembre y septiembre tiene que verla entera.
    """
    profile, hogar = _get_hogar(request)
    if not hogar:
        return JsonResponse({'ok': False, 'error': 'sin_hogar'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'metodo'}, status=405)

    mov = get_object_or_404(MovimientoBancario, pk=pk, hogar=hogar)
    if mov.importe is None or mov.importe >= 0:
        return JsonResponse({'ok': False, 'error': 'no_es_un_gasto'}, status=400)
    mov.sin_reserva = request.POST.get('valor') == '1'
    mov.save(update_fields=['sin_reserva'])
    return JsonResponse({'ok': True, 'sin_reserva': mov.sin_reserva})


@login_required
def candidatos_reserva(request):
    """Con qué se puede emparejar este movimiento, mire desde donde se mire.

    Se entra por los dos lados, porque nadie piensa igual las dos veces: unas
    desde el ingreso («esto que acabo de meter era para la revisión») y otras
    desde el gasto («la revisión: 928 € los puse de la hucha»). La primera
    versión solo ofrecía el camino del ingreso y el botón acababa en la única
    fila en la que no se te ocurre buscarlo.

    Desde un INGRESO devuelve los gastos cercanos que puede cubrir; desde un
    GASTO, los ingresos cercanos que pueden cubrirlo a él, más lo que ya tenga
    emparejado para poder deshacerlo ahí mismo.
    """
    profile, hogar = _get_hogar(request)
    if not hogar:
        return JsonResponse({'ok': False, 'error': 'sin_hogar'}, status=403)

    mov = get_object_or_404(MovimientoBancario, pk=request.GET.get('mov') or 0, hogar=hogar)
    desde = mov.fecha - timedelta(days=62)
    hasta = mov.fecha + timedelta(days=62)
    cerca = (
        MovimientoBancario.objects
        .filter(hogar=hogar, fecha__range=(desde, hasta)).exclude(pk=mov.pk)
        .select_related('categoria', 'dividido_de')
        .prefetch_related('coberturas', 'partes',
                          'dividido_de__partes', 'dividido_de__coberturas', 'reembolsos', 'dividido_de__reembolsos')
        .order_by('-fecha')
    )

    def fila(m):
        return {
            'id': m.pk,
            'etiqueta': f'{m.fecha.strftime("%d/%m")} · {m.concepto[:48]}',
            'importe': float(m.importe),
            'cubierto': float(m.cubierto_por_reserva),
            'pendiente': float(m.impacto_real),
        }

    if mov.importe is not None and mov.importe < 0:
        # Desde el gasto: quién le puede meter dinero. Se descartan los ingresos
        # que ya están puestos en OTRO pago, para no robárselo sin avisar.
        candidatos = cerca.filter(importe__gte=0, reembolsa__isnull=True).filter(
            Q(cubre__isnull=True) | Q(cubre=mov),
        )[:60]
        return JsonResponse({
            'ok': True,
            'sentido': 'gasto',
            'pendiente': float(mov.impacto_real),
            'candidatos': [fila(m) for m in candidatos],
            'puestos': [fila(c) for c in mov.coberturas.all()],
        })

    # Desde el ingreso: qué gasto cubre. Una reposición no cubre otra, o las
    # coberturas se encadenarían y el impacto real dejaría de significar nada.
    # Se ofrecen tanto el cobro entero como sus partes: hay quien empareja la
    # hucha con el recibo de Norauto y quien la empareja con la revisión de
    # dentro. Cubrir el cobro reparte el dinero entre sus partes a prorrata.
    candidatos = cerca.filter(importe__lt=0, cubre__isnull=True)[:60]
    return JsonResponse({
        'ok': True,
        'sentido': 'ingreso',
        'candidatos': [fila(m) for m in candidatos],
        'puestos': [],
    })


# ── Gastos compartidos ──────────────────────────────────────────────────────
# Pagas la cena de seis —180 €— y los demás te hacen un Bizum o te dan su parte
# en efectivo. El banco dice que gastaste 180 € en restaurantes e ingresaste
# 150, y ninguna de las dos cosas es verdad: la cena te costó 30 y los Bizum
# son tu dinero volviendo. Aquí se emparejan, y a partir de ahí el gasto pesa
# solo tu parte en todas las cifras y los Bizum dejan de ser ingresos.

# Cuánto se mira alrededor del gasto al buscar lo que te devolvieron. Hacia
# atrás, poco: hay quien paga su parte por adelantado, pero es raro. Hacia
# delante, dos meses: el amigo que tarda en hacer el Bizum siempre existe.
COMPARTIDO_DIAS_ANTES = 7
COMPARTIDO_DIAS_DESPUES = 62

def _prefetch_compartido(qs):
    return (
        qs.select_related('categoria', 'dividido_de', 'reembolsa')
        .prefetch_related('reembolsos', 'coberturas', 'partes',
                          'dividido_de__partes', 'dividido_de__reembolsos',
                          'dividido_de__coberturas')
    )


def _puede_compartirse(gasto):
    """Solo un gasto de verdad: no un traspaso, ni un ingreso, ni una reposición."""
    return (
        gasto.importe is not None and gasto.importe < 0
        and not gasto.es_reembolso and not gasto.es_cobertura
        and (gasto.cuenta_como_gasto or gasto.esta_dividido)
    )


def _puede_ser_reembolso(ingreso):
    return (
        ingreso.importe is not None and ingreso.importe > 0
        and not ingreso.es_cobertura and not ingreso.esta_dividido
        and not ingreso.es_parte
    )


def _fila_reembolso(m):
    return {
        'id': m.pk,
        'fecha': m.fecha.strftime('%d/%m/%Y'),
        'fecha_iso': m.fecha.isoformat(),
        'concepto': m.concepto,
        'importe': float(m.importe),
        'manual': m.manual,
    }


def _estado_compartido(gasto):
    """Todo lo que el diálogo necesita pintar de un gasto compartido."""
    total = -gasto.importe
    reembolsado = gasto.reembolsado
    return {
        'id': gasto.pk,
        'concepto': gasto.concepto,
        'fecha': gasto.fecha.strftime('%d/%m/%Y'),
        'fecha_iso': gasto.fecha.isoformat(),
        'categoria': gasto.categoria.nombre if gasto.categoria else '',
        'total': float(total),
        'reembolsado': float(reembolsado),
        'tu_parte': float(total - reembolsado),
        'pendiente': float(gasto.pendiente_de_reembolso),
        'es_parte': gasto.es_parte,
        'dividido': gasto.esta_dividido,
        # Lo emparejado con el cobro entero, si esto es una de sus partes: se
        # enseña para que no parezca que falta dinero, pero se suelta desde el
        # cobro, que es donde se puso.
        'del_cobro': (
            float(reembolsado - sum((r.importe for r in gasto._reembolsos()), Decimal('0')))
            if gasto.es_parte else 0.0
        ),
        'reembolsos': [_fila_reembolso(r) for r in gasto.reembolsos_lista],
    }


def _candidatos_reembolso(hogar, gasto):
    """Ingresos de alrededor que pueden ser lo que te devolvieron de este gasto.

    Primero los que parecen un Bizum o una transferencia y caben en lo que
    queda por devolver; dentro de cada grupo, los más cercanos a la fecha del
    gasto. Los que ya están puestos en OTRO gasto no salen: se los robaría sin
    avisar.
    """
    desde = gasto.fecha - timedelta(days=COMPARTIDO_DIAS_ANTES)
    hasta = gasto.fecha + timedelta(days=COMPARTIDO_DIAS_DESPUES)
    cerca = _prefetch_compartido(
        MovimientoBancario.objects.filter(
            hogar=hogar, fecha__range=(desde, hasta), importe__gt=0,
            reembolsa__isnull=True, cubre__isnull=True, dividido_de__isnull=True,
        ).exclude(pk=gasto.pk)
    )
    pendiente = gasto.pendiente_de_reembolso
    filas = []
    for m in cerca:
        if m.esta_dividido:
            continue
        cabe = m.importe <= pendiente
        pista = m.parece_reembolso
        filas.append((
            (not (cabe and pista), not cabe, abs((m.fecha - gasto.fecha).days)),
            dict(_fila_reembolso(m), cabe=cabe, sugerido=cabe and pista),
        ))
    filas.sort(key=lambda f: f[0])
    return [f[1] for f in filas[:50]]


def _gastos_candidatos(hogar, ingreso):
    """Desde el Bizum: a qué gasto de los de antes puede corresponder.

    Solo los que aún tienen sitio para este dinero; primero los del mismo
    importe multiplicado (seis a 30 € = una cena de 180), luego por cercanía.
    """
    desde = ingreso.fecha - timedelta(days=COMPARTIDO_DIAS_DESPUES)
    hasta = ingreso.fecha + timedelta(days=COMPARTIDO_DIAS_ANTES)
    cerca = _prefetch_compartido(
        MovimientoBancario.objects.filter(
            hogar=hogar, fecha__range=(desde, hasta), importe__lt=0,
        ).exclude(pk=ingreso.pk)
    )
    filas = []
    for m in cerca:
        if not _puede_compartirse(m) or m.pendiente_de_reembolso < ingreso.importe:
            continue
        total = -m.importe
        # ¿Es el gasto un múltiplo exacto de lo que te han pagado? Es la huella
        # de «pagamos a partes iguales», y casi siempre es el bueno.
        multiplo = bool(ingreso.importe) and (total % ingreso.importe) == 0 and total > ingreso.importe
        filas.append((
            (not multiplo, abs((m.fecha - ingreso.fecha).days)),
            {
                'id': m.pk,
                'fecha': m.fecha.strftime('%d/%m/%Y'),
                'concepto': m.concepto,
                'categoria': m.categoria.nombre if m.categoria else '',
                'importe': float(m.importe),
                'reembolsado': float(m.reembolsado),
                'pendiente': float(m.pendiente_de_reembolso),
                'sugerido': multiplo,
            },
        ))
    filas.sort(key=lambda f: f[0])
    return [f[1] for f in filas[:50]]


def _respuesta_compartido(hogar, gasto_pk):
    """El estado fresco del gasto, recalculado desde la base de datos."""
    gasto = _prefetch_compartido(MovimientoBancario.objects.filter(pk=gasto_pk)).first()
    return JsonResponse({
        'ok': True,
        'sentido': 'gasto',
        'gasto': _estado_compartido(gasto),
        'candidatos': _candidatos_reembolso(hogar, gasto),
    })


def _cabe_reembolso(gasto, importe, excepto=None):
    """¿Cabe `importe` en lo que queda por devolver de este gasto?

    Se comprueba al emparejar: devolverte más de lo que pagaste no es un
    reembolso, y si se dejara pasar la categoría del gasto acabaría en negativo.
    `excepto` es el reembolso que se está editando, que no compite consigo mismo.
    """
    pendiente = gasto.pendiente_de_reembolso
    if excepto is not None and excepto.reembolsa_id == gasto.pk:
        pendiente += excepto.importe
    return importe <= pendiente, pendiente


@login_required
def compartido(request, pk):
    """El estado de un gasto compartido y con qué se puede emparejar.

    Se entra por los dos lados. Desde el GASTO (el caso normal: «esta cena la
    pagué yo, y me devolvieron…») se devuelven lo ya emparejado y los ingresos
    de alrededor. Desde un INGRESO («este Bizum era de la cena») se devuelven los
    gastos a los que puede corresponder; si ya está emparejado, el estado de su
    gasto, para verlo y deshacerlo.
    """
    profile, hogar = _get_hogar(request)
    if not hogar:
        return JsonResponse({'ok': False, 'error': 'sin_hogar'}, status=403)

    mov = get_object_or_404(
        _prefetch_compartido(MovimientoBancario.objects.all()), pk=pk, hogar=hogar,
    )
    if mov.es_reembolso:
        return _respuesta_compartido(hogar, mov.reembolsa_id)
    if _puede_compartirse(mov):
        return _respuesta_compartido(hogar, mov.pk)
    if _puede_ser_reembolso(mov):
        return JsonResponse({
            'ok': True,
            'sentido': 'ingreso',
            'ingreso': _fila_reembolso(mov),
            'candidatos': _gastos_candidatos(hogar, mov),
        })
    return JsonResponse({'ok': False, 'error': 'no_se_puede_compartir'}, status=400)


def _post_compartido(request, pk):
    """Comprobaciones comunes de las acciones que cambian un gasto compartido."""
    profile, hogar = _get_hogar(request)
    if not hogar:
        return None, None, JsonResponse({'ok': False, 'error': 'sin_hogar'}, status=403)
    if request.method != 'POST':
        return None, None, JsonResponse({'ok': False, 'error': 'metodo'}, status=405)
    gasto = get_object_or_404(
        _prefetch_compartido(MovimientoBancario.objects.all()), pk=pk, hogar=hogar,
    )
    if not _puede_compartirse(gasto):
        return None, None, JsonResponse(
            {'ok': False, 'error': 'no_es_un_gasto',
             'mensaje': 'Solo se puede compartir un gasto.'}, status=400,
        )
    return hogar, gasto, None


def _error_no_cabe(pendiente):
    return JsonResponse({
        'ok': False, 'error': 'supera',
        'mensaje': (
            'Supera lo que queda por devolverte de este gasto '
            f'({str(pendiente.quantize(Decimal("0.01"))).replace(".", ",")} €).'
        ),
        'pendiente': float(pendiente),
    }, status=400)


@login_required
def compartido_vincular(request, pk):
    """Empareja un ingreso (un Bizum, una transferencia) con el gasto `pk`."""
    hogar, gasto, error = _post_compartido(request, pk)
    if error:
        return error
    ingreso = MovimientoBancario.objects.filter(
        hogar=hogar, pk=request.POST.get('ingreso') or 0,
    ).prefetch_related('partes').first()
    if not ingreso or ingreso.pk == gasto.pk or not _puede_ser_reembolso(ingreso):
        return JsonResponse({
            'ok': False, 'error': 'ingreso_invalido',
            'mensaje': 'Ese movimiento no puede ser un reembolso.',
        }, status=400)
    cabe, pendiente = _cabe_reembolso(gasto, ingreso.importe, excepto=ingreso)
    if not cabe:
        return _error_no_cabe(pendiente)
    ingreso.reembolsa = gasto
    ingreso.save(update_fields=['reembolsa'])
    return _respuesta_compartido(hogar, gasto.pk)


@login_required
def compartido_efectivo(request, pk):
    """Apunta lo que te dieron en mano por un gasto compartido, o lo corrige.

    Crea un apunte manual que no viene de ningún banco —como un pago en
    efectivo— ya emparejado con el gasto. Con `reembolso` se edita uno que ya
    existe en vez de crear otro.
    """
    hogar, gasto, error = _post_compartido(request, pk)
    if error:
        return error
    try:
        importe = Decimal((request.POST.get('importe') or '').replace(',', '.')).quantize(
            Decimal('0.01'),
        )
    except InvalidOperation:
        importe = None
    if importe is None or importe <= 0:
        return JsonResponse({
            'ok': False, 'error': 'importe',
            'mensaje': 'Pon cuánto te dieron, en positivo.',
        }, status=400)

    existente = None
    if request.POST.get('reembolso'):
        existente = MovimientoBancario.objects.filter(
            hogar=hogar, pk=request.POST.get('reembolso'), reembolsa=gasto, manual=True,
        ).first()
        if not existente:
            return JsonResponse({'ok': False, 'error': 'reembolso_invalido'}, status=400)

    cabe, pendiente = _cabe_reembolso(gasto, importe, excepto=existente)
    if not cabe:
        return _error_no_cabe(pendiente)

    quien = (request.POST.get('quien') or '').strip()[:120]
    fecha = parse_date(request.POST.get('fecha') or '') or gasto.fecha
    concepto = f'Efectivo de {quien}' if quien else 'Efectivo · reembolso'
    mov = existente or MovimientoBancario(
        hogar=hogar, extracto=None, manual=True, reembolsa=gasto,
        estado_categorizacion='manual', saldo=None,
    )
    mov.fecha = fecha
    mov.concepto = concepto[:300]
    mov.concepto_raw = f'{concepto} · {gasto.concepto}'
    mov.comercio = ''
    mov.importe = importe
    mov.save()
    return _respuesta_compartido(hogar, gasto.pk)


@login_required
def compartido_soltar(request, pk):
    """Deshace un reembolso: el ingreso vuelve a contar como lo que era.

    Si era efectivo apuntado a mano, se borra: solo existía para esto, y
    dejarlo suelto lo convertiría en un ingreso que nunca fue.
    """
    hogar, gasto, error = _post_compartido(request, pk)
    if error:
        return error
    ingreso = MovimientoBancario.objects.filter(
        hogar=hogar, pk=request.POST.get('ingreso') or 0, reembolsa=gasto,
    ).first()
    if not ingreso:
        return JsonResponse({'ok': False, 'error': 'ingreso_invalido'}, status=400)
    if ingreso.manual:
        ingreso.delete()
    else:
        ingreso.reembolsa = None
        ingreso.save(update_fields=['reembolsa'])
    return _respuesta_compartido(hogar, gasto.pk)


@login_required
def dividir_movimiento(request, pk):
    """Reparte un cobro en varias partes, cada una con su propia categoría.

    En Norauto pagas de una vez los neumáticos y la revisión anual: son dos
    partidas distintas del presupuesto y hasta ahora no había forma de decirlo,
    porque el movimiento solo admite una categoría.

    El apunte original NO se borra: es lo que dice el banco, es lo que evita que
    reimportar el extracto lo duplique, y es donde se ve el cobro tal cual fue.
    Simplemente deja de sumar y pasan a contar sus partes.
    """
    profile, hogar = _get_hogar(request)
    if not hogar:
        return JsonResponse({'ok': False, 'error': 'sin_hogar'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'metodo'}, status=405)

    mov = get_object_or_404(MovimientoBancario, pk=pk, hogar=hogar)
    if mov.es_parte:
        return JsonResponse({'ok': False, 'error': 'ya_es_parte'}, status=400)

    importes = request.POST.getlist('importe')
    categorias = request.POST.getlist('categoria_id')
    conceptos = request.POST.getlist('concepto')
    if len(importes) < 2:
        return JsonResponse({'ok': False, 'error': 'minimo_dos'}, status=400)

    activos = request.POST.getlist('activo')
    partes = []
    for i, crudo in enumerate(importes):
        try:
            importe = Decimal((crudo or '').replace(',', '.'))
        except InvalidOperation:
            return JsonResponse({'ok': False, 'error': 'importe_invalido'}, status=400)
        if importe == 0:
            return JsonResponse({'ok': False, 'error': 'importe_cero'}, status=400)
        partes.append({
            'importe': importe,
            'categoria_id': (categorias[i] if i < len(categorias) else '') or '',
            'concepto': ((conceptos[i] if i < len(conceptos) else '') or '').strip(),
            # Un recibo puede pagar de golpe el seguro de dos coches: cada parte
            # va a su activo. Si no viene nada, hereda el del cobro, o repartir
            # un gasto imputado a un coche lo borraría de su ficha.
            'activo': (activos[i] if i < len(activos) else None),
        })

    # Las partes tienen que sumar el cobro. Si no cuadran, el reparto no
    # representa lo que pasó y todos los totales quedarían mal.
    suma = sum(p['importe'] for p in partes)
    if suma != mov.importe:
        return JsonResponse({
            'ok': False, 'error': 'no_cuadra',
            'suma': float(suma), 'total': float(mov.importe),
        }, status=400)

    validas = {
        str(c.id): c for c in CategoriaGasto.objects.filter(
            hogar=hogar, id__in=[p['categoria_id'] for p in partes if p['categoria_id']],
        )
    }

    # Se traduce a la forma que entiende el motor, que es la misma que usan el
    # reparto de los recibos parecidos y el de la importación. Que no venga el
    # campo `activo` (None) y que venga vacío ('') son cosas distintas: lo
    # primero es heredar el del cobro y lo segundo, no imputarlo a ninguno.
    resueltas = []
    for p in partes:
        parte = {
            'importe': p['importe'],
            'categoria': validas.get(p['categoria_id']),
            'concepto': p['concepto'],
        }
        if p['activo'] is not None:
            parte['activo'] = costes_activo.resolver(hogar, p['activo'])
        resueltas.append(parte)

    with transaction.atomic():
        reparto.crear_partes(mov, resueltas)

    respuesta = {'ok': True, 'partes': len(resueltas)}

    # Lo mismo que al cambiar una categoría a mano: se ofrece llevar el criterio
    # a los demás recibos del comercio y, después, recordarlo. Repartir a mano
    # el recibo del taller cada vez que llega es el trabajo que hace que la
    # pantalla se abandone a medias.
    if mov.comercio:
        respuesta['sugerencia'] = _sugerencia_division(hogar, mov, resueltas)

    return JsonResponse(respuesta)


def _divisibles(hogar, patron, excluir=None, anio=None, mes=None, desde=None,
                incluir_repartidos=False):
    """Los movimientos del comercio que se pueden repartir, listos para usar.

    Fuera quedan siempre las partes de otro y los de importe cero, que no tienen
    nada que repartir.

    Los que YA están repartidos entran solo con `incluir_repartidos`. Al aprender
    un reparto nuevo se dejan fuera, porque una división automática no puede
    pisar un reparto que alguien revisó a mano. Pero al CORREGIR uno hacen falta:
    son justo los que llevan el reparto equivocado, y sin ellos corregir la parte
    del coche no se podía llevar a ningún sitio.

    Se vuelven a traer enteros a propósito: `_encajan` devuelve los campos justos
    para contar, y repartir necesita el extracto, el importe y el activo de cada
    uno. Sin esto, cada movimiento costaba media docena de consultas sueltas.
    """
    ids = [
        m.id for m in _encajan(
            hogar, patron, incluir_categorizados=True, anio=anio, mes=mes, desde=desde,
        )
        if m.id != excluir
    ]
    if not ids:
        return []
    return [
        m for m in MovimientoBancario.objects.filter(hogar=hogar, id__in=ids)
        .select_related('extracto').prefetch_related('partes')
        if m.importe and not m.es_parte and (incluir_repartidos or not m.esta_dividido)
    ]


def _sugerencia_division(hogar, mov, partes):
    """Qué se puede ofrecer tras repartir: a cuántos recibos llevarlo y desde
    cuándo, y si la regla se queda como está.

    Los recibos YA repartidos cuentan aquí. Antes no, y por eso corregir un
    reparto aprendido no ofrecía nada: los demás recibos ya estaban partidos,
    así que «recibos parecidos» salía cero y la corrección se quedaba en ese
    único apunte.
    """
    similares = _divisibles(hogar, mov.comercio, excluir=mov.pk, incluir_repartidos=True)
    en_adelante = [m for m in similares if m.fecha >= mov.fecha]
    fracciones = reparto.proporciones([p['importe'] for p in partes])

    # La regla que HOY se le aplicaría a este recibo, con el mismo criterio que
    # usa la importación: una de «norauto» ya cubre a «norauto sevilla».
    vigente = _mejor_division(
        mov, list(ReglaDivision.objects.filter(hogar=hogar, activo=True)),
    )
    return {
        'patron': mov.comercio,
        'n_similares': len(similares),
        'n_repartidos': sum(1 for m in similares if m.esta_dividido),
        'n_adelante': len(en_adelante),
        'n_mes': sum(
            1 for m in similares
            if m.fecha.year == mov.fecha.year and m.fecha.month == mov.fecha.month
        ),
        'anio': mov.fecha.year,
        'mes': mov.fecha.month,
        'fecha': mov.fecha.isoformat(),
        'fecha_texto': mov.fecha.strftime('%d/%m/%Y'),
        'etiqueta_mes': f"{MESES_ES[mov.fecha.month]} {mov.fecha.year}",
        'pesos': [round(float(f) * 100, 1) for f in fracciones],
        # Y si ya produce EXACTAMENTE este reparto. Antes bastaba con que
        # existiera alguna, así que después de aprenderla una vez ya no se
        # ofrecía corregirla nunca más.
        'ya_hay_regla': bool(vigente) and reparto.coincide(vigente, partes),
        'regla_desfasada': bool(vigente) and not reparto.coincide(vigente, partes),
    }


@login_required
def aprender_division(request):
    """Lleva un reparto al resto de recibos del mismo comercio, y lo recuerda.

    Es el gemelo de `aprender_regla` para los cobros que son varias cosas a la
    vez: mismo guion —primero hasta dónde llega el cambio, después si además
    debe quedarse— porque es la misma pregunta y aprenderla dos veces no tiene
    sentido.

    El reparto viaja en PROPORCIONES, no en importes: la revisión de este año no
    cuesta lo que la del anterior. Cada recibo se parte en la misma forma.

    Tres alcances, y el que se elige es también desde cuándo vale la regla:

    * `todos`: todo el histórico del comercio, y la regla vale desde siempre.
    * `adelante`: de la fecha de este recibo en adelante. Es el caso de un
      recibo que cambia de forma —el seguro pasa de tres coberturas a cuatro—:
      lo de antes se queda como estaba y la versión nueva rige a partir de ahí.
    * `mes`: solo ese mes.
    """
    profile, hogar = _get_hogar(request)
    if not hogar:
        return JsonResponse({'ok': False, 'error': 'sin_hogar'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'metodo'}, status=405)

    modelo = get_object_or_404(
        MovimientoBancario, pk=request.POST.get('modelo') or 0, hogar=hogar,
    )
    # `order_by` explícito: el orden por defecto de los movimientos es por fecha
    # y las partes comparten fecha, así que salían del revés y el reparto se
    # aplicaba con las proporciones cruzadas de categoría.
    partes_modelo = list(
        modelo.partes.select_related('categoria', 'vehiculo', 'propiedad')
        .order_by('orden_parte')
    )
    if len(partes_modelo) < 2:
        return JsonResponse({'ok': False, 'error': 'sin_reparto'}, status=400)

    patron = normalizar_texto(request.POST.get('patron') or modelo.comercio)
    if not patron:
        return JsonResponse({'ok': False, 'error': 'sin_patron'}, status=400)

    plantilla = [
        {
            'importe': p.importe,
            'categoria': p.categoria,
            'concepto': p.concepto,
            'activo': p.activo_imputado or reparto.HEREDAR,
        }
        for p in partes_modelo
    ]

    ambito = request.POST.get('ambito') or ''
    # La fecha de efecto SALE DEL ALCANCE, no se pregunta aparte: quien decide
    # «de este recibo en adelante» está diciendo exactamente desde cuándo vale.
    # Preguntarlo dos veces sería pedir la misma decisión con otras palabras.
    if ambito == 'todos':
        desde = None                       # la versión de siempre
    elif ambito == 'mes':
        desde = modelo.fecha.replace(day=1)
    else:
        desde = modelo.fecha               # «de aquí en adelante», y «solo este»

    # «Solo recordar»: guarda la versión sin tocar ningún movimiento. Es el
    # segundo paso del aviso —primero se aplica con el alcance elegido y después
    # se pregunta si además debe quedar así para lo que venga—.
    if request.POST.get('accion') == 'solo_regla':
        reparto.guardar_regla(hogar, patron, plantilla, desde=desde)
        return JsonResponse({
            'ok': True, 'aplicados': 0, 'recordada': True,
            'desde': desde.isoformat() if desde else None,
        })

    anio = _entero_o_none(request.POST.get('anio')) if ambito == 'mes' else None
    mes = _entero_o_none(request.POST.get('mes')) if ambito == 'mes' else None
    if ambito == 'mes' and not (anio and mes):
        return JsonResponse({'ok': False, 'error': 'mes_invalido'}, status=400)

    fracciones = reparto.proporciones([p['importe'] for p in plantilla])

    aplicados = 0
    rehechos = 0
    with transaction.atomic():
        candidatos = _divisibles(
            hogar, patron, excluir=modelo.pk, anio=anio, mes=mes,
            desde=modelo.fecha if ambito == 'adelante' else None,
            # Corregir un reparto tiene que alcanzar a los que ya lo llevan mal:
            # son justo los que hay que arreglar.
            incluir_repartidos=True,
        )
        for m in candidatos:
            importes = reparto.repartir(m.importe, fracciones)
            partes = [
                dict(plantilla[i], importe=importe) for i, importe in enumerate(importes)
            ]
            if not reparto.es_division_valida(partes):
                continue
            ya_estaba = m.esta_dividido
            reparto.crear_partes(m, partes)
            aplicados += 1
            rehechos += 1 if ya_estaba else 0

    recordar = request.POST.get('recordar') == '1'
    if recordar:
        reparto.guardar_regla(hogar, patron, plantilla, desde=desde)

    return JsonResponse({
        'ok': True, 'aplicados': aplicados, 'rehechos': rehechos,
        'recordada': recordar, 'desde': desde.isoformat() if desde else None,
    })


@login_required
def deshacer_division(request, pk):
    """Quita las partes y devuelve el movimiento a contar por sí mismo."""
    profile, hogar = _get_hogar(request)
    if not hogar:
        return JsonResponse({'ok': False, 'error': 'sin_hogar'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'metodo'}, status=405)

    mov = get_object_or_404(MovimientoBancario, pk=pk, hogar=hogar)
    borradas, _ = mov.partes.all().delete()
    return JsonResponse({'ok': True, 'borradas': borradas})


@login_required
def filas_del_mes(request):
    """Las filas de un mes concreto, para cuando se despliega en el listado.

    El listado manda los meses plegados sin sus apuntes —si no, ver el mes de
    arriba costaba cargar el histórico entero— y los pide aquí al abrirlos.
    Aplica los MISMOS filtros que la pantalla: lo que se despliega tiene que
    sumar lo que dice la cabecera del mes.
    """
    profile, hogar = _get_hogar(request)
    if not hogar:
        return JsonResponse({'ok': False, 'error': 'sin_hogar'}, status=403)

    anio = _entero_o_none(request.GET.get('anio_mes_anio'))
    mes = _entero_o_none(request.GET.get('anio_mes_mes'))
    if not anio or not mes or not 1 <= mes <= 12:
        return JsonResponse({'ok': False, 'error': 'mes_invalido'}, status=400)

    extracto_id = _entero_o_none(request.GET.get('extracto'))
    base = MovimientoBancario.objects.filter(hogar=hogar)
    if extracto_id:
        # El panel también se usa dentro del detalle de UN extracto; sin esto,
        # desplegar un mes ahí traería los apuntes de todos los extractos.
        base = base.filter(extracto_id=extracto_id)
    todos = list(
        base.select_related('categoria', 'partida_conciliada', 'cubre', 'reembolsa', 'dividido_de')
            .prefetch_related('etiquetas', 'partes', 'coberturas',
                          'dividido_de__partes', 'dividido_de__coberturas', 'reembolsos', 'dividido_de__reembolsos').order_by('-fecha')
    )

    # El año y el mes de la fila mandan sobre los de la URL: se está pidiendo
    # ESE mes, no el que hubiera filtrado la pantalla.
    f = _leer_filtros(request, anio=str(anio), mes=str(mes))
    movimientos = [m for m in todos if _pasa_filtro(m, f)]

    return render(request, 'extractos/_filas_mes.html', {
        'movimientos': movimientos,
        'panel': _contexto_edicion(hogar),
    })


@login_required
def movimientos_de_categoria(request):
    """El desglose de una categoría, para el modal que se abre desde su pilar.

    Devuelve un trozo de HTML, no una página: el objetivo es ver de un vistazo
    qué hay dentro de «Restaurantes» sin perder la pantalla en la que estabas,
    y poder cambiarlo ahí mismo. Reutiliza los filtros de la pantalla —año, mes,
    etiqueta, activo, buscador— y solo fuerza la categoría, para que lo que se
    ve dentro sume exactamente lo que decía la fila de la que se ha entrado.
    """
    profile, hogar = _get_hogar(request)
    if not hogar:
        return JsonResponse({'ok': False, 'error': 'sin_hogar'}, status=403)

    clave = request.GET.get('categoria') or ''
    categoria = (
        CategoriaGasto.objects.filter(hogar=hogar, id=clave).first()
        if clave.isdigit() else None
    )
    if not categoria and clave != 'sin':
        return JsonResponse({'ok': False, 'error': 'categoria_invalida'}, status=404)

    todos = list(
        MovimientoBancario.objects.filter(hogar=hogar)
        .select_related('categoria', 'partida_conciliada', 'cubre', 'reembolsa', 'dividido_de').prefetch_related('etiquetas', 'partes', 'coberturas',
                          'dividido_de__partes', 'dividido_de__coberturas', 'reembolsos', 'dividido_de__reembolsos')
    )
    # El bloque se quita: la categoría ya es más concreta que su pilar, y
    # dejarlo puesto vaciaría la lista justo cuando se entra desde otro bloque
    # (por ejemplo, después de mover la categoría de sitio).
    f = _leer_filtros(request, categoria=str(categoria.id) if categoria else 'sin', bloque='')
    movimientos = sorted(
        (m for m in todos if _pasa_filtro(m, f)), key=lambda m: m.fecha, reverse=True,
    )

    meses_periodo = max(
        len({(m.fecha.year, m.fecha.month) for m in todos if _pasa_periodo(m, f)}), 1,
    )
    # Los FIJOS ANUALES no se miden por meses sino por años: lo que declaras es
    # lo que te va a costar el año, y el pago llega de golpe cuando toca. Su
    # divisor es el número de años que hay a la vista, no el de meses.
    anios_periodo = max(
        len({m.fecha.year for m in todos if _pasa_periodo(m, f)}), 1,
    )
    # Mismo criterio que la pantalla desde la que se abre: si ahí la revisión
    # pesa 272 € porque la reserva puso el resto, aquí dentro también, o el
    # desglose contradice a la fila que se acaba de pinchar.
    vista_de_mes = f['mes'] != 'all'

    # Y el pago anual que la pantalla saca de su mes tampoco pesa aquí. Antes
    # sí: la fila decía 1 € y el modal que se abría desde ella, 273,90 €.
    def peso_de(m):
        if not vista_de_mes:
            return -m.importe_neto
        return Decimal('0') if m.se_saca_del_mes else m.impacto_real

    total = sum((peso_de(m) for m in movimientos if m.cuenta_como_gasto), Decimal('0'))
    es_anual = bool(categoria and categoria.tipo == 'anual')
    limite_mensual = (
        presupuesto.por_categoria(hogar).get(categoria.id, Decimal('0'))
        if categoria else Decimal('0')
    )
    # Un fijo anual se juzga contra lo que provisionas al AÑO y con el recibo
    # entero, igual que su fila: contra la doceava parte, una revisión de
    # 273 € decía «107 € por encima de 167 €» mientras el bloque la daba por
    # buena dentro de sus 3.130 € del año.
    if es_anual:
        limite_categoria = (
            presupuesto.por_categoria(hogar, anual=True).get(categoria.id, Decimal('0'))
            * anios_periodo
        )
        # Hasta el mes que se mira, no solo el mes: como la fila del bloque.
        anterior = _anuales_meses_anteriores(todos, f).get(
            categoria.nombre, {},
        ).get('importe', Decimal('0'))
        medido = anterior + sum(
            (-m.importe_neto for m in movimientos if m.cuenta_como_gasto), Decimal('0'),
        )
    else:
        limite_categoria = limite_mensual * meses_periodo
    # Lo mismo que hace el reparto de la pantalla: la parte que a este mes le
    # toca de un gasto anual ya pagado ocupa su límite, así que el modal tiene
    # que decir la misma cifra que la fila desde la que se abre.
    prorrateo = _anuales_prorrateados(todos, f)
    prorrateado = prorrateo['por_categoria'].get(
        categoria.id if categoria else None, {},
    ).get('importe', Decimal('0'))
    if es_anual:
        prorrateado = Decimal('0')
    else:
        medido = total + prorrateado

    por_mes = defaultdict(lambda: Decimal('0'))
    for m in movimientos:
        if m.cuenta_como_gasto:
            por_mes[(m.fecha.year, m.fecha.month)] += peso_de(m)
    meses = [
        {'etiqueta': f"{MESES_ES[mes]} {anio}", 'importe': importe,
         'pct': float(importe / max(por_mes.values()) * 100) if por_mes else 0}
        for (anio, mes), importe in sorted(por_mes.items(), reverse=True)
    ]

    contexto = {
        'categoria': categoria,
        'nombre': categoria.nombre if categoria else 'Sin categorizar',
        'color': COLOR_TIPO.get(categoria.tipo if categoria else 'sin', '#9aa5a0'),
        'bloque_etiqueta': (
            ETIQUETAS_TIPO.get(categoria.tipo, 'Sin categorizar')
            if categoria else 'Sin categorizar'
        ),
        'movimientos': movimientos,
        'total': total,
        'num': len(movimientos),
        'meses_periodo': meses_periodo,
        'media_mes': total / meses_periodo,
        'periodo_etiqueta': _etiqueta_periodo(f['anio'], f['mes']),
        'meses': meses,
        'comercios': _comercios_del_periodo(
            [m for m in movimientos if not (vista_de_mes and m.se_saca_del_mes)],
            meses_periodo, tope=8, vista_de_mes=vista_de_mes),
        **presupuesto.estado(medido, limite_categoria),
        'limite_mensual': limite_mensual,
        'es_anual': es_anual,
        'restante_anual': limite_categoria - medido if es_anual else None,
        'prorrateado': prorrateado,
        # Lo que se mide contra el límite: el gasto del periodo más la parte que
        # le toca de los anuales ya pagados; en un anual, lo pagado entero.
        'medido': medido,
        # Anidado, no expandido: la plantilla de una fila de movimiento espera
        # este contexto bajo el nombre `panel`, igual que en el listado.
        'contexto_edicion': _contexto_edicion(hogar),
    }
    return render(request, 'extractos/_modal_categoria.html', contexto)


# Acciones que se pueden aplicar a varios movimientos de una vez. Están
# enumeradas a propósito: un endpoint que acepte «el campo que venga» sobre una
# lista de ids es una puerta abierta a cambiar cualquier cosa en bloque.
ACCIONES_LOTE = (
    'categoria', 'etiqueta', 'quitar_etiqueta', 'activo', 'provision',
    'traspaso', 'eliminar',
)


@login_required
def accion_lote(request):
    """Aplica un mismo cambio a varios movimientos seleccionados.

    Categorizar veinte apuntes de uno en uno es el trabajo que hace que la
    pantalla se abandone a medias. Con la selección múltiple, repasar un mes
    entero es marcar y elegir una vez.
    """
    profile, hogar = _get_hogar(request)
    if not hogar:
        return JsonResponse({'ok': False, 'error': 'sin_hogar'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'metodo'}, status=405)

    accion = request.POST.get('accion') or ''
    if accion not in ACCIONES_LOTE:
        return JsonResponse({'ok': False, 'error': 'accion_invalida'}, status=400)

    ids = [i for i in request.POST.getlist('ids') if str(i).isdigit()]
    movimientos = list(MovimientoBancario.objects.filter(hogar=hogar, id__in=ids))
    if not movimientos:
        return JsonResponse({'ok': False, 'error': 'sin_movimientos'}, status=400)

    respuesta = {'ok': True, 'accion': accion, 'num': len(movimientos)}

    if accion == 'categoria':
        crudo = request.POST.get('categoria_id') or ''
        categoria = CategoriaGasto.objects.filter(hogar=hogar, id=crudo).first() if crudo else None
        if crudo and not categoria:
            return JsonResponse({'ok': False, 'error': 'categoria_invalida'}, status=400)
        MovimientoBancario.objects.filter(id__in=[m.id for m in movimientos]).update(
            categoria=categoria,
            estado_categorizacion='manual' if categoria else 'sin_categorizar',
        )
        respuesta['etiqueta'] = categoria.nombre if categoria else 'Sin categorizar'
        # Cambiar veinte apuntes en bloque es exactamente el momento de
        # preguntar si eso ha de quedarse: es el gesto con MÁS criterio detrás y
        # era el único sitio donde no se ofrecía. Se pregunta después, como en
        # la fila suelta: aplicar y recordar son decisiones distintas.
        if categoria:
            respuesta['sugerencia'] = _comercios_a_recordar(hogar, movimientos, categoria)

    elif accion in ('etiqueta', 'quitar_etiqueta'):
        etiqueta = _etiqueta_para_lote(hogar, request, crear=accion == 'etiqueta')
        if not etiqueta:
            return JsonResponse({'ok': False, 'error': 'etiqueta_invalida'}, status=400)
        for m in movimientos:
            if accion == 'etiqueta':
                m.etiquetas.add(etiqueta)
            else:
                m.etiquetas.remove(etiqueta)
        respuesta['etiqueta'] = etiqueta.nombre

    elif accion == 'activo':
        crudo = request.POST.get('activo') or ''
        activo = costes_activo.resolver(hogar, crudo) if crudo else None
        if crudo and not activo:
            return JsonResponse({'ok': False, 'error': 'activo_invalido'}, status=400)
        for m in movimientos:
            costes_activo.asignar(m, activo)
            m.save(update_fields=['propiedad', 'vehiculo'])
        respuesta['etiqueta'] = activo.nombre if activo else 'sin activo'

    elif accion == 'provision':
        crudo = request.POST.get('partida_id') or ''
        partida = (
            PartidaGasto.objects.filter(hogar=hogar, id=crudo).exclude(periodicidad='mensual').first()
            if crudo else None
        )
        if crudo and not partida:
            return JsonResponse({'ok': False, 'error': 'partida_invalida'}, status=400)
        MovimientoBancario.objects.filter(id__in=[m.id for m in movimientos]).update(
            partida_conciliada=partida,
        )
        respuesta['etiqueta'] = partida.nombre if partida else 'no es un pago anual'

    elif accion == 'traspaso':
        # Marcar o desmarcar en bloque: cuando el banco no pone tu nombre, son
        # todos los traspasos de esa cuenta los que entran mal, no uno.
        es_traspaso = request.POST.get('es_traspaso') == '1'
        ids = [m.id for m in movimientos]
        MovimientoBancario.objects.filter(id__in=ids).update(es_traspaso=es_traspaso)
        if not es_traspaso:
            # Igual que en la fila suelta: con la categoría de traspasos puesta,
            # quitar la marca no cambiaría nada.
            MovimientoBancario.objects.filter(
                id__in=ids, categoria__computo=COMPUTO_NEUTRO,
            ).update(categoria=None, estado_categorizacion='sin_categorizar')
        respuesta['etiqueta'] = 'traspaso' if es_traspaso else 'ya no es traspaso'

    elif accion == 'eliminar':
        extractos_afectados = {m.extracto_id for m in movimientos}
        MovimientoBancario.objects.filter(id__in=[m.id for m in movimientos]).delete()
        for extracto in ExtractoBancario.objects.filter(id__in=extractos_afectados):
            extracto.num_movimientos = extracto.movimientos.count()
            extracto.save(update_fields=['num_movimientos'])

    return JsonResponse(respuesta)


def _comercios_a_recordar(hogar, movimientos, categoria):
    """Los comercios del lote que aún no tienen esta categoría como regla.

    Se agrupa por comercio porque la regla se aprende del comercio, no del
    apunte: marcar doce recibos de tres comercios y ponerles «Restaurantes» son
    tres reglas, no doce ni una.
    """
    comercios = sorted({m.comercio for m in movimientos if m.comercio})
    if not comercios:
        return None

    ya_con_regla = set(
        ReglaCategorizacion.objects.filter(
            hogar=hogar, patron__in=comercios, categoria=categoria, activo=True,
        ).values_list('patron', flat=True)
    )
    pendientes = [c for c in comercios if c not in ya_con_regla]
    if not pendientes:
        return None

    return {
        'patrones': pendientes,
        'categoria': categoria.nombre,
        'categoria_id': categoria.id,
    }


def _etiqueta_para_lote(hogar, request, crear):
    """La etiqueta del cambio en bloque: por id si viene elegida, o por nombre
    creándola si hace falta, igual que al etiquetar un movimiento suelto."""
    crudo = request.POST.get('etiqueta_id') or ''
    if crudo:
        return Etiqueta.objects.filter(hogar=hogar, id=crudo).first()
    nombre = (request.POST.get('nombre') or '').strip()[:60]
    if not nombre:
        return None
    existente = Etiqueta.objects.filter(hogar=hogar, nombre__iexact=nombre).first()
    if existente or not crear:
        return existente
    return Etiqueta.objects.create(
        hogar=hogar, nombre=nombre[:60],
        color=Etiqueta.color_sugerido(hogar),
    )


@login_required
def conciliacion(request):
    """La pantalla de Conciliación ya no existe.

    Decía lo mismo que Movimientos —el gasto observado contra el presupuesto
    declarado, bloque a bloque— pero con sus propias cifras y su propio
    periodo, así que había dos pantallas que respondían a la misma pregunta y
    que no siempre respondían lo mismo. Lo que aportaba de más (cuánto llevas
    pagado de cada gasto anual) está ahora dentro del reparto de Movimientos,
    prorrateado al mes que se esté mirando.

    La ruta se conserva redirigiendo porque estaba enlazada desde el propio
    panel y desde los marcadores del usuario.
    """
    parametros = request.GET.urlencode()
    destino = reverse('extractos:listar')
    return redirect(f'{destino}?{parametros}' if parametros else destino)


@login_required
def analisis(request):
    """La pantalla de Análisis ya no existe por separado.

    Todo lo que decía —cuánto llevas frente a tu media de los meses anteriores,
    en qué comercios se ha ido y qué se ha salido del presupuesto— está ahora en
    Movimientos, junto a los apuntes que lo explican: era la misma pregunta
    partida en dos pantallas, y obligaba a saltar de una a otra para cruzar una
    cifra con los movimientos que la componen.

    La ruta se conserva redirigiendo porque estaba enlazada desde la
    conciliación y desde cualquier marcador que el usuario tuviera guardado.
    """
    parametros = request.GET.urlencode()
    destino = reverse('extractos:listar')
    return redirect(f'{destino}?{parametros}' if parametros else destino)


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
    for etiqueta in Etiqueta.objects.filter(hogar=hogar).prefetch_related(
        'movimientos', 'movimientos__reembolsos', 'movimientos__partes',
    ):
        movimientos = list(etiqueta.movimientos.all())
        # Por el cómputo de la categoría y no por el signo del importe: por el
        # signo, un traspaso entre cuentas propias contaba como gasto de la
        # etiqueta y un cobro repartido sumaba su total además del de sus partes.
        gasto = sum((-m.importe_neto for m in movimientos if m.cuenta_como_gasto), Decimal('0'))
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


@login_required
def anuales(request):
    """Los fijos anuales del año: qué se declaró, qué se ha pagado y cuándo."""
    profile, hogar = _get_hogar(request)
    if not hogar:
        messages.error(request, "Necesitas pertenecer a un hogar.")
        return redirect('dashboard')
    anio = _entero_o_none(request.GET.get('anio')) or date.today().year
    datos = analizar_anuales(hogar, anio)
    return render(request, 'extractos/anuales.html', {
        'd': datos,
        'grafico_json': datos['grafico'],
    })


@login_required
def anuales_calendario(request, pk):
    """Cambia el mes de pago de una partida, o la parte en plazos, sin salir
    de Fijos anuales: es ahí donde se ve que falta o que está mal."""
    profile, hogar = _get_hogar(request)
    if not hogar:
        return redirect('dashboard')
    partida = get_object_or_404(PartidaGasto, pk=pk, hogar=hogar)
    anio = _entero_o_none(request.POST.get('anio'))
    destino = reverse('extractos:anuales') + (f'?anio={anio}' if anio else '') + f'#partida-{partida.id}'
    if request.method != 'POST':
        return redirect(destino)
    error = guardar_calendario(
        partida, request.POST.getlist('mes'), request.POST.getlist('importe'),
    )
    if error:
        messages.error(request, f'{partida.nombre}: {error}')
    elif partida.plazos_de_pago:
        messages.success(request, f'{partida.nombre}: se paga en {partida.meses_pago_display.lower()}.')
    elif partida.mes_pago:
        messages.success(request, f'{partida.nombre}: se paga en {partida.get_mes_pago_display().lower()}.')
    else:
        messages.success(request, f'{partida.nombre}: sin mes de pago.')
    return redirect(destino)


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


def _encajan(hogar, patron, incluir_categorizados=False, anio=None, mes=None, desde=None):
    """Movimientos del hogar cuyo comercio o concepto contiene `patron`.

    Es el criterio ÚNICO de «este movimiento es de ese comercio»: lo usan tanto
    el aviso que cuenta cuántos hay como la aplicación que los cambia, para que
    el número que se ofrece sea exactamente el que acaba cambiando.

    Por defecto solo mira lo que está sin categorizar, para que aprender una
    regla nueva no pise clasificaciones que el usuario ya había dado por buenas.
    Con `anio` y `mes` se acota a ese mes: el caso de quien revisa un mes
    concreto y no quiere tocar el histórico. Con `desde`, de esa fecha en
    adelante: el caso de un recibo que ha cambiado de forma y hay que corregir
    a partir de ahí sin reescribir lo de antes.
    """
    patron = normalizar_texto(patron)
    if not patron:
        return []

    candidatos = MovimientoBancario.objects.filter(hogar=hogar, es_traspaso=False)
    if not incluir_categorizados:
        candidatos = candidatos.filter(categoria__isnull=True)
    if anio and mes:
        candidatos = candidatos.filter(fecha__year=anio, fecha__month=mes)
    if desde:
        candidatos = candidatos.filter(fecha__gte=desde)

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
    """La pantalla de «Sin categorizar» ya no existe por separado.

    Hacía una cosa —agrupar lo que quedó en blanco y nombrarlo de una vez— que
    Movimientos ya hace mejor: se filtra por «Sin categorizar», se marcan los
    que sean y se cambian en bloque, o se cambia uno y el aviso ofrece aplicarlo
    a todo su comercio y recordarlo como regla. Tener las dos era mantener dos
    sitios donde clasificar, y solo uno con los filtros y el buscador.

    La ruta se conserva redirigiendo al listado con el filtro puesto, porque
    estaba enlazada desde la conciliación, desde las reglas y desde los
    marcadores del usuario.
    """
    destino = reverse('extractos:listar')
    return redirect(f'{destino}?categoria=sin')


@login_required
def aprender_regla(request):
    """Crea (o actualiza) una regla y la aplica a los movimientos que encajen.

    Es el punto único que usan tanto el formulario de la pantalla antigua como el
    aviso que sale al cambiar la categoría en el listado."""
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
        return redirect(f"{reverse('extractos:listar')}?categoria=sin")

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
        return redirect(f"{reverse('extractos:listar')}?categoria=sin")

    # Alcance del cambio. 'mes' lo acota al mes que se está revisando; por
    # defecto se aplica a todo el histórico, que es lo que hacía siempre.
    solo_mes = request.POST.get('ambito') == 'mes'
    anio = _entero_o_none(request.POST.get('anio')) if solo_mes else None
    mes = _entero_o_none(request.POST.get('mes')) if solo_mes else None
    if solo_mes and not (anio and mes):
        if es_ajax:
            return JsonResponse({'ok': False, 'error': 'mes_invalido'}, status=400)
        messages.error(request, "No se ha podido identificar el mes a corregir.")
        return redirect(f"{reverse('extractos:listar')}?categoria=sin")

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
    return redirect(f"{reverse('extractos:listar')}?categoria=sin")


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

        # Las divisiones aprendidas se gestionan aquí también: sin un sitio
        # donde verlas y borrarlas, una división mal aprendida seguiría
        # partiendo recibos en cada importación sin forma de pararla.
        if accion in ('eliminar_division', 'alternar_division'):
            division = get_object_or_404(
                ReglaDivision, pk=request.POST.get('regla_id'), hogar=hogar,
            )
            if accion == 'eliminar_division':
                division.delete()
                messages.success(
                    request,
                    "Regla de reparto eliminada. Los recibos ya repartidos no cambian: "
                    "para deshacer uno, entra en su fila.",
                )
            else:
                division.activo = not division.activo
                division.save(update_fields=['activo'])
                messages.success(
                    request,
                    f"Reparto de «{division.patron}» "
                    f"{'activado' if division.activo else 'desactivado'}.",
                )
            return redirect('extractos:reglas')

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
        'divisiones': (
            ReglaDivision.objects.filter(hogar=hogar)
            .prefetch_related('partes__categoria', 'partes__vehiculo', 'partes__propiedad')
        ),
        'bloques_categorias': _categorias_por_bloque(hogar),
    })


@login_required
def grabar(request):
    """Graba los movimientos: dejan de colgar de sus extractos y estos se borran.

    Hasta ahora cada apunte vivía dentro del archivo del que salió, y borrar el
    extracto se llevaba por delante sus movimientos, con las categorías, los
    repartos y las reservas que les hubieras puesto. Grabar los deja en la base
    de datos por su cuenta y borra los extractos ya grabados: no es opcional,
    porque un extracto vacío que sigue en la lista invita a reimportarlo o a
    borrarlo pensando que se lleva algo, y la deduplicación por huella ya
    impide que volver a subir el mismo archivo los duplique.

    Todo o nada: si algo falla a medias no puede quedar un extracto borrado con
    sus movimientos todavía dentro.
    """
    profile, hogar = _get_hogar(request)
    if not hogar:
        return redirect('dashboard')
    if request.method != 'POST':
        return redirect('extractos:listar')

    with transaction.atomic():
        extractos = ExtractoBancario.objects.filter(hogar=hogar)
        num_extractos = extractos.count()
        num_movimientos = MovimientoBancario.objects.filter(
            hogar=hogar, extracto__in=extractos,
        ).update(extracto=None)
        extractos.delete()

    if num_extractos:
        messages.success(
            request,
            f"Grabados {num_movimientos} movimientos. Los {num_extractos} extractos "
            "de los que venían se han borrado: los movimientos ya no dependen de ellos.",
        )
    else:
        messages.info(request, "No había extractos pendientes de grabar.")
    return redirect('extractos:listar')


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
