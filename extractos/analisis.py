"""Análisis del gasto de un mes: en qué se ha ido y qué lo explica.

La pregunta que resuelve este módulo no es «cuánto me he gastado» —eso ya lo
dice la conciliación— sino **qué ha cambiado respecto a un mes normal**. De ahí
las tres piezas:

* el PUENTE: cuánto aporta cada categoría a la desviación contra la media de
  los meses anteriores, ordenado por peso. Comparar contra la propia media es
  más honesto que contra el presupuesto, que puede estar mal puesto.
* los COMERCIOS: dentro de una categoría, ocho pedidos de 24 € y una cena de
  190 € suman parecido y son dos problemas distintos. Uno se corta cambiando
  un hábito; el otro no se corta, se prevé.
* RECURRENTE vs PUNTUAL: qué parte del mes volverá a pasar el mes que viene
  (el suelo de gasto) y qué parte fue excepcional.

Todo se calcula sobre el gasto en valor absoluto (`cuenta_como_gasto`), que es
lo que se está analizando; ingresos y movimientos neutros quedan fuera.
"""

from collections import defaultdict
from decimal import Decimal

# Meses hacia atrás que forman la referencia. Seis es suficiente para que un mes
# raro no arrastre la media y poco para que siga describiendo el presente.
MESES_REFERENCIA = 6

# Un comercio es RECURRENTE si aparece en esta proporción de los meses de
# referencia. Con 6 meses, 0.6 significa «al menos 4 de los 6».
UMBRAL_RECURRENTE = 0.6

# Mínimo de meses con datos para que la comparación signifique algo.
MINIMO_MESES_REFERENCIA = 2

# Por debajo de este importe un gasto es «hormiga»: individualmente
# despreciable, en bloque no.
UMBRAL_HORMIGA = Decimal('20')


def _clave_mes(fecha):
    return (fecha.year, fecha.month)


def _meses_previos(clave, cuantos):
    """Las `cuantos` claves (año, mes) inmediatamente anteriores a `clave`."""
    anio, mes = clave
    previos = []
    for _ in range(cuantos):
        mes -= 1
        if mes == 0:
            anio, mes = anio - 1, 12
        previos.append((anio, mes))
    return previos


def _etiqueta_comercio(movimientos):
    """El concepto más repetido del grupo: es el que el usuario reconoce, no la
    clave normalizada con la que se agrupa."""
    conteo = defaultdict(int)
    for m in movimientos:
        conteo[m.concepto] += 1
    return max(conteo.items(), key=lambda kv: kv[1])[0]


def analizar_mes(movimientos, anio, mes, bloque=None, categoria_id=None,
                 etiqueta_id=None, meses_referencia=MESES_REFERENCIA):
    """Analiza el gasto de un mes frente a los meses anteriores.

    `movimientos`: todos los del hogar (con categoría precargada). El filtrado
    por periodo se hace aquí porque la referencia necesita los meses previos,
    que por definición quedan fuera del filtro de la pantalla.

    `bloque` / `categoria_id` / `etiqueta_id` acotan el análisis a una parte del
    gasto, que es lo que hace útil el drill-down: mismo cálculo, menos ámbito.
    """
    objetivo = (anio, mes)
    referencia = set(_meses_previos(objetivo, meses_referencia))

    def en_ambito(m):
        if not m.cuenta_como_gasto:
            return False
        if bloque and (m.categoria.tipo if m.categoria else 'sin') != bloque:
            return False
        if categoria_id and m.categoria_id != categoria_id:
            return False
        if etiqueta_id and etiqueta_id not in {e.id for e in m.etiquetas.all()}:
            return False
        return True

    del_ambito = [m for m in movimientos if en_ambito(m)]
    del_mes = [m for m in del_ambito if _clave_mes(m.fecha) == objetivo]
    previos = [m for m in del_ambito if _clave_mes(m.fecha) in referencia]

    # Solo cuentan como referencia los meses en los que hubo ALGÚN movimiento
    # del hogar: dividir entre seis cuando solo se han importado dos hunde la
    # media y hace que todo parezca un exceso.
    meses_con_datos = {
        _clave_mes(m.fecha) for m in movimientos if _clave_mes(m.fecha) in referencia
    }
    num_referencia = len(meses_con_datos)
    hay_referencia = num_referencia >= MINIMO_MESES_REFERENCIA

    total = _suma(del_mes)
    media = (_suma(previos) / num_referencia) if num_referencia else Decimal('0')

    return {
        'anio': anio,
        'mes': mes,
        'total': total,
        'media': media,
        'desviacion': total - media,
        'num_movimientos': len(del_mes),
        'hay_referencia': hay_referencia,
        'meses_referencia': num_referencia,
        'puente': _puente(del_mes, previos, num_referencia) if hay_referencia else [],
        'categorias': _por_categoria(del_mes, previos, num_referencia),
        'comercios': _por_comercio(del_mes, previos, meses_con_datos),
        'etiquetas': _por_etiqueta(del_mes),
        **_recurrencia(del_mes, previos, meses_con_datos),
        **_hormiga(del_mes),
    }


def _suma(movimientos):
    """Gasto en positivo. Un abono dentro de una categoría de gasto (una
    devolución) resta, que es justo lo que hace en la realidad."""
    return sum((-m.importe for m in movimientos), Decimal('0'))


