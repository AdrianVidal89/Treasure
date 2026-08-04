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
from collections import defaultdict
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


def _clave_valor(inv):
    """Clave de 'valor homogéneo' para Hacienda: mismo contribuyente + mismo
    valor, CON INDEPENDENCIA de la cartera/plataforma. Hacienda trata todas las
    participaciones del mismo valor de una persona como un único pool FIFO,
    estén donde estén. Se agrupa por ticker; sin ticker, por nombre normalizado
    (aproximación)."""
    ticker = (inv.ticker or '').strip().upper()
    if ticker:
        return (inv.usuario_id, 'TICKER:' + ticker)
    return (inv.usuario_id, 'NOMBRE:' + (inv.nombre or '').strip().upper())


def _origen(inv):
    """Etiqueta corta de la cartera/broker de donde procede un lote, para poder
    rastrear en el desglose de qué cartera salió cada compra."""
    return inv.plataforma or inv.nombre or ''


def calcular_informe_ventas(hogar, anio):
    """Devuelve un dict con las ventas del año y los totales/estimación fiscal.

    Los DEPÓSITOS se excluyen del pool FIFO de plusvalías: fiscalmente son
    rendimientos del capital mobiliario, no ganancias patrimoniales. Se calculan
    aparte en `calcular_informe_depositos` y se suman a la base del ahorro."""
    inversiones = (
        Inversion.objects
        .filter(usuario__userprofile__hogar=hogar)
        .exclude(tipo='DEPOSITO')
        .select_related('usuario')
        .prefetch_related('movimientos')
    )

    # Agrupa TODOS los movimientos por (contribuyente, valor) a través de todas
    # las carteras: un único pool FIFO por valor homogéneo, como exige Hacienda.
    grupos = defaultdict(list)  # clave -> [(movimiento, inversion), ...]
    for inv in inversiones:
        clave = _clave_valor(inv)
        for m in inv.movimientos.all():
            if m.tipo in ('COMPRA', 'VENTA'):
                grupos[clave].append((m, inv))

    ventas = []
    for movs in grupos.values():
        # Orden cronológico global del valor, mezclando todas sus carteras.
        movs.sort(key=lambda mi: (mi[0].fecha, mi[0].id))

        # Cola FIFO de lotes de compra pendientes de vender. Cada lote guarda su
        # fecha, precio, la comisión de compra prorrateada por unidad y de qué
        # cartera salió, para el valor de adquisición y el desglose.
        lotes = []
        for m, inv in movs:
            if m.tipo == 'COMPRA':
                comision_unit = (m.comision / m.cantidad) if m.cantidad else Decimal('0')
                lotes.append({
                    'fecha': m.fecha,
                    'cantidad': m.cantidad,
                    'precio': m.precio_unitario,
                    'comision_unit': comision_unit,
                    'origen': _origen(inv),
                })
                continue

            # VENTA: consume los lotes más antiguos primero (FIFO) —de cualquier
            # cartera— y guarda de qué compras concretas sale el coste.
            unidades_restantes = m.cantidad
            lotes_consumidos = []
            while unidades_restantes > 0 and lotes:
                lote = lotes[0]
                consumidas = min(unidades_restantes, lote['cantidad'])
                coste_lote = round(consumidas * (lote['precio'] + lote['comision_unit']), 2)
                lotes_consumidos.append({
                    'fecha': lote['fecha'],
                    'cantidad': consumidas,
                    'precio': lote['precio'],
                    'coste': coste_lote,
                    'origen': lote['origen'],
                })
                lote['cantidad'] -= consumidas
                unidades_restantes -= consumidas
                if lote['cantidad'] <= 0:
                    lotes.pop(0)

            # El coste total es la suma de los lotes (así el desglose cuadra al céntimo).
            coste = sum((l['coste'] for l in lotes_consumidos), Decimal('0'))
            unidades_sin_coste = unidades_restantes  # >0 → faltan compras registradas
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
                    'lotes': lotes_consumidos,
                    'cubierto': unidades_sin_coste == 0,
                    'unidades_sin_coste': unidades_sin_coste,
                })

    ventas.sort(key=lambda v: v['fecha'])

    total_importe = sum((v['importe_venta'] for v in ventas), Decimal('0'))
    total_coste = sum((v['coste_adquisicion'] for v in ventas), Decimal('0'))
    total_comisiones = sum((v['comision'] for v in ventas), Decimal('0'))
    ganancias_positivas = sum((v['ganancia'] for v in ventas if v['ganancia'] > 0), Decimal('0'))
    perdidas = sum((v['ganancia'] for v in ventas if v['ganancia'] < 0), Decimal('0'))
    ganancia_neta = sum((v['ganancia'] for v in ventas), Decimal('0'))

    # ── Rendimientos de depósitos (capital mobiliario) del ejercicio ──────────
    depositos = calcular_informe_depositos(hogar, anio)
    interes_depositos = depositos['total_interes']

    # La base del ahorro combina plusvalías (parte positiva) y rendimientos.
    base_ahorro = max(ganancia_neta, Decimal('0')) + max(interes_depositos, Decimal('0'))
    impuesto_estimado = _impuesto_base_ahorro(base_ahorro)
    tipo_efectivo = (
        round(impuesto_estimado / base_ahorro * 100, 2)
        if base_ahorro > 0 else Decimal('0')
    )

    ventas_sin_cobertura = [v for v in ventas if not v['cubierto']]

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
        'depositos': depositos['depositos'],
        'interes_depositos': interes_depositos,
        'base_ahorro': base_ahorro,
        'impuesto_estimado': impuesto_estimado,
        'tipo_efectivo': tipo_efectivo,
        'tramos': TRAMOS_AHORRO_ES,
        'hay_sin_cobertura': bool(ventas_sin_cobertura),
        'ventas_sin_cobertura': len(ventas_sin_cobertura),
    }


def calcular_informe_depositos(hogar, anio):
    """Rendimientos (intereses) de los depósitos del hogar en el ejercicio.

    Para cada depósito calcula el interés DEVENGADO durante el año (variación
    de valor descontando aportaciones/retiradas). Es una estimación orientativa
    del rendimiento del capital mobiliario a declarar: la fiscalidad exacta
    depende de cuándo se paga/liquida el interés."""
    depositos = (
        Inversion.objects
        .filter(usuario__userprofile__hogar=hogar, tipo='DEPOSITO')
        .select_related('usuario')
        .prefetch_related('movimientos')
    )

    filas = []
    for inv in depositos:
        interes_anio = inv.deposito_interes_anio(anio)
        valor, aportado = inv.deposito_valor_y_aportado()
        interes_total = round(valor - aportado, 2)
        liquidado_en_anio = bool(
            inv.deposito_fecha_liquidacion and inv.deposito_fecha_liquidacion.year == anio
        )
        # Incluimos el depósito si generó interés ese año o se liquidó ese año.
        if interes_anio == 0 and not liquidado_en_anio:
            continue
        filas.append({
            'nombre': inv.nombre,
            'titular': _titular(inv.usuario),
            'apertura': inv.deposito_fecha_apertura,
            'tipo_interes': inv.deposito_tipo_interes,
            'frecuencia': inv.get_deposito_frecuencia_display(),
            'interes_anio': interes_anio,
            'interes_total': interes_total,
            'liquidado': inv.deposito_fecha_liquidacion,
            'liquidado_en_anio': liquidado_en_anio,
        })

    filas.sort(key=lambda f: (f['titular'], f['nombre']))
    total_interes = sum((f['interes_anio'] for f in filas), Decimal('0'))
    return {'anio': anio, 'depositos': filas, 'total_interes': total_interes}
