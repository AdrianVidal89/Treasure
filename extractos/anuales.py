"""Los fijos anuales de un año: lo que se declaró frente a lo que se pagó.

El IBI, el seguro del coche, la revisión: se declaran una vez, se provisionan
todo el año y se pagan de golpe en un mes concreto. La pregunta que hay que
poder contestar de un vistazo es la de cada recibo —«¿lo he pagado ya?»— y la
del año —«¿cuánto me falta por pagar y cuándo me toca?»—. Las demás pantallas
lo miran mes a mes y no la contestan.

QUÉ PAGO ES DE QUÉ PARTIDA, por orden:

1. El que se marcó en Movimientos con «pago anual» de esa partida. Es lo que
   el usuario ha dicho, y manda.
2. Si no se marcó, el de una categoría que solo tiene UNA partida anual: un
   cargo en «Seguro coche» no puede ser de otra cosa. Se enseña como deducido.
3. El resto de pagos en categorías de fijos anuales quedan «sin asignar», a la
   vista, para marcarlos: adivinar entre dos seguros sería mentir.

El importe de cada pago es lo que te costó a ti (`importe_neto`): si un gasto
compartido te lo devolvieron en parte, es tu parte la que consume la partida.
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal

from finanzas.models import MESES_CHOICES, PartidaGasto

from .models import MovimientoBancario

MESES = dict(MESES_CHOICES)
# Por debajo de esto una diferencia es redondeo, no un pago que falta: el
# recibo del seguro rara vez es exactamente lo que se declaró.
TOLERANCIA = Decimal('0.01')


def _cuotas(partida):
    """En qué meses del año se espera pagar, y cuánto cada vez.

    Vacío si no se ha dicho el mes de pago: sin él no hay «previsto en junio»
    que enseñar, solo el total del año. Los que duran más de un año —unos
    neumáticos cada tres— no tienen cuota «este año»: no se sabe si toca.
    """
    n = partida.meses_periodo
    if not partida.mes_pago or n > 12:
        return []
    veces = max(12 // n, 1)
    meses = sorted(((partida.mes_pago - 1 + k * n) % 12) + 1 for k in range(veces))
    return [(mes, partida.importe) for mes in meses]


def _pagos_del_anio(hogar, anio):
    return [
        m for m in MovimientoBancario.objects.filter(hogar=hogar, fecha__year=anio)
        .select_related('categoria', 'partida_conciliada', 'dividido_de')
        .prefetch_related('partes', 'reembolsos', 'dividido_de__partes',
                          'dividido_de__reembolsos')
        .order_by('fecha', 'id')
        if m.cuenta_como_gasto and not m.es_neutro
    ]


def _estado(fila, anio, hoy):
    """«Pagado», «Te falta», «No se ha pagado»… y la frase que lo explica."""
    pagado, esperado = fila['pagado'], fila['esperado']

    if fila['plurianual']:
        if pagado > 0:
            return 'pagado', 'Pagado este año'
        return 'no_toca', f"{fila['periodicidad']} · sin pago este año"

    if esperado <= 0:
        return ('pagado', 'Pagado') if pagado > 0 else ('pendiente', 'Sin importe declarado')
    if pagado >= esperado * (1 - TOLERANCIA):
        if pagado > esperado * (1 + TOLERANCIA):
            return 'pagado', f"Pagado · {pagado - esperado:.2f} € más de lo declarado".replace('.', ',')
        return 'pagado', 'Pagado'

    # Lo que ya debería estar pagado a estas alturas del año.
    if anio < hoy.year:
        vencido = esperado
    elif anio > hoy.year:
        vencido = Decimal('0')
    else:
        vencido = sum((c['importe'] for c in fila['cuotas'] if c['mes'] < hoy.month), Decimal('0'))
    falta = esperado - pagado

    if vencido and pagado < vencido * (1 - TOLERANCIA):
        tocaba = [c['nombre_mes'] for c in fila['cuotas'] if anio < hoy.year or c['mes'] < hoy.month]
        cuando = f" · tocaba en {tocaba[-1].lower()}" if tocaba else ''
        if anio < hoy.year and not fila['cuotas']:
            cuando = ''
        return 'atrasado', ('No se pagó' if pagado == 0 else 'Falta por pagar') + cuando

    siguiente = next(
        (c for c in fila['cuotas'] if anio > hoy.year or c['mes'] >= hoy.month), None,
    )
    if siguiente:
        if anio == hoy.year and siguiente['mes'] == hoy.month:
            cuando = 'toca este mes'
        else:
            cuando = f"toca en {siguiente['nombre_mes'].lower()}"
    else:
        cuando = 'sin mes de pago declarado'
    if pagado > 0:
        return 'parcial', f"Pagado en parte · {cuando}"
    return 'pendiente', f"Pendiente · {cuando}"


def analizar_anuales(hogar, anio, hoy=None):
    hoy = hoy or date.today()
    partidas = [
        p for p in PartidaGasto.objects.filter(hogar=hogar, activo=True)
        .select_related('categoria').order_by('mes_pago', 'nombre')
        if p.tipo_bloque == 'anual'
    ]
    ids = {p.id for p in partidas}
    por_categoria = defaultdict(list)
    for p in partidas:
        if p.categoria_id:
            por_categoria[p.categoria_id].append(p)

    asignados = defaultdict(list)
    sin_asignar = []
    for m in _pagos_del_anio(hogar, anio):
        if m.partida_conciliada_id in ids:
            asignados[m.partida_conciliada_id].append((m, False))
        elif m.partida_conciliada_id:
            continue  # es de otra partida (una no anual): no es de aquí
        elif m.categoria_id and len(por_categoria.get(m.categoria_id, [])) == 1:
            asignados[por_categoria[m.categoria_id][0].id].append((m, True))
        elif m.categoria and m.categoria.tipo == 'anual':
            sin_asignar.append(m)

    filas = []
    previsto_mes = defaultdict(list)
    pagado_mes = defaultdict(list)
    for p in partidas:
        plurianual = p.meses_periodo > 12
        cuotas = [
            {'mes': mes, 'nombre_mes': MESES[mes], 'importe': importe}
            for mes, importe in _cuotas(p)
        ]
        pagos = [
            {'mov': m, 'importe': -m.importe_neto, 'deducido': deducido,
             'fecha': m.fecha, 'mes': m.fecha.month}
            for m, deducido in asignados.get(p.id, [])
        ]
        pagado = sum((x['importe'] for x in pagos), Decimal('0'))
        esperado = p.importe if plurianual else p.importe_anual
        fila = {
            'partida': p,
            'nombre': p.nombre,
            'categoria': p.categoria.nombre if p.categoria else '',
            'periodicidad': p.get_periodicidad_display(),
            'meses_periodo': p.meses_periodo,
            'plurianual': plurianual,
            'cuotas': cuotas,
            'sin_mes': not p.mes_pago,
            'esperado': esperado,
            'provision_anual': p.importe_anual,
            'pagado': pagado,
            'falta': max(esperado - pagado, Decimal('0')) if not plurianual else Decimal('0'),
            'pagos': pagos,
            'hay_deducidos': any(x['deducido'] for x in pagos),
        }
        # Pagado en otro mes del previsto: se dice, porque es justo lo que la
        # gráfica enseña y la lista tiene que contar lo mismo.
        meses_cuota = {c['mes'] for c in cuotas}
        fila['fuera_de_mes'] = bool(cuotas) and any(x['mes'] not in meses_cuota for x in pagos)
        fila['estado'], fila['frase'] = _estado(fila, anio, hoy)
        if fila['estado'] == 'pagado':
            # Pagado dentro del redondeo: los céntimos que no llegan no son una
            # deuda, y sumarlos a «falta por pagar» la haría mentir.
            fila['falta'] = Decimal('0')
        fila['pct'] = (
            min(float(pagado / esperado * 100), 100) if esperado > 0 else (100 if pagado else 0)
        )
        filas.append(fila)

        for c in cuotas:
            previsto_mes[c['mes']].append({'nombre': p.nombre, 'importe': float(c['importe'])})
        for x in pagos:
            pagado_mes[x['mes']].append({
                'nombre': p.nombre, 'importe': float(x['importe']),
                'fecha': x['fecha'].strftime('%d/%m'),
            })
    for m in sin_asignar:
        pagado_mes[m.fecha.month].append({
            'nombre': f'{m.concepto} (sin asignar)', 'importe': float(-m.importe_neto),
            'fecha': m.fecha.strftime('%d/%m'),
        })

    orden = {'atrasado': 0, 'parcial': 1, 'pendiente': 2, 'pagado': 3, 'no_toca': 4}
    filas.sort(key=lambda f: (
        orden[f['estado']],
        f['cuotas'][0]['mes'] if f['cuotas'] else 13,
        f['nombre'],
    ))

    grafico = [
        {
            'mes': n, 'nombre': MESES[n],
            'previsto': previsto_mes.get(n, []),
            'pagado': pagado_mes.get(n, []),
            'total_previsto': sum(x['importe'] for x in previsto_mes.get(n, [])),
            'total_pagado': sum(x['importe'] for x in pagado_mes.get(n, [])),
        }
        for n in range(1, 13)
    ]

    anuales = [f for f in filas if not f['plurianual']]
    previsto = sum((f['esperado'] for f in anuales), Decimal('0'))
    pagado_partidas = sum((f['pagado'] for f in filas), Decimal('0'))
    total_sin_asignar = sum((-m.importe_neto for m in sin_asignar), Decimal('0'))
    falta = sum((f['falta'] for f in anuales), Decimal('0'))
    anios = sorted(
        {d.year for d in MovimientoBancario.objects.filter(hogar=hogar).dates('fecha', 'year')}
        | {hoy.year},
        reverse=True,
    )
    return {
        'anio': anio,
        'anios': anios,
        'es_actual': anio == hoy.year,
        'mes_hoy': hoy.month if anio == hoy.year else None,
        'filas': filas,
        'num_partidas': len(filas),
        'num_pagadas': sum(1 for f in filas if f['estado'] == 'pagado'),
        'num_atrasadas': sum(1 for f in filas if f['estado'] == 'atrasado'),
        'num_sin_mes': sum(1 for f in filas if f['sin_mes'] and not f['plurianual']),
        'previsto': previsto,
        'pagado': pagado_partidas,
        'falta': falta,
        'pct_pagado': min(float(pagado_partidas / previsto * 100), 100) if previsto > 0 else 0,
        'sin_asignar': sin_asignar,
        'total_sin_asignar': total_sin_asignar,
        'grafico': grafico,
    }
