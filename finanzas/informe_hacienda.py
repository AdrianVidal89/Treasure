"""Informe de plusvalías para Hacienda.

Para un año fiscal dado, recorre todas las inversiones del hogar y calcula, por
cada VENTA, la ganancia o pérdida patrimonial materializada usando el criterio
**FIFO** (primero en entrar, primero en salir), que es el que exige la Agencia
Tributaria para valores homogéneos: cada venta consume primero las unidades
compradas más antiguas. El valor de adquisición incluye las comisiones de compra
(prorrateadas por unidad) y el valor de transmisión descuenta la comisión de la
venta, tal como establece la normativa del IRPF.

Es una herramienta de apoyo para declarar sin trampas; aun así, conviene
contrastar las cifras con un asesor antes de presentar la declaración.
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

        # Cola FIFO de lotes de compra pendientes de vender. Cada lote guarda su
        # precio y la comisión de compra prorrateada por unidad, para incluirla
        # en el valor de adquisición.
        lotes = []
        for m in movimientos:
            if m.tipo == 'COMPRA':
                comision_unit = (m.comision / m.cantidad) if m.cantidad else Decimal('0')
                lotes.append({
                    'cantidad': m.cantidad,
                    'precio': m.precio_unitario,
                    'comision_unit': comision_unit,
                })
                continue

            # VENTA: consume los lotes más antiguos primero (FIFO).
            unidades_restantes = m.cantidad
            coste = Decimal('0')
            while unidades_restantes > 0 and lotes:
                lote = lotes[0]
                consumidas = min(unidades_restantes, lote['cantidad'])
                coste += consumidas * lote['precio'] + consumidas * lote['comision_unit']
                lote['cantidad'] -= consumidas
                unidades_restantes -= consumidas
                if lote['cantidad'] <= 0:
                    lotes.pop(0)

            coste = round(coste, 2)
            importe_venta = round(m.precio_unitario * m.cantidad, 2)
            # Valor de transmisión = importe − comisión de venta.
            ganancia = round(importe_venta - m.comision - coste, 2)

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
