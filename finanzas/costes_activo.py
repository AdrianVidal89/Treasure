"""Lo que cuesta mantener un activo: lo declarado frente a lo que pasó por el banco.

Vale igual para un vehículo y para una propiedad porque la pregunta es la
misma —«¿cuánto me cuesta tener esto?»— y la respuesta se construye igual:

* TEÓRICO: lo que el hogar ha presupuestado para ese activo (las PartidaGasto
  imputadas), prorrateado a mes y a año. Es la previsión.
* REAL: lo que de verdad ha salido de la cuenta (los MovimientoBancario
  imputados). Es el hecho.

Comparar ambos es lo que convierte una lista de gastos en una respuesta: el
coche «cuesta 120 €/mes» solo si de verdad se van 120 €/mes.

Y para que la comparación signifique algo, los dos lados se miden IGUAL, en
DEVENGO: lo declarado ya viene prorrateado (unos neumáticos de 470 € que duran
tres años son 13 €/mes), así que lo real tiene que venir prorrateado también.
Poner el pago entero de los neumáticos contra el presupuesto de un año decía
que el coche se había pasado un 117% el mes en que tocó cambiarlos, cuando lo
que había pasado es que se pagó de golpe algo que cubre tres años.

De ahí que haya dos cifras de «real» y que no sean la misma:

* `real_anual` es CAJA: lo que salió del banco en el año. Es lo que uno ve en
  el extracto y con lo que se calcula el neto de un piso alquilado.
* `devengado_anual` es DEVENGO: la parte de esos pagos que le toca al año, ya
  repartida entre los meses que cada gasto cubre. Es la que se compara con lo
  declarado, y la que se enseña como «lo que cuesta al mes».

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

    # Lo que el año lleva costando. Todo se imputa AL AÑO y la cifra mensual es
    # ese año entre doce, que es como se hace la cuenta a mano:
    #
    #     (543/3 + 385 + 28,46 + 238 + 54) / 12 = 73,87 €/mes
    #
    # Un pago corriente se imputa entero —ya ha pasado, es de este año— y uno
    # periódico imputa solo la parte del año que le toca: unos neumáticos de
    # 543 € que duran tres años son 181 € al año, no 543.
    meses_transcurridos = _meses_transcurridos(anio)
    provisiones_devengadas = sum(
        (_cuota_anual(m) for m in provisiones if m.partida_conciliada_id),
        Decimal('0'),
    )
    devengado_anual = round(corriente_anual + provisiones_devengadas, 2)

    # La serie del año: lo que pasó por el banco cada mes, con sus movimientos.
    por_mes = _por_mes(del_anio, meses_transcurridos)

    # Y el mes, SIEMPRE entre doce. Repartir el gasto corriente entre los meses
    # transcurridos daba una cifra que no cuadraba con ninguna otra de la
    # tarjeta: 82,85 €/mes cuando la cuenta a mano da 73,87. Lo que va de año lo
    # dice la marca de la barra, no el divisor.
    ritmo_corriente = round(corriente_anual / 12, 2)
    ritmo_provisiones = round(provisiones_devengadas / 12, 2)
    ritmo_mensual = round(devengado_anual / 12, 2)

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
        'devengado_anual': devengado_anual,
        'provisiones_devengadas': round(provisiones_devengadas, 2),
        # La barra mide lo imputado al año contra el presupuesto del año, con la
        # marca de lo que va de año: así un 84% en septiembre se lee contra el
        # 75% que tocaría, en vez de contra un 117% que solo decía que ese mes
        # tocaba pagar los neumáticos de los próximos tres años.
        'diferencia_anual': devengado_anual - teorico_anual,
        'pct_ejecucion': _pct(devengado_anual, teorico_anual),
        'pct_transcurrido': _pct_transcurrido(anio),
        'por_mes': por_mes,
        'grafico_mensual': _grafico_mensual(por_mes, teorico_mensual),
        'por_categoria': _por_categoria(partidas, corrientes, provisiones),
        'movimientos': sorted(del_anio, key=lambda m: m.fecha, reverse=True),
        'anios_con_datos': sorted({m.fecha.year for m in movimientos}, reverse=True),
    }


def _cuota_anual(movimiento):
    """Lo que un pago periódico le imputa al año en el que se pagó.

    Un pago cubre `meses_periodo` meses. Si cubre MÁS de doce, al año solo le
    toca su parte: unos neumáticos de 543 € que duran tres años son 181 € al
    año. Si cubre doce o menos, se imputa entero, porque los meses que cubre
    caen todos dentro del año —y los demás pagos del mismo gasto llegarán
    también dentro de él—: un recibo trimestral de 100 € son 100 € este año, y
    los cuatro del año suman los 400 que declaraste. Sin el `max`, ese recibo
    se multiplicaba por cuatro y luego otra vez por los cuatro pagos.
    """
    meses = movimiento.partida_conciliada.meses_periodo
    return -movimiento.importe * 12 / Decimal(max(meses, 12))


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


# Cuántos movimientos de un mes caben en la tarjeta del gráfico. Más que esto
# no se lee de un vistazo, la tarjeta se sale del lienzo y tapa el eje; el resto
# se ve en la lista de movimientos de abajo.
MAXIMO_EN_LA_TARJETA = 5


def _por_mes(movimientos, meses_transcurridos):
    """Lo que pasó por el banco cada mes del año, mes a mes.

    Antes esto eran doce barras con los pagos anuales dibujados aparte para que
    no aplastasen al resto. Contaba dos cosas a la vez y no se leía ninguna:
    para saber en qué mes se fue el dinero hay que ver el dinero, incluido el
    mes en que tocó pagar la revisión. Con una línea y una escala que se ajusta,
    el pico de septiembre es justo lo que hay que ver.

    Cada mes lleva sus movimientos para la tarjeta del gráfico: la pregunta
    inmediata al ver un pico es «¿y eso qué fue?», y mandarte a otra pantalla
    para responderla te hace perder el sitio.
    """
    del_mes = defaultdict(list)
    for m in movimientos:
        del_mes[m.fecha.month].append(m)

    filas = []
    for n in range(1, 13):
        apuntes = sorted(del_mes[n], key=lambda m: (m.fecha, -abs(m.importe)))
        total = sum((-m.importe for m in apuntes), Decimal('0'))
        provision = sum(
            (-m.importe for m in apuntes if m.es_pago_provision), Decimal('0'),
        )
        filas.append({
            'mes': n,
            'etiqueta': MESES_ES[n],
            'total': total,
            'provision': provision,
            'corriente': total - provision,
            'num': len(apuntes),
            # Un mes que aún no ha llegado no es un mes de cero euros: la línea
            # se corta, o el año en curso se desploma a cero en el futuro.
            'futuro': n > meses_transcurridos,
            'movimientos': [
                {
                    'fecha': m.fecha.strftime('%d/%m'),
                    'concepto': m.concepto[:60],
                    'categoria': m.categoria.nombre if m.categoria else 'Sin categorizar',
                    'importe': float(-m.importe),
                    'provision': m.es_pago_provision,
                }
                for m in apuntes[:MAXIMO_EN_LA_TARJETA]
            ],
            'ocultos': max(0, len(apuntes) - MAXIMO_EN_LA_TARJETA),
        })
    return filas


# Geometría del gráfico. Se calcula aquí y no en el navegador para que la línea
# exista aunque el JS no llegue: lo que añade el JS es la tarjeta al pasar el
# ratón, no el dibujo.
#
# Las marcas (línea y área) van en un SVG estirado al ancho que haya, con el
# trazo sin escalar. El texto y los puntos van en HTML encima, colocados por
# porcentaje: así se leen igual de nítidos en la ficha del coche, que es ancha,
# y en la tarjeta de una propiedad, que es la mitad de estrecha. Metidos en el
# SVG, o se deformaban con él o había que elegir un tamaño de letra que solo
# valía para uno de los dos sitios.
# El canal del eje Y no vive aquí sino en el CSS, como un margen en píxeles:
# reservarlo en unidades del viewBox daba un canal del 5% del ancho, que son 60
# píxeles en la ficha del coche y 22 en la tarjeta de una propiedad —donde el
# «1.500 €» se montaba encima del primer punto—.
ANCHO, ALTO = 1000, 200
MARGEN = {'izq': 6, 'der': 6, 'arriba': 16, 'abajo': 10}


def _escala(maximo, divisiones=3):
    """Un tope redondo por encima del máximo, y sus marcas.

    Sin redondear, el eje decía «1.249,34» y «624,67»: cifras que nadie lee en
    un eje. Se sube al 1, 2, 2,5 o 5 más cercano de la potencia que toque.
    """
    if maximo <= 0:
        return Decimal('100'), [Decimal(0), Decimal(50), Decimal(100)]
    paso_crudo = Decimal(maximo) / divisiones
    potencia = Decimal(10) ** (len(str(int(paso_crudo))) - 1)
    for multiplo in ('1', '2', '2.5', '5', '10'):
        paso = (potencia * Decimal(multiplo)).quantize(Decimal('0.01'))
        if paso * divisiones >= Decimal(maximo):
            break
    tope = paso * divisiones
    return tope, [paso * i for i in range(divisiones + 1)]


def _grafico_mensual(filas, teorico_mensual):
    """La línea del año lista para pintar: puntos, área, marcas y eje.

    La previsión mensual va de línea de referencia, no de segunda serie: es el
    listón contra el que se lee la línea, y ponerla como serie con su propia
    escala sería inventarse una comparación que no está en los datos.
    """
    hasta = [f for f in filas if not f['futuro']]
    maximo = max([f['total'] for f in hasta] + [teorico_mensual, Decimal('0')])
    tope, marcas = _escala(maximo)

    base = ALTO - MARGEN['abajo']
    alto_util = base - MARGEN['arriba']
    ancho_util = ANCHO - MARGEN['izq'] - MARGEN['der']

    def x_de(mes):
        return round(MARGEN['izq'] + (mes - 1) * ancho_util / 11, 2)

    def y_de(valor):
        if tope <= 0:
            return base
        crudo = base - float(Decimal(valor) / tope) * alto_util
        # Un mes con más devoluciones que gastos daría un total negativo y la
        # línea se saldría por debajo del marco.
        return round(min(max(crudo, MARGEN['arriba']), base), 2)

    def pct(valor, total):
        return round(valor / total * 100, 3)

    for f in filas:
        f['x'] = x_de(f['mes'])
        f['y'] = y_de(f['total']) if not f['futuro'] else None
        f['pct_x'] = pct(f['x'], ANCHO)
        f['pct_y'] = pct(f['y'], ALTO) if f['y'] is not None else None

    # La zona sensible de cada mes llega hasta media distancia con sus vecinos,
    # no solo al punto: se apunta a un mes, no a un círculo de ocho píxeles.
    medio = ancho_util / 11 / 2
    for f in filas:
        izq = max(f['x'] - medio, 0)
        der = min(f['x'] + medio, ANCHO)
        f['zona_izq'] = pct(izq, ANCHO)
        f['zona_ancho'] = pct(der - izq, ANCHO)

    puntos = [(f['x'], f['y']) for f in hasta]
    linea = ' '.join(f'{x},{y}' for x, y in puntos)
    area = ''
    if len(puntos) >= 2:
        area = (
            f'M {puntos[0][0]},{base} '
            + ' '.join(f'L {x},{y}' for x, y in puntos)
            + f' L {puntos[-1][0]},{base} Z'
        )

    con_datos = [f for f in hasta if f['num']]
    return {
        'ancho': ANCHO, 'alto': ALTO, 'base': base,
        'x_min': MARGEN['izq'], 'x_max': ANCHO - MARGEN['der'],
        'linea': linea,
        'area': area,
        'hay_linea': len(puntos) >= 2,
        'hay_datos': bool(con_datos),
        'meses': filas,
        'y_teorico': y_de(teorico_mensual) if teorico_mensual else None,
        'pct_teorico': pct(y_de(teorico_mensual), ALTO) if teorico_mensual else None,
        'teorico': teorico_mensual,
        'marcas': [
            {'valor': v, 'y': y_de(v), 'pct_y': pct(y_de(v), ALTO)} for v in marcas
        ],
        'tope': tope,
        # Lo mismo, en tipos que viajan a JSON: es lo que lee la tarjeta al
        # pasar el ratón. Va aparte de `meses` porque ahí hay Decimales, que
        # `json_script` no sabe serializar.
        'datos': [
            {
                'mes': f['mes'], 'etiqueta': f['etiqueta'],
                'total': float(f['total']),
                'corriente': float(f['corriente']),
                'provision': float(f['provision']),
                'num': f['num'], 'futuro': f['futuro'],
                'movimientos': f['movimientos'], 'ocultos': f['ocultos'],
            }
            for f in filas
        ],
        # El mes con más gasto se etiqueta directamente: es el que responde a
        # «¿en qué mes se me fue?» sin tener que pasar el ratón por encima. Se
        # etiqueta ese y ninguno más: un número en cada punto es ruido.
        'pico': max(con_datos, key=lambda f: f['total'], default=None),
    }


def _por_categoria(partidas, corrientes, provisiones):
    """Declarado y real por categoría, los dos AL AÑO: dónde se desvía el coste.

    Mantenimiento vehicular salía «929 € de 593 €, +336 €» el mes en que se
    pagaron unos neumáticos que duran tres años: el pago entero contra el
    presupuesto de un año. Y el intento siguiente, prorratear lo declarado a los
    meses transcurridos, daba «425 € de 444 €»: dos cifras que no son ninguna de
    las que uno puede comprobar a mano.

    Las dos columnas son ahora del AÑO, sin prorrateos de por medio. Lo
    declarado es lo que suman las partidas de la categoría en doce meses
    (592,56 € en mantenimiento, 468 € en el seguro) y lo real es lo que los
    pagos de este año le imputan, con los plurianuales repartidos entre los años
    que cubren (543 € de neumáticos a tres años → 181 €).

    `pagado_anual` se conserva aparte: es lo que salió del banco, y sigue siendo
    la respuesta a «¿cuánto he pagado ya de esto?».
    """
    filas = {}

    def _fila(categoria):
        nombre = categoria.nombre if categoria else 'Sin categorizar'
        return filas.setdefault(nombre, {
            'categoria': nombre,
            'tipo': categoria.tipo if categoria else 'sin',
            'declarado_anual': Decimal('0'),
            'real_anual': Decimal('0'),
            'pagado_anual': Decimal('0'),
        })

    for p in partidas:
        _fila(p.categoria)['declarado_anual'] += p.importe_anual

    for m in corrientes:
        fila = _fila(m.categoria)
        fila['real_anual'] += -m.importe
        fila['pagado_anual'] += -m.importe

    for m in provisiones:
        fila = _fila(m.categoria)
        fila['real_anual'] += _cuota_anual(m)
        fila['pagado_anual'] += -m.importe

    orden = sorted(filas.values(), key=lambda f: f['real_anual'], reverse=True)
    for f in orden:
        f['real_anual'] = f['real_anual'].quantize(Decimal('0.01'))
        f['diferencia'] = f['real_anual'] - f['declarado_anual']
        # Lo que pagaste por adelantado y cubre años que aún no han llegado.
        f['diferido'] = f['pagado_anual'] - f['real_anual']
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
        'devengado_anual': sum((f['devengado_anual'] for f in fichas), Decimal('0')),
        'ritmo_mensual': sum((f['ritmo_mensual'] for f in fichas), Decimal('0')),
        'meses_transcurridos': _meses_transcurridos(anio),
        'provisiones_anual': sum((f['provisiones_anual'] for f in fichas), Decimal('0')),
        'partidas_sueltas': sorted(
            {p for f in fichas for p in f['partidas_sueltas']}, key=lambda p: p.nombre,
        ),
        'ingreso_real_anual': sum((f['ingreso_real_anual'] for f in fichas), Decimal('0')),
        'neto_real_anual': sum((f['neto_real_anual'] for f in fichas), Decimal('0')),
    }
