"""¿Llega la reserva para los fijos anuales?

La reserva es la hucha de los gastos que no son de cada mes: el IBI, los
seguros, la ITV, unos neumáticos cada tres años. Cada mes se le aparta la
provisión de todas esas partidas, y cuando toca un recibo sale de ahí. La
pregunta que las demás pantallas no contestan es si, con lo que hay HOY y lo que
se va a ir apartando, se llega a cada pago, o en qué mes se queda en negativo.

Todo parte de lo que el usuario dice que hay hoy (`SaldoReserva`), no de los
movimientos: la hucha puede vivir en una cuenta que no se importa, y un saldo
reajustado a mano es justo lo que absorbe un recibo que vino más caro.

Las reglas, de una vez:

  · APORTE: cada mes, la suma de las provisiones de las partidas de fijos
    anuales (importe ÷ meses del periodo: 600 € cada 3 años son 16,67 €/mes).
    Empieza el mes siguiente al del saldo: el de este mes, hecho o no, ya está
    —o no— dentro de lo que has dicho que hay.
  · PAGOS: los recibos que tocan, en su mes y por su importe declarado.
    · Del año del saldo se quitan los ya pagados hasta la fecha del saldo
      (ese dinero ya no está en lo que has dicho que hay) y los que diste por
      pagados. Lo que tocaba antes y no se ha pagado sale ya, en el primer mes:
      si en realidad está pagado, se marca en Fijos anuales y desaparece.
    · De los años siguientes, todo lo que toque, salvo lo dado por pagado.
  · Lo que no tiene fecha (anual sin mes, plurianual sin cuándo toca) no se
    puede poner en ningún mes: se avisa, porque su aporte sí está sumado.

Con la línea del saldo mes a mes, lo que importa es su punto más bajo: si es
negativo, eso es exactamente lo que hay que meter HOY para no quedarse corto
nunca (sube toda la línea por igual). La alternativa es subir el aporte
mensual lo justo para que el peor mes llegue a cero.
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_UP

from .anuales import (
    MESES, asignar_pagos, chequeo_fechas, cuotas_de, leer_horizonte, partidas_anuales,
)
from .models import SaldoReserva

CERO = Decimal('0')
CORTOS = ['', 'ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sep', 'oct', 'nov', 'dic']


def _pendientes_del_anio(partida, anio, mes_inicio, pagado_antes, hoy):
    """Cuotas de `anio` que aún tienen que salir de la reserva.

    Lo pagado antes del saldo cubre las cuotas por orden. Lo que quedó sin
    cubrir de un mes anterior al del saldo sale en `mes_inicio`, marcado como
    atrasado."""
    restante = pagado_antes
    salida = []
    for mes, importe in cuotas_de(partida, anio, hoy=hoy):
        cubierto = min(restante, importe)
        restante -= cubierto
        falta = importe - cubierto
        if falta <= Decimal('0.01'):
            continue
        if mes < mes_inicio:
            salida.append((mes_inicio, falta, True))
        else:
            salida.append((mes, falta, False))
    return salida


def prever_reserva(hogar, anios=1, hoy=None):
    hoy = hoy or date.today()
    anios = leer_horizonte(anios)
    ultimo = SaldoReserva.objects.filter(hogar=hogar).first()
    saldo_inicial = ultimo.saldo if ultimo else CERO
    inicio = ultimo.fecha if ultimo else hoy
    anio_fin = hoy.year + anios - 1
    if anio_fin < inicio.year:
        anio_fin = inicio.year

    partidas = partidas_anuales(hogar)
    # Lo fijo de cada mes, y aparte los puntuales, que solo aportan los meses
    # que se aparta para ellos (de `ahorro_desde` al mes del pago).
    aporte = sum((p.importe_mensual for p in partidas if not p.es_puntual), CERO)
    puntuales = [p for p in partidas if p.es_puntual]

    def aporte_puntuales(anio, mes, es_inicio):
        total = CERO
        for p in puntuales:
            if not p.ahorra_en(anio, mes):
                continue
            # En el mes del saldo no se suma lo fijo —ya está, o no, dentro de
            # lo que has dicho que hay—; un puntual creado DESPUÉS de apuntar
            # el saldo no puede estar dentro, así que su cuota de ese mes sí.
            if es_inicio and ultimo and p.fecha_creacion and ultimo.creado_en >= p.fecha_creacion:
                continue
            total += p.importe_mensual
        return total

    # Pagos por mes absoluto (año·12 + mes-1).
    pagos = defaultdict(list)
    for anio in range(inicio.year, anio_fin + 1):
        asignados = asignar_pagos(hogar, anio, partidas)[0] if anio == inicio.year else {}
        for p in partidas:
            if anio in {int(a) for a in p.anios_dados_por_pagados or []}:
                continue
            if anio == inicio.year:
                pagado_antes = sum(
                    (-m.importe_neto for m, _ in asignados.get(p.id, []) if m.fecha <= inicio),
                    CERO,
                )
                filas = _pendientes_del_anio(p, anio, inicio.month, pagado_antes, hoy)
            else:
                filas = [(mes, imp, False) for mes, imp in cuotas_de(p, anio, hoy=hoy)]
            for mes, importe, atrasado in filas:
                pagos[anio * 12 + mes - 1].append({
                    'nombre': p.nombre, 'importe': importe, 'atrasado': atrasado,
                    'partida_id': p.id,
                })

    desde = inicio.year * 12 + inicio.month - 1
    hasta = anio_fin * 12 + 11
    puntos = []
    saldo = saldo_inicial
    aportes_hechos = 0
    total_aportes = CERO
    total_pagos = CERO
    for t in range(desde, hasta + 1):
        anio, mes = t // 12, t % 12 + 1
        aporte_mes = (aporte if t > desde else CERO) + aporte_puntuales(anio, mes, t == desde)
        if t > desde:
            aportes_hechos += 1
        total_aportes += aporte_mes
        del_mes = sorted(pagos.get(t, []), key=lambda x: -x['importe'])
        salida = sum((x['importe'] for x in del_mes), CERO)
        total_pagos += salida
        saldo = saldo + aporte_mes - salida
        puntos.append({
            'anio': anio, 'mes': mes,
            'etiqueta': f'{CORTOS[mes]} {str(anio)[2:]}',
            'nombre': f'{MESES[mes]} {anio}',
            'aporte': aporte_mes,
            'pagos': del_mes,
            'total_pagos': salida,
            'saldo': saldo,
            'aportes_hechos': aportes_hechos,
        })

    peor = min(puntos, key=lambda x: x['saldo'])
    falta_hoy = max(-peor['saldo'], CERO)
    # Cuánto más al mes para que el peor mes llegue a cero. Un hueco en el
    # mismo mes del saldo, antes de cualquier aporte, solo se tapa hoy.
    extra_mensual = None
    if falta_hoy > 0:
        necesidades = []
        for x in puntos:
            if x['saldo'] < 0:
                if not x['aportes_hechos']:
                    necesidades = None
                    break
                necesidades.append(-x['saldo'] / x['aportes_hechos'])
        if necesidades:
            extra_mensual = max(necesidades).quantize(Decimal('0.01'), rounding=ROUND_UP)

    proximos = [x for x in puntos if x['pagos']]
    return {
        'saldo': ultimo,
        'saldo_inicial': saldo_inicial,
        'inicio': inicio,
        'anios': anios,
        'anio_fin': anio_fin,
        'aporte': aporte,
        'aporte_hoy': aporte + sum((p.importe_mensual for p in puntuales if p.ahorra_en(hoy.year, hoy.month)), CERO),
        'puntuales': [
            {'nombre': p.nombre, 'importe': p.importe, 'mensual': p.importe_mensual,
             'cuando': p.meses_pago_display, 'activo': p.ahorra_en(hoy.year, hoy.month)}
            for p in puntuales if p.anio_pago and (p.anio_pago, p.mes_pago) >= (hoy.year, hoy.month)
        ],
        'aportes_partidas': sorted(
            ({'nombre': p.nombre, 'mensual': p.importe_mensual,
              'periodicidad': (f'puntual, hasta {p.meses_pago_display.lower()}' if p.es_puntual
                               else p.get_periodicidad_display())}
             for p in partidas if not p.es_puntual or p.ahorra_en(hoy.year, hoy.month)),
            key=lambda x: -x['mensual'],
        ),
        'puntos': puntos,
        'peor': peor,
        'cubierto': falta_hoy == 0,
        'falta_hoy': falta_hoy.quantize(Decimal('0.01'), rounding=ROUND_UP) if falta_hoy else CERO,
        'extra_mensual': extra_mensual,
        'saldo_final': puntos[-1]['saldo'],
        'total_pagos': total_pagos,
        'total_aportes': total_aportes,
        'proximos': proximos,
        'hay_atrasados': any(x['atrasado'] for p in proximos for x in p['pagos']),
        'fechas': chequeo_fechas(partidas),
        'historial': list(SaldoReserva.objects.filter(hogar=hogar)[:6]),
        'grafico': {
            'puntos': [
                {
                    'etiqueta': x['etiqueta'], 'nombre': x['nombre'],
                    'saldo': float(x['saldo']), 'aporte': float(x['aporte']),
                    'pagos': [{'nombre': y['nombre'], 'importe': float(y['importe']),
                               'atrasado': y['atrasado']} for y in x['pagos']],
                }
                for x in puntos
            ],
            'inicial': float(saldo_inicial),
        },
    }
