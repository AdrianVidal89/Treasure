"""El mes real, resumido para el Dashboard.

El Dashboard enseñaba solo el plan —lo que declaras que entra y sale—, y lo
que de verdad estaba pasando había que ir a buscarlo a Extractos. Aquí se
calcula con EL MISMO panel de Extractos, no con una suma paralela: en cuanto
hay dos cálculos del gasto del mes, las dos pantallas dejan de coincidir.
"""
from datetime import date

from .models import MovimientoBancario
from .views import _leer_filtros, _panel_context

# Cuántas filas de cada lado se enseñan: el Dashboard es para mirar de un
# vistazo; el detalle entero está a un clic.
FILAS_POR_LADO = 3
FILAS_A_REVISAR = 4


def _periodo(movimientos):
    """El mes en curso, o el último con apuntes si este aún no tiene ninguno."""
    hoy = date.today()
    con_datos = {(m.fecha.year, m.fecha.month) for m in movimientos}
    if (hoy.year, hoy.month) in con_datos or not con_datos:
        return hoy.year, hoy.month
    return max(con_datos)


def resumen_del_mes(hogar, request):
    """Lo que el Dashboard enseña del mes real, o None si no hay movimientos."""
    todos = list(
        MovimientoBancario.objects.filter(hogar=hogar)
        .select_related('categoria', 'partida_conciliada', 'cubre', 'reembolsa', 'dividido_de')
        .prefetch_related('etiquetas', 'partes', 'coberturas',
                          'dividido_de__partes', 'dividido_de__coberturas', 'reembolsos', 'dividido_de__reembolsos')
    )
    if not todos:
        return None

    anio, mes = _periodo(todos)
    filtros = _leer_filtros(request, anio=str(anio), mes=str(mes), categoria='all',
                            bloque='', etiqueta='', activo='', busqueda='',
                            ver_traspasos=True)
    panel = _panel_context(hogar, todos, request, filtros=filtros)

    comparativa = panel['comparativa'] or {}
    puente = comparativa.get('puente') or []
    sube = sorted((p for p in puente if p['desviacion'] > 0),
                  key=lambda p: p['desviacion'], reverse=True)[:FILAS_POR_LADO]
    baja = sorted((p for p in puente if p['desviacion'] < 0),
                  key=lambda p: p['desviacion'])[:FILAS_POR_LADO]
    # Las barras del resumen se escalan entre ellas, no contra el puente
    # entero: con solo seis filas, la mayor tiene que llenar su mitad.
    tope = max((abs(p['desviacion']) for p in sube + baja), default=0)
    filas = [
        dict(p, pct_resumen=float(abs(p['desviacion']) / tope * 100) if tope else 0)
        for p in sube + list(reversed(baja))
    ]

    fp = panel['fuera_presupuesto']
    return {
        'anio': anio,
        'mes': mes,
        'etiqueta': panel['periodo_etiqueta'],
        'url': f'/extractos/?anio={anio}&mes={mes}',
        'gasto': panel['kpi_gasto_abs'],
        'ingresos': panel['kpi_ingresos'],
        # Lo que te devolvieron de los gastos compartidos: el gasto de arriba
        # ya lo tiene descontado, y sin decirlo parecería que falta dinero.
        'reembolsado': panel['reembolsado'],
        'presupuesto': panel['presupuesto'],
        'comparativa': comparativa if comparativa.get('hay_referencia') else None,
        'filas_media': filas,
        'no_previsto': fp.get('no_previsto', 0),
        'exceso_categorias': fp.get('exceso_categorias', 0),
        'total_sin_presupuesto': fp.get('total_sin_presupuesto', 0),
        'num_pasadas': len(fp.get('categorias', [])),
        'num_sin_presupuesto': len(fp.get('sin_presupuesto', [])),
        'revisar': (fp.get('revisar') or [])[:FILAS_A_REVISAR],
        'num_revisar': len(fp.get('revisar') or []),
    }
