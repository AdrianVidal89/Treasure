"""El cuadre de un mes: lo que dicen los extractos frente a lo que dicen los saldos.

Hay dos formas de medir lo ahorrado en un mes, y deberían dar casi lo mismo:

  · EXTRACTOS: ingresos − gastos de los movimientos del mes.
  · EVOLUCIÓN: la liquidez al cierre del mes menos la del mes anterior, con los
    saldos que apuntas a mano cuenta por cuenta.

No dan lo mismo por diseño, y cuando la diferencia es grande hay que poder ver
de dónde sale sin ir movimiento a movimiento. El balance de Extractos no es un
flujo de caja: es la cifra que se compara con el presupuesto, y por eso

  · descuenta lo que puso la reserva (el dinero salió de otra cuenta tuya, pero
    salió) y saca del mes los pagos anuales sin emparejar;
  · cuenta los gastos compartidos solo por tu parte, aunque el Bizum llegue
    otro mes o te lo dieran en efectivo;
  · no cuenta los traspasos, pero un traspaso a una cuenta que no está en la
    liquidez (la de inversión) o que no has importado sí mueve los saldos.

Y la liquidez de Evolución lleva cosas que ningún extracto ve: el interés de
los depósitos, las cuentas que no importas, el saldo que no apuntaste.

Aquí se pone todo eso en fila, en euros, para que lo que quede sin explicar
sea de verdad lo que no cuadra.
"""
from collections import defaultdict
from decimal import Decimal

from .normalizacion import normalizar_texto

CERO = Decimal('0')
UMBRAL = Decimal('0.50')


def _mes_anterior(anio, mes):
    return (anio - 1, 12) if mes == 1 else (anio, mes - 1)


def _saldo_final(lineas):
    """El saldo con el que el banco cierra el mes, si los apuntes lo traen.

    Varios apuntes el último día no dicen por sí solos cuál fue el último: el
    último es el que no es «el saldo de antes» de ningún otro de ese día.
    """
    con_saldo = [m for m in lineas if m.saldo is not None]
    if not con_saldo:
        return None
    ultimo_dia = max(m.fecha for m in con_saldo)
    del_dia = [m for m in con_saldo if m.fecha == ultimo_dia]
    previos = {m.saldo - m.importe for m in del_dia}
    finales = [m for m in del_dia if m.saldo not in previos] or del_dia
    return max(finales, key=lambda m: m.pk).saldo


def _encaja(banco, fondo):
    """¿Es este fondo una cuenta de ese banco? Por el nombre, que es lo único
    que los une: el extracto dice «CaixaBank» y el fondo «CaixaBank · Adrian»."""
    b = normalizar_texto(banco)
    if not b:
        return False
    texto = normalizar_texto(f'{fondo.cuenta_asociada} {fondo.nombre}')
    return b in texto or any(p and p in b for p in normalizar_texto(fondo.cuenta_asociada).split())