def _puente(del_mes, previos, num_referencia):
    """Qué categoría explica la desviación del mes, de mayor a menor.

    Incluye las que bajan: saber que la luz ha ido 30 € a favor es parte de
    entender por qué el mes cuadra o no."""
    actual = defaultdict(lambda: Decimal('0'))
    for m in del_mes:
        actual[_nombre_categoria(m)] += -m.importe

    historico = defaultdict(lambda: Decimal('0'))
    for m in previos:
        historico[_nombre_categoria(m)] += -m.importe

    filas = []
    for nombre in set(actual) | set(historico):
        media = historico[nombre] / num_referencia if num_referencia else Decimal('0')
        desviacion = actual[nombre] - media
        if desviacion == 0:
            continue
        filas.append({
            'categoria': nombre,
            'actual': actual[nombre],
            'media': media,
            'desviacion': desviacion,
        })
    filas.sort(key=lambda f: abs(f['desviacion']), reverse=True)

    # El ancho de cada barra es relativo a la desviación más grande, para que la
    # comparación entre ellas se vea sin leer las cifras.
    tope = max((abs(f['desviacion']) for f in filas), default=Decimal('0'))
    for f in filas:
        f['pct'] = float(abs(f['desviacion']) / tope * 100) if tope else 0
    return filas


def _nombre_categoria(movimiento):
    return movimiento.categoria.nombre if movimiento.categoria else 'Sin categorizar'


def _por_categoria(del_mes, previos, num_referencia):
    """Desglose del mes por categoría, con su media y su desviación."""
    grupos = defaultdict(list)
    for m in del_mes:
        grupos[m.categoria_id].append(m)

    historico = defaultdict(lambda: Decimal('0'))
    for m in previos:
        historico[m.categoria_id] += -m.importe

    filas = []
    for categoria_id, movs in grupos.items():
        categoria = movs[0].categoria
        total = _suma(movs)
        media = historico[categoria_id] / num_referencia if num_referencia else Decimal('0')
        filas.append({
            'id': categoria_id,
            'nombre': categoria.nombre if categoria else 'Sin categorizar',
            'tipo': categoria.tipo if categoria else 'sin',
            'total': total,
            'media': media,
            'desviacion': total - media,
            'num': len(movs),
        })
    filas.sort(key=lambda f: f['total'], reverse=True)

    tope = max((f['total'] for f in filas), default=Decimal('0'))
    for f in filas:
        f['pct'] = float(f['total'] / tope * 100) if tope else 0
    return filas


def _por_comercio(del_mes, previos, meses_con_datos):
    """Ranking de comercios del mes: cuánto, cuántas veces y de cuánto cada vez.

    El ticket medio es la mitad del diagnóstico: ocho pedidos de 24 € y una cena
    de 190 € pesan parecido en el total y no son el mismo problema."""
    grupos = defaultdict(list)
    for m in del_mes:
        grupos[m.comercio or 'otros'].append(m)

    # Meses previos en los que se vio cada comercio, para saber si es habitual.
    presencia = defaultdict(set)
    for m in previos:
        presencia[m.comercio or 'otros'].add(_clave_mes(m.fecha))

    filas = []
    for comercio, movs in grupos.items():
        total = _suma(movs)
        meses_visto = len(presencia[comercio])
        filas.append({
            'comercio': comercio,
            'etiqueta': _etiqueta_comercio(movs),
            'categoria': _nombre_categoria(movs[0]),
            'num': len(movs),
            'total': total,
            'ticket_medio': total / len(movs),
            'meses_visto': meses_visto,
            'recurrente': _es_recurrente(meses_visto, meses_con_datos),
            'ids': [m.id for m in movs],
        })
    filas.sort(key=lambda f: f['total'], reverse=True)

    tope = max((f['total'] for f in filas), default=Decimal('0'))
    for f in filas:
        f['pct'] = float(f['total'] / tope * 100) if tope else 0
    return filas


def _es_recurrente(meses_visto, meses_con_datos):
    """Sin historia suficiente no se moja: marcar como «puntual» algo que solo
    lleva un mes importado sería mentir con seguridad."""
    if len(meses_con_datos) < MINIMO_MESES_REFERENCIA:
        return False
    return meses_visto >= UMBRAL_RECURRENTE * len(meses_con_datos)


def _recurrencia(del_mes, previos, meses_con_datos):
    """Cuánto del mes volverá a pasar (suelo de gasto) y cuánto fue excepcional."""
    presencia = defaultdict(set)
    for m in previos:
        presencia[m.comercio or 'otros'].add(_clave_mes(m.fecha))

    recurrente = Decimal('0')
    puntual = Decimal('0')
    for m in del_mes:
        visto = len(presencia[m.comercio or 'otros'])
        if _es_recurrente(visto, meses_con_datos):
            recurrente += -m.importe
        else:
            puntual += -m.importe
    return {'gasto_recurrente': recurrente, 'gasto_puntual': puntual}


def _hormiga(del_mes):
    """Los microgastos: cada uno es despreciable, juntos no."""
    pequenos = [m for m in del_mes if Decimal('0') < -m.importe < UMBRAL_HORMIGA]
    return {
        'hormiga_total': _suma(pequenos),
        'hormiga_num': len(pequenos),
        'hormiga_umbral': UMBRAL_HORMIGA,
    }


def _por_etiqueta(del_mes):
    """Gasto del mes por etiqueta: el corte transversal (un viaje, una obra) que
    las categorías no pueden dar porque reparten el mismo gasto en varias."""
    grupos = defaultdict(list)
    for m in del_mes:
        for etiqueta in m.etiquetas.all():
            grupos[etiqueta].append(m)

    filas = [
        {
            'id': etiqueta.id,
            'nombre': etiqueta.nombre,
            'color': etiqueta.color,
            'total': _suma(movs),
            'num': len(movs),
        }
        for etiqueta, movs in grupos.items()
    ]
    filas.sort(key=lambda f: f['total'], reverse=True)
    return filas
