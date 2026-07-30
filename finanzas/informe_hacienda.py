"""Informe de plusvalías para Hacienda.

Para un año fiscal dado, recorre todas las inversiones del hogar y calcula, por
cada VENTA, la ganancia o pérdida patrimonial materializada, usando el método de
**coste medio ponderado** (AVCO móvil): cada venta se valora al precio medio de
compra vigente en ese momento, y las unidades vendidas se retiran del pool (de
modo que el precio medio no cambia al vender, solo al comprar).

Nota fiscal: para valores homogéneos, la Agencia Tributaria aplica por defecto el
criterio FIFO (primero en entrar, primero en salir). Este informe usa coste medio
ponderado —coherente con el resto de la app— por lo que debe tomarse como una
estimación orientativa; conviene contrastarlo con un asesor antes de declarar.
"""
from decimal import Decimal

from .models import Inversion

# Tramos de la base imponible del ahorro (IRPF España), vigentes desde 2023.
# (base_desde, base_hasta_o_None, porcentaje)
TRAMOS_AHORRO_ES = [
    (Decimal('0'), Decimal('6000'), Decimal('19')),
    (Decimal('6000'), Decimal('50000'), Decimal('21')),
    (Decimal('50000'), Decimal('200000'), Decimal('23')),
    (Decimal('200000'), Decimal('300000'), Decimal('27')),
    (Decimal('300000'), None, Decimal('28')),
]


def _impuesto_base_ahorro(base):
    """Cuota estimada aplicando los tramos progresivos de la base del ahorro."""
    base = Decimal(base)
    if base <= 0:
        return Decimal('0')
    impuesto = Decimal('0')
    for desde, hasta, pct in TRAMOS_AHORRO_ES:
        if base <= desde:
            break
        tope = base if hasta is None else min(base, hasta)
        porcion = tope - desde
        if porcion > 0:
            impuesto += porcion * pct / Decimal('100')
    return round(impuesto, 2)


def _titular(usuario):
    return usuario.first_name or usuario.username


def calcular_informe_ventas(hogar, anio):
    """Devuelve un dict con las ventas del año y los totales/estimación fiscal."""
    inversiones = (
        Inversion.objects
        .filter(usuario__userprofile__hogar=hogar)
        .select_related('usuario')
        .prefetch_related('movimientos')
    )

    ventas = []
    for inv in inversiones:
        movimientos = [
            m for m in inv.movimientos.all()
            if m.tipo in ('COMPRA', 'VENTA')
        ]
        movimientos.sort(key=lambda m: (m.fecha, m.id))

        total_invertido = Decimal('0')
        total_unidades = Decimal('0')
        for m in movimientos:
            if m.tipo == 'COMPRA':
                total_invertido += m.cantidad * m.precio_unitario
                total_unidades += m.cantidad
                continue

            # VENTA: se valora al PMC vigente y se retiran las unidades del pool.
            pmc = round(total_invertido / total_unidades, 8) if total_unidades > 0 else Decimal('0')
            coste = round(pmc * m.cantidad, 2)
            importe_venta = round(m.precio_unitario * m.cantidad, 2)
            ganancia = round(importe_venta - coste - m.comision, 2)

            total_invertido -= pmc * m.cantidad
            total_unidades -= m.cantidad
            if total_unidades < 0:
                total_unidades = Decimal('0')
                total_invertido = Decimal('0')

            if m.fecha.year == anio:
                ventas.append({
                    'fecha': m.fecha,
                    'inversion': inv.nombre,
                    'ticker': inv.ticker or '',
                    'tipo_activo': inv.get_tipo_display(),
                    'plataforma': inv.plataforma or '',
                    'titular': _titular(inv.usuario),
                    'cantidad': m.cantidad,
                    'precio_venta': m.precio_unitario,
                    'importe_venta': importe_venta,
                    'pmc': pmc,
                    'coste_adquisicion': coste,
                    'comision': m.comision,
                    'ganancia': ganancia,
                })

    ventas.sort(key=lambda v: v['fecha'])

    total_importe = sum((v['importe_venta'] for v in ventas), Decimal('0'))
    total_coste = sum((v['coste_adquisicion'] for v in ventas), Decimal('0'))
    total_comisiones = sum((v['comision'] for v in ventas), Decimal('0'))
    ganancias_positivas = sum((v['ganancia'] for v in ventas if v['ganancia'] > 0), Decimal('0'))
    perdidas = sum((v['ganancia'] for v in ventas if v['ganancia'] < 0), Decimal('0'))
    ganancia_neta = sum((v['ganancia'] for v in ventas), Decimal('0'))

    impuesto_estimado = _impuesto_base_ahorro(max(ganancia_neta, Decimal('0')))
    tipo_efectivo = (
        round(impuesto_estimado / ganancia_neta * 100, 2)
        if ganancia_neta > 0 else Decimal('0')
    )

    return {
        'anio': anio,
        'ventas': ventas,
        'num_ventas': len(ventas),
        'total_importe': total_importe,
        'total_coste': total_coste,
        'total_comisiones': total_comisiones,
        'ganancias_positivas': ganancias_positivas,
        'perdidas': perdidas,
        'ganancia_neta': ganancia_neta,
        'impuesto_estimado': impuesto_estimado,
        'tipo_efectivo': tipo_efectivo,
        'tramos': TRAMOS_AHORRO_ES,
    }