def _lado_extractos(hogar, anio, mes, request):
    from .models import MovimientoBancario
    from .views import _leer_filtros, _panel_context

    todos = list(
        MovimientoBancario.objects.filter(hogar=hogar)
        .select_related('categoria', 'partida_conciliada', 'cubre', 'reembolsa',
                        'dividido_de', 'extracto')
        .prefetch_related('etiquetas', 'partes', 'coberturas', 'dividido_de__partes',
                          'dividido_de__coberturas', 'reembolsos', 'dividido_de__reembolsos')
    )
    filtros = _leer_filtros(request, anio=str(anio), mes=str(mes), categoria='all',
                            bloque='', etiqueta='', activo='', busqueda='',
                            ver_traspasos=True)
    panel = _panel_context(hogar, todos, request, filtros=filtros)

    del_mes = [m for m in todos if m.fecha.year == anio and m.fecha.month == mes]
    # Lo que escribió el banco: los apuntes originales, no las partes que
    # salen de repartir uno (sumarían el mismo dinero dos veces), ni lo metido
    # a mano, que no ha pasado por ninguna cuenta.
    banco = [m for m in del_mes if not m.es_parte and not m.manual]
    caja = sum((m.importe for m in banco), CERO)

    neutros = [m for m in banco if m.es_neutro and not m.es_reembolso]
    neutros_netos = sum((m.importe for m in neutros), CERO)
    reembolsos_banco = sum((m.importe for m in banco if m.es_reembolso), CERO)
    a_mano = sum(
        (m.importe for m in del_mes
         if m.manual and not m.es_parte and not m.es_neutro and not m.es_reembolso),
        CERO,
    )

    puente = [
        {
            'texto': 'Lo que puso la reserva',
            'detalle': 'Pagos cubiertos con dinero apartado: el balance solo cuenta la diferencia, '
                       'pero el pago entero salió de tus cuentas.',
            'importe': -panel['cubierto_reserva'],
        },
        {
            'texto': 'Pagos anuales sacados del mes',
            'detalle': 'Se reparten en el año para el presupuesto, pero se pagaron este mes.',
            'importe': -panel['total_provisiones'],
        },
        {
            'texto': 'Reembolsos que no llegaron al banco este mes',
            'detalle': 'El gasto compartido ya cuenta solo tu parte; aquí va lo que te devolvieron '
                       'en efectivo o en otro mes (o lo que llegó este mes de gastos de otro).',
            'importe': reembolsos_banco - panel['reembolsado'],
        },
        {
            'texto': 'Apuntes a mano',
            'detalle': 'Pagos en efectivo y demás metidos a mano: cuentan en el balance, pero no '
                       'pasan por el banco.',
            'importe': -a_mano,
        },
        {
            'texto': 'Traspasos y neutros',
            'detalle': 'Entre cuentas importadas se anulan. Lo que queda es dinero que entra o sale '
                       'hacia cuentas cuyos extractos no están (inversión, depósitos, otra persona…).',
            'importe': neutros_netos,
        },
    ]
    explicado = panel['kpi_neto'] + sum((p['importe'] for p in puente), CERO)
    resto = caja - explicado
    if abs(resto) >= Decimal('0.01'):
        puente.append({
            'texto': 'Otros ajustes',
            'detalle': 'Coberturas o reembolsos por encima del gasto y redondeos.',
            'importe': resto,
        })
    puente = [p for p in puente if p['importe']]

    por_categoria = defaultdict(lambda: {'importe': CERO, 'num': 0, 'ejemplos': []})
    for m in neutros:
        clave = m.categoria.nombre if m.categoria else 'Traspaso (sin categoría)'
        fila = por_categoria[clave]
        fila['importe'] += m.importe
        fila['num'] += 1
    for m in sorted(neutros, key=lambda m: -abs(m.importe)):
        clave = m.categoria.nombre if m.categoria else 'Traspaso (sin categoría)'
        if len(por_categoria[clave]['ejemplos']) < 4:
            por_categoria[clave]['ejemplos'].append(m)
    neutros_categorias = sorted(
        ({'nombre': k, **v} for k, v in por_categoria.items()),
        key=lambda f: -abs(f['importe']),
    )

    bancos = defaultdict(list)
    for m in banco:
        nombre = (m.extracto.nombre_banco if m.extracto else '') or 'Sin banco'
        bancos[nombre].append(m)

    return {
        'panel': panel,
        'balance': panel['kpi_neto'],
        'ingresos': panel['kpi_ingresos'],
        'caja': caja,
        'puente': puente,
        'neutros_categorias': neutros_categorias,
        'bancos': {
            nombre: {'variacion': sum((m.importe for m in lineas), CERO),
                     'saldo_final': _saldo_final(lineas), 'num': len(lineas)}
            for nombre, lineas in bancos.items()
        },
    }


