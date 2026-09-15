"""Lo que cuesta mantener un activo: lo declarado frente a lo que pasó por el banco.

Vale igual para un vehículo y para una propiedad porque la pregunta es la
misma —«¿cuánto me cuesta tener esto?»— y la respuesta se construye igual:

* TEÓRICO: lo que el hogar ha presupuestado para ese activo (las PartidaGasto
  imputadas), prorrateado a mes y a año. Es la previsión.
* REAL: lo que de verdad ha salido de la cuenta (los MovimientoBancario
  imputados). Es el hecho.

Comparar ambos es lo que convierte una lista de gastos en una respuesta: el
coche «cuesta 120 €/mes» solo si de verdad se van 120 €/mes.

La clave del activo (`vehiculo:3`, `propiedad:1`) es lo que permite que haya un
único selector en toda la interfaz en vez de uno por tipo.
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal

MESES_ES = [
    '', 'Ene', 'Feb', 'Mar', 'Abr', 'May', 'Jun',
    'Jul', 'Ago', 'Sep', 'Oct', 'Nov', 'Dic',
]

# Nombre del campo que apunta al activo, según su tipo.
CAMPO_POR_TIPO = {'vehiculo': 'vehiculo', 'propiedad': 'propiedad'}


def clave(activo):
    """«vehiculo:3» / «propiedad:1» para un objeto activo."""
    from .models import Vehiculo

    tipo = 'vehiculo' if isinstance(activo, Vehiculo) else 'propiedad'
    return f'{tipo}:{activo.pk}'


def resolver(hogar, clave_texto):
    """El activo del hogar que corresponde a una clave, o None.

    Devuelve None ante cualquier cosa rara (clave inventada, activo de otro
    hogar): la imputación es un dato del usuario, no una ruta de confianza.
    """
    from .models import Propiedad, Vehiculo

    if not clave_texto or ':' not in clave_texto:
        return None
    tipo, _, ident = clave_texto.partition(':')
    if not ident.isdigit():
        return None
    modelo = {'vehiculo': Vehiculo, 'propiedad': Propiedad}.get(tipo)
    if modelo is None:
        return None
    return modelo.objects.filter(hogar=hogar, pk=int(ident)).first()


def opciones(hogar):
    """Los activos del hogar agrupados para un selector, en un solo sitio para
    que el desplegable sea idéntico en gastos y en extractos."""
    from .models import Propiedad, Vehiculo

    return [
        {
            'etiqueta': 'Vehículos',
            'opciones': [
                {'clave': v.clave_activo, 'nombre': v.nombre}
                for v in Vehiculo.objects.filter(hogar=hogar, activo=True)
            ],
        },
        {
            'etiqueta': 'Propiedades',
            'opciones': [
                {'clave': p.clave_activo, 'nombre': p.nombre}
                for p in Propiedad.objects.filter(hogar=hogar, activo=True)
            ],
        },
    ]


def asignar(obj, activo):
    """Imputa el objeto al activo dado (o lo desimputa con None).

    Se limpian SIEMPRE los dos campos: sin esto, reasignar del coche a la casa
    dejaría el gasto contado en los dos sitios."""
    from .models import Vehiculo

    obj.vehiculo = activo if isinstance(activo, Vehiculo) else None
    obj.propiedad = activo if (activo is not None and not isinstance(activo, Vehiculo)) else None
    return obj


def _partidas(activo):
    from .models import PartidaGasto

    campo = 'vehiculo' if clave(activo).startswith('vehiculo') else 'propiedad'
    return (
        PartidaGasto.objects.filter(activo=True, **{campo: activo})
        .select_related('categoria')
    )


def _movimientos(activo):
    from extractos.models import MovimientoBancario

    campo = 'vehiculo' if clave(activo).startswith('vehiculo') else 'propiedad'
    return (
        MovimientoBancario.objects.filter(**{campo: activo})
        # `partida_conciliada` porque el prorrateo de cada pago necesita saber
        # cuántos meses cubre su gasto, y `partes` porque un movimiento dividido
        # deja de contar por sí mismo.
        .select_related('categoria', 'partida_conciliada')
        .prefetch_related('partes')
    )


def _fuentes(activo):
    """Ingresos declarados que pertenecen al activo (el alquiler de ese piso)."""
    from .models import FuenteIngreso

    campo = 'vehiculo' if clave(activo).startswith('vehiculo') else 'propiedad'
    return FuenteIngreso.objects.filter(activo=True, **{campo: activo})


def costes(activo, anio):
    """Balance del activo en un año: lo que cuesta y lo que deja.

    Un piso alquilado no es solo gasto: si el alquiler está imputado a él, la
    pregunta deja de ser «cuánto me cuesta» y pasa a ser «cuánto me renta», que
    es la que de verdad importa. Por eso el mismo cálculo devuelve las dos
    patas y su neto.

    `pct_ejecucion` es la barra que se va llenando: cuánto del presupuesto anual
    llevas gastado. Puede pasar de 100 (y entonces interesa verlo).
    """
    from .distribucion import _neto_fuente_base

    partidas = list(_partidas(activo))
    todos = list(_movimientos(activo))
    movimientos = [m for m in todos if m.cuenta_como_gasto]
    del_anio = [m for m in movimientos if m.fecha.year == anio]

    # --- Lo que deja ---
    fuentes = list(_fuentes(activo))
    ingreso_mensual = sum((_neto_fuente_base(f)[0] for f in fuentes), Decimal('0'))
    ingresos_reales = [
        m for m in todos if m.cuenta_como_ingreso and m.fecha.year == anio
    ]
    ingreso_real_anual = sum((m.importe for m in ingresos_reales), Decimal('0'))

    teorico_mensual = sum((p.importe_mensual for p in partidas), Decimal('0'))
    teorico_anual = sum((p.importe_anual for p in partidas), Decimal('0'))
    real_anual = sum((-m.importe for m in del_anio), Decimal('0'))

    # Los pagos de gastos que se provisionan todo el año y se pagan de golpe
    # —la revisión del coche, el seguro— van aparte del gasto corriente. Contra
    # el AÑO se comparan igual que todo lo demás, pero meterlos en la media
    # mensual dice que el coche cuesta mil doscientos euros al mes porque en
    # septiembre pasó por el taller.
    provisiones = [m for m in del_anio if m.es_pago_provision]
    corrientes = [m for m in del_anio if not m.es_pago_provision]
    provisiones_anual = sum((-m.importe for m in provisiones), Decimal('0'))
    corriente_anual = sum((-m.importe for m in corrientes), Decimal('0'))

    # Meses del año con algún gasto CORRIENTE del activo: la media se calcula
    # sobre ellos, no sobre doce, o un activo estrenado en noviembre parecería
    # baratísimo.
    meses_con_datos = {m.fecha.month for m in corrientes}
    real_mensual = (
        corriente_anual / len(meses_con_datos) if meses_con_datos else Decimal('0')
    )

    # Un pago imputado al activo cuya PARTIDA no lo está deja la comparación
    # coja: el gasto suma en lo real y su provisión no suma en lo teórico, así
    # que el activo parece pasarse cuando lo que falta es imputar la partida.
    imputadas = {p.id for p in partidas}
    partidas_sueltas = sorted(
        {
            m.partida_conciliada for m in provisiones
            if m.partida_conciliada_id and m.partida_conciliada_id not in imputadas
        },
        key=lambda p: p.nombre,
    )

    # Lo que de verdad está costando al mes, para poder ponerlo al lado del
    # presupuesto mensual. Antes la ficha enseñaba «85,27 €/mes» de teórico
    # junto a «1.249,34 €» de real, que es el total del año: parecía que el
    # coche costaba mil doscientos al mes cuando eso era lo de nueve meses.
    #
    # El gasto corriente se reparte entre los meses que van de año. Los pagos de
    # un gasto periódico, NO: se reparten entre los meses que ese gasto cubre.
    # Unos neumáticos de 470 € que duran tres años cuestan 13 €/mes, no 470
    # entre los meses que lleve el año; meterlos en el mismo saco decía que el
    # coche se había puesto carísimo el mes en que tocó cambiarlos.
    meses_transcurridos = _meses_transcurridos(anio)
    ritmo_corriente = (
        corriente_anual / meses_transcurridos if meses_transcurridos else Decimal('0')
    )
    ritmo_provisiones = sum(
        (
            -m.importe / Decimal(m.partida_conciliada.meses_periodo)
            for m in provisiones if m.partida_conciliada_id
        ),
        Decimal('0'),
    )
    ritmo_mensual = round(ritmo_corriente + ritmo_provisiones, 2)

    return {
        'activo': activo,
        'clave': clave(activo),
        'anio': anio,
        'partidas': partidas,

        'fuentes': fuentes,
        'ingreso_mensual': ingreso_mensual,
        'ingreso_anual': ingreso_mensual * 12,
        'ingreso_real_anual': ingreso_real_anual,
        'movimientos_ingreso': sorted(ingresos_reales, key=lambda m: m.fecha, reverse=True),
        # El neto es la cifra que decide si el activo suma o resta. Se calcula
        # con lo REAL de los dos lados: comparar lo declarado de uno con lo
        # real del otro daría un número que no es de nadie.
        'neto_real_anual': ingreso_real_anual - real_anual,
        'neto_declarado_anual': ingreso_mensual * 12 - teorico_anual,
        'renta': bool(fuentes) or bool(ingresos_reales),
        'num_partidas': len(partidas),
        'num_movimientos': len(del_anio),
        'teorico_mensual': teorico_mensual,
        'teorico_anual': teorico_anual,
        'real_anual': real_anual,
        'real_mensual': real_mensual,
        'ritmo_mensual': ritmo_mensual,
        'ritmo_corriente': round(ritmo_corriente, 2),
        'ritmo_provisiones': round(ritmo_provisiones, 2),
        'meses_transcurridos': meses_transcurridos,
        'diferencia_mensual': ritmo_mensual - teorico_mensual,
        'corriente_anual': corriente_anual,
        'provisiones_anual': provisiones_anual,
        'num_provisiones': len(provisiones),
        'movimientos_provision': sorted(provisiones, key=lambda m: m.fecha, reverse=True),
        'partidas_sueltas': partidas_sueltas,
        'meses_con_datos': len(meses_con_datos),
        'diferencia_anual': real_anual - teorico_anual,
        'pct_ejecucion': _pct(real_anual, teorico_anual),
        'pct_transcurrido': _pct_transcurrido(anio),
        'por_mes': _por_mes(corrientes, teorico_mensual, provisiones),
        'por_categoria': _por_categoria(partidas, del_anio),
        'movimientos': sorted(del_anio, key=lambda m: m.fecha, reverse=True),
        'anios_con_datos': sorted({m.fecha.year for m in movimientos}, reverse=True),
    }


def _meses_transcurridos(anio):
    """Meses del año que ya han pasado. Un año cerrado son doce."""
    hoy = date.today()
    if anio < hoy.year:
        return 12
    if anio > hoy.year:
        return 0
    return hoy.month


def _pct_transcurrido(anio):
    """Qué parte del año va consumida.

    Sin esto, un 76% del presupuesto no dice nada: en diciembre es ir sobrado y
    en marzo es ir camino de duplicarlo."""
    hoy = date.today()
    if anio < hoy.year:
        return 100
    if anio > hoy.year:
        return 0
    return int(hoy.month / 12 * 100)


def _pct(real, teorico):
    if teorico <= 0:
        return None
    return int(min(real / teorico * 100, 999))


def _por_mes(movimientos, teorico_mensual, provisiones=None):
    """Serie de doce meses: lo real de cada uno contra la previsión mensual.

    Los pagos de gastos anuales se dibujan aparte: sumados al mes en el que
    caen convertían septiembre en un rascacielos al lado del que ningún otro
    mes se distingue, cuando lo que pasó es que tocaba pagar la revisión.
    """
    totales = defaultdict(lambda: Decimal('0'))
    for m in movimientos:
        totales[m.fecha.month] += -m.importe

    de_provision = defaultdict(lambda: Decimal('0'))
    for m in (provisiones or []):
        de_provision[m.fecha.month] += -m.importe

    tope = max(list(totales.values()) + [teorico_mensual, Decimal('0')])
    return [
        {
            'mes': n,
            'etiqueta': MESES_ES[n],
            'real': totales[n],
            'provision': de_provision[n],
            'pct': float(totales[n] / tope * 100) if tope else 0,
            'pct_teorico': float(teorico_mensual / tope * 100) if tope else 0,
        }
        for n in range(1, 13)
    ]


def _por_categoria(partidas, movimientos):
    """Declarado y real por categoría: dónde se desvía el coste del activo."""
    filas = {}

    def _fila(categoria):
        nombre = categoria.nombre if categoria else 'Sin categorizar'
        return filas.setdefault(nombre, {
            'categoria': nombre,
            'tipo': categoria.tipo if categoria else 'sin',
            'declarado_anual': Decimal('0'),
            'real_anual': Decimal('0'),
        })

    for p in partidas:
        _fila(p.categoria)['declarado_anual'] += p.importe_anual
    for m in movimientos:
        _fila(m.categoria)['real_anual'] += -m.importe

    orden = sorted(filas.values(), key=lambda f: f['real_anual'], reverse=True)
    for f in orden:
        f['diferencia'] = f['real_anual'] - f['declarado_anual']
        f['pct'] = _pct(f['real_anual'], f['declarado_anual'])
    return orden


def resumen(activos, anio):
    """Los totales de una lista de activos, para la pantalla de listado."""
    fichas = [costes(a, anio) for a in activos]
    return {
        'fichas': fichas,
        'teorico_mensual': sum((f['teorico_mensual'] for f in fichas), Decimal('0')),
        'teorico_anual': sum((f['teorico_anual'] for f in fichas), Decimal('0')),
        'real_anual': sum((f['real_anual'] for f in fichas), Decimal('0')),
        'ritmo_mensual': sum((f['ritmo_mensual'] for f in fichas), Decimal('0')),
        'meses_transcurridos': _meses_transcurridos(anio),
        'provisiones_anual': sum((f['provisiones_anual'] for f in fichas), Decimal('0')),
        'partidas_sueltas': sorted(
            {p for f in fichas for p in f['partidas_sueltas']}, key=lambda p: p.nombre,
        ),
        'ingreso_real_anual': sum((f['ingreso_real_anual'] for f in fichas), Decimal('0')),
        'neto_real_anual': sum((f['neto_real_anual'] for f in fichas), Decimal('0')),
    }
