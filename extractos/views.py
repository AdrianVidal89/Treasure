from collections import defaultdict
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.shortcuts import get_object_or_404, redirect, render

from finanzas.models import CategoriaGasto, CuentaBancaria, PartidaGasto
from finanzas.parsing import leer_csv

from .categorizacion import categorizar_por_codigo
from .models import ExtractoBancario, MovimientoBancario
from .parser import parse_extracto

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

    resumen = MovimientoBancario.objects.filter(hogar=hogar).aggregate(
        total=Sum('importe'),
    )
    total_movimientos = MovimientoBancario.objects.filter(hogar=hogar).count()
    sin_categorizar = MovimientoBancario.objects.filter(
        hogar=hogar, estado_categorizacion='sin_categorizar', importe__lt=0,
    ).count()

    return render(request, 'extractos/listar.html', {
        'extractos': extractos,
        'total_extractos': extractos.count(),
        'total_movimientos': total_movimientos,
        'saldo_neto': resumen['total'] or Decimal('0'),
        'sin_categorizar': sin_categorizar,
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

    nombre_banco = (request.POST.get('nombre_banco') or '').strip()
    cuenta_id = request.POST.get('cuenta') or None
    cuenta = None
    if cuenta_id:
        cuenta = CuentaBancaria.objects.filter(
            usuario__userprofile__hogar=hogar, id=cuenta_id,
        ).first()

    # Categorías del hogar indexadas por nombre para el matcheo "por código".
    categorias_por_nombre = {
        c.nombre: c for c in CategoriaGasto.objects.filter(hogar=hogar, activo=True)
    }

    total_creados = 0
    total_duplicados = 0
    total_categorizados = 0
    total_errores = 0
    extractos_ok = 0

    for archivo in archivos:
        texto = leer_csv(archivo)
        movimientos, errores = parse_extracto(texto)
        total_errores += len(errores)

        if not movimientos:
            for e in errores[:3]:
                messages.warning(request, f"{archivo.name}: {e}")
            continue

        extracto = ExtractoBancario.objects.create(
            hogar=hogar,
            usuario=request.user,
            nombre_banco=nombre_banco,
            cuenta=cuenta,
            archivo_nombre=archivo.name[:255],
        )

        creados = 0
        fechas = []
        for mov in movimientos:
            hash_mov = MovimientoBancario.calcular_hash(
                mov['fecha'], mov['concepto'], mov['importe'], mov['saldo'],
            )
            if MovimientoBancario.objects.filter(hogar=hogar, hash_dedupe=hash_mov).exists():
                total_duplicados += 1
                continue

            categoria = None
            estado = 'sin_categorizar'
            # Solo se categoriza automáticamente el gasto (importe negativo).
            if mov['importe'] < 0:
                nombre_cat = categorizar_por_codigo(mov['concepto'])
                if nombre_cat and nombre_cat in categorias_por_nombre:
                    categoria = categorias_por_nombre[nombre_cat]
                    estado = 'por_codigo'
                    total_categorizados += 1

            MovimientoBancario.objects.create(
                extracto=extracto,
                hogar=hogar,
                fecha=mov['fecha'],
                concepto=mov['concepto'][:300],
                importe=mov['importe'],
                saldo=mov['saldo'],
                categoria=categoria,
                estado_categorizacion=estado,
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

    if total_creados:
        messages.success(
            request,
            f"Importados {total_creados} movimientos en {extractos_ok} extracto(s). "
            f"{total_categorizados} categorizados por código."
        )
    if total_duplicados:
        messages.info(request, f"{total_duplicados} movimientos duplicados ignorados.")
    if not total_creados and not total_duplicados:
        messages.error(request, "No se pudo importar ningún movimiento. Revisa el formato del CSV.")

    return redirect('extractos:listar')


@login_required
def detalle(request, pk):
    profile, hogar = _get_hogar(request)
    if not hogar:
        messages.error(request, "Necesitas pertenecer a un hogar.")
        return redirect('dashboard')

    extracto = get_object_or_404(ExtractoBancario, pk=pk, hogar=hogar)
    movimientos = extracto.movimientos.select_related('categoria').all()

    # Agrupación por mes (año-mes) preservando orden descendente.
    grupos_mes = defaultdict(lambda: {'movimientos': [], 'ingresos': Decimal('0'), 'gastos': Decimal('0')})
    for m in movimientos:
        clave = (m.fecha.year, m.fecha.month)
        g = grupos_mes[clave]
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

    # Agrupación por categoría (solo gastos).
    por_categoria = defaultdict(lambda: Decimal('0'))
    for m in movimientos:
        if m.importe < 0:
            nombre = m.categoria.nombre if m.categoria else 'Sin categorizar'
            por_categoria[nombre] += m.importe
    categorias = sorted(por_categoria.items(), key=lambda kv: kv[1])

    return render(request, 'extractos/detalle.html', {
        'extracto': extracto,
        'grupos': grupos,
        'categorias': categorias,
    })


@login_required
def conciliacion(request):
    """Cruza los movimientos observados (gasto) contra lo declarado en Gastos."""
    profile, hogar = _get_hogar(request)
    if not hogar:
        messages.error(request, "Necesitas pertenecer a un hogar.")
        return redirect('dashboard')

    movimientos = MovimientoBancario.objects.filter(hogar=hogar, importe__lt=0).select_related('categoria')

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