def _lado_evolucion(hogar, anio, mes):
    from finanzas.models import FondoFamiliar
    from finanzas.views_evolucion import (
        _depositos_del_hogar, celdas_del_mes, saldos_del_mes, MESES_NOMBRES,
    )

    fondos = list(FondoFamiliar.objects.filter(hogar=hogar, activo=True).order_by('orden', 'nombre'))
    depositos = _depositos_del_hogar(hogar)
    a_ant, m_ant = _mes_anterior(anio, mes)
    saldos = saldos_del_mes(hogar, anio, mes)
    saldos_ant = saldos_del_mes(hogar, a_ant, m_ant)
    if not saldos or not saldos_ant:
        return {'hay': False, 'mes_anterior': MESES_NOMBRES[m_ant]}

    celdas, deps, liquidez, _ = celdas_del_mes(
        hogar, anio, mes, fondos=fondos, depositos=depositos, saldos=saldos)
    celdas_ant, deps_ant, liquidez_ant, _ = celdas_del_mes(
        hogar, a_ant, m_ant, fondos=fondos, depositos=depositos, saldos=saldos_ant)

    filas = []
    for c, c_ant in zip(celdas, celdas_ant):
        antes = c_ant['saldo_valor'] or CERO
        ahora = c['saldo_valor'] or CERO
        filas.append({
            'fondo': c['fondo'],
            'nombre': c['fondo'].nombre,
            'liquido': c['fondo'].tipo_fondo in ('comun', 'ahorro'),
            'antes': antes,
            'ahora': ahora,
            'delta': ahora - antes,
            'auto': c['auto_deposito'],
            'arrastrado': c.get('arrastrado_de') or '',
            'arrastrado_antes': c_ant.get('arrastrado_de') or '',
        })
    for d, d_ant in zip(deps, deps_ant):
        antes = d_ant['valor'] or CERO
        ahora = d['valor'] or CERO
        filas.append({
            'fondo': None,
            'nombre': d['deposito'].nombre,
            'liquido': True,
            'antes': antes,
            'ahora': ahora,
            'delta': ahora - antes,
            'auto': True,
            'arrastrado': '',
            'arrastrado_antes': '',
        })
    return {
        'hay': True,
        'mes_anterior': MESES_NOMBRES[m_ant],
        'liquidez': liquidez,
        'liquidez_ant': liquidez_ant,
        'ahorro': liquidez - liquidez_ant,
        'filas': filas,
    }


def cuadre_del_mes(hogar, anio, mes, request):
    from finanzas.cierres import flujo_del_mes
    from finanzas.views_evolucion import MESES_NOMBRES

    ext = _lado_extractos(hogar, anio, mes, request)
    evo = _lado_evolucion(hogar, anio, mes)

    # Cuenta a cuenta: cada banco importado frente a los fondos que son suyos.
    # Es donde se ve la cuenta concreta que no cuadra.
    cuentas = []
    usados = set()
    if evo['hay']:
        for nombre, datos in sorted(ext['bancos'].items(), key=lambda kv: kv[0].lower()):
            # Solo lo líquido: un fondo de inversión cambia con la bolsa, no
            # con los apuntes, y metido aquí la cuenta nunca cuadraría.
            suyos = [f for f in evo['filas'] if f['fondo'] and not f['auto'] and f['liquido']
                     and id(f) not in usados and _encaja(nombre, f['fondo'])]
            usados.update(id(f) for f in suyos)
            delta = sum((f['delta'] for f in suyos), CERO)
            ahora = sum((f['ahora'] for f in suyos), CERO)
            cuentas.append({
                'banco': nombre,
                'variacion': datos['variacion'],
                'saldo_final': datos['saldo_final'],
                'num': datos['num'],
                'fondos': suyos,
                'delta_fondos': delta,
                'saldo_fondos': ahora,
                'diferencia': (delta - datos['variacion']) if suyos else None,
                'no_cuadra': bool(suyos) and abs(delta - datos['variacion']) >= UMBRAL,
            })
        sin_extracto = [f for f in evo['filas'] if id(f) not in usados and f['delta']]
    else:
        sin_extracto = []

    # Ingresos: Evolución usa los de Distribución para deducir el gasto real
    # (ingreso − ahorro). Si no son los que dicen los extractos, ese gasto
    # también está mal.
    try:
        ingreso_evo = flujo_del_mes(hogar, mes, anio)['ingreso_base_hogar']
    except Exception:  # pragma: no cover - Distribución sin configurar
        ingreso_evo = None

    liquido_fuera = sum(
        (f['delta'] for f in sin_extracto if f['liquido']), CERO,
    ) if evo['hay'] else CERO
    return {
        'anio': anio,
        'mes': mes,
        'mes_nombre': MESES_NOMBRES[mes],
        'ext': ext,
        'evo': evo,
        'cuentas': cuentas,
        'sin_extracto': sin_extracto,
        'liquido_fuera': liquido_fuera,
        'diferencia': (evo['ahorro'] - ext['balance']) if evo['hay'] else None,
        'diferencia_caja': (evo['ahorro'] - ext['caja']) if evo['hay'] else None,
        'ingreso_evo': ingreso_evo,
        'ingreso_ext': ext['ingresos'],
        'ingresos_distintos': (
            ingreso_evo is not None and abs(ingreso_evo - ext['ingresos']) >= Decimal('50')
        ),
    }
