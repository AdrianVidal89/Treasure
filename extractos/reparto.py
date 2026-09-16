"""Repartir un cobro en varias partidas, y volver a hacerlo solo.

En Norauto se pagan de una vez los neumáticos y la revisión: un apunte del
banco, dos partidas del presupuesto. Repartirlo a mano una vez está bien; a la
quinta, no. Este módulo es lo que permite decir «los recibos de Norauto van
así» y que la aplicación lo haga: a los que ya están importados y a los que
vengan.

Un reparto aprendido se guarda en PROPORCIONES, no en importes. Los importes no
se repiten —la revisión de este año no cuesta lo que la del anterior— pero la
forma del recibo sí: dos tercios de neumáticos y un tercio de revisión. Es una
suposición, y por eso el reparto automático se ve en la lista con sus partes y
se deshace de un clic; lo que nunca cambia es el total, que sigue siendo el del
banco.
"""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

CENTIMO = Decimal('0.01')

# Una parte que no dice a qué activo va hereda el del cobro. Hace falta un
# centinela porque `None` ya significa otra cosa —«a ninguno»— y confundirlos
# borraba el gasto de la ficha del coche al repartirlo.
HEREDAR = object()


def proporciones(importes):
    """Cada importe como fracción del total, en el mismo orden.

    Se calcula sobre la SUMA, no sobre el movimiento: es la misma cifra —las
    partes tienen que cuadrar con el cobro— y así la función sirve también para
    una lista suelta, sin tener que traer el movimiento.
    """
    total = sum(importes, Decimal('0'))
    if not total:
        return []
    return [Decimal(i) / total for i in importes]


def repartir(total, fracciones):
    """Aplica unas proporciones a un importe. Los trozos suman EXACTAMENTE el
    total.

    El último absorbe el resto del redondeo. Sin eso, tres tercios de 100 €
    dan 33,33 tres veces —99,99— y el reparto se rechaza por no cuadrar, que es
    justo lo que tiene que seguir pasando cuando el descuadre es de verdad.
    """
    if not fracciones:
        return []
    total = Decimal(total)
    trozos = [
        (total * Decimal(f)).quantize(CENTIMO, rounding=ROUND_HALF_UP)
        for f in fracciones[:-1]
    ]
    trozos.append(total - sum(trozos, Decimal('0')))
    return trozos


def partes_de_regla(regla, total):
    """Las partes que le tocan a un cobro de `total` según una división
    aprendida.

    Devuelve la misma forma que manda el formulario de dividir, para que la
    creación de partes sea un único camino y no dos que se van separando.
    """
    de_la_regla = list(regla.partes.all())
    importes = repartir(total, [p.proporcion for p in de_la_regla])
    return [
        {
            'importe': importe,
            'categoria': parte.categoria,
            'concepto': parte.concepto,
            # Una parte de la regla sin activo propio hereda el del cobro, que
            # es lo que pasa también cuando se reparte a mano sin tocar ese
            # desplegable.
            'activo': parte.activo_imputado or HEREDAR,
        }
        for parte, importe in zip(de_la_regla, importes)
    ]


def crear_partes(movimiento, partes):
    """Sustituye las partes de un movimiento por las dadas. Devuelve cuántas.

    Es el ÚNICO sitio donde nacen las partes: lo usan el reparto a mano, el que
    se aplica a los recibos parecidos y el de la importación. Cuando había un
    camino por cada uno, cada arreglo (heredar el activo, no duplicar el saldo)
    había que hacerlo tres veces y siempre quedaba uno sin hacer.

    Dividir de nuevo reemplaza la división anterior: si no, cada intento dejaría
    partes viejas sumando por detrás.
    """
    from finanzas import costes_activo

    from .models import MovimientoBancario

    movimiento.partes.all().delete()
    for i, p in enumerate(partes, start=1):
        parte = MovimientoBancario(
            extracto=movimiento.extracto, hogar_id=movimiento.hogar_id,
            dividido_de=movimiento, orden_parte=i,
            fecha=movimiento.fecha,
            concepto=(p.get('concepto') or f'{movimiento.concepto} ({i})')[:300],
            concepto_raw=movimiento.concepto_raw or movimiento.concepto,
            importe=p['importe'],
            # El saldo es del apunte del banco, no de cada trozo: repetirlo en
            # las partes haría creer que hubo varios movimientos.
            saldo=None,
            categoria=p.get('categoria'),
            estado_categorizacion='manual' if p.get('categoria') else 'sin_categorizar',
            es_traspaso=movimiento.es_traspaso,
        )
        # Sin activo propio se hereda el del cobro: el padre deja de contar al
        # dividirse, así que no heredarlo borraba el gasto de la ficha del coche
        # o de la casa.
        activo = p.get('activo', HEREDAR)
        if activo is HEREDAR:
            parte.propiedad_id = movimiento.propiedad_id
            parte.vehiculo_id = movimiento.vehiculo_id
        else:
            costes_activo.asignar(parte, activo)
        parte.save()
    return len(partes)


def aplicar_regla(regla, movimiento):
    """Divide un movimiento según una regla. Devuelve True si lo ha dividido.

    No toca lo que ya está dividido —a mano o antes— porque una regla nunca
    debe pisar un reparto que alguien revisó. Y no divide un movimiento a cero,
    que no tiene nada que repartir.
    """
    if not movimiento.importe or movimiento.partes.exists():
        return False
    partes = partes_de_regla(regla, movimiento.importe)
    if not es_division_valida(partes):
        return False
    crear_partes(movimiento, partes)
    return True


def version_vigente(reglas, fecha):
    """De varias versiones del mismo patrón, la que regía en esa fecha.

    Gana la más reciente que ya hubiera empezado. Si todas empiezan después
    —el reparto se corrigió «de enero en adelante» y el recibo es de octubre
    del año pasado—, no rige ninguna y el recibo se queda como estaba, que es
    justo lo que se pidió al fecharla.
    """
    candidatas = [r for r in reglas if r.rige_en(fecha)]
    if not candidatas:
        return None
    # `date.min` para que la versión «de siempre» quede la última de la cola:
    # cualquier versión con fecha le gana a partir de su día.
    return max(candidatas, key=lambda r: r.desde or date.min)


def coincide(regla, partes):
    """¿Esta versión ya produce exactamente este reparto?

    Es lo que decide si hay que ofrecer actualizarla. Antes bastaba con que
    EXISTIERA una regla para el comercio, así que corregir un reparto ya
    aprendido no preguntaba nada: ni se propagaba a los demás recibos ni se
    actualizaba la regla, y la corrección se quedaba en ese único apunte.

    Las proporciones se comparan con holgura de una diezmilésima: vienen de
    dividir importes y compararlas al céntimo exacto daría falsos distintos.
    """
    de_la_regla = list(regla.partes.all())
    if len(de_la_regla) != len(partes):
        return False

    fracciones = proporciones([p['importe'] for p in partes])
    for guardada, nueva, fraccion in zip(de_la_regla, partes, fracciones):
        if guardada.categoria_id != (nueva.get('categoria').id if nueva.get('categoria') else None):
            return False
        activo = nueva.get('activo', HEREDAR)
        activo = None if activo is HEREDAR else activo
        if guardada.activo_imputado != activo:
            return False
        if abs(guardada.proporcion - fraccion) > Decimal('0.0001'):
            return False
    return True


def es_division_valida(partes):
    """Una división necesita al menos dos partes y ninguna a cero.

    Una sola parte no es una división, y una parte de cero euros es una fila que
    alguien se dejó a medias: crearla dejaría un movimiento fantasma en la lista
    que no suma nada y confunde al leerla. En un cobro tan pequeño que al
    repartirlo alguna parte se va a cero, lo correcto es no repartirlo.
    """
    return len(partes) >= 2 and all(p['importe'] != 0 for p in partes)


def guardar_regla(hogar, patron, partes, desde=None, origen='manual'):
    """Crea o reemplaza la versión de la división aprendida para un patrón.

    `desde` es la fecha a partir de la cual vale. En blanco es la versión de
    siempre. Guardar una versión NO borra las otras: precisamente el caso es que
    el recibo del seguro llevaba tres coberturas hasta enero y cuatro después, y
    cada recibo tiene que seguir usando la que estaba vigente el día que se pagó.

    Las partes de una versión se reemplazan enteras en vez de intentar casarlas
    una a una: un reparto nuevo puede tener otro número de partidas, y media
    regla vieja mezclada con media nueva no es lo que pidió nadie.
    """
    from finanzas import costes_activo

    from .models import ParteDeDivision, ReglaDivision

    regla, _ = ReglaDivision.objects.update_or_create(
        hogar=hogar, patron=patron, desde=desde,
        defaults={'origen': origen, 'activo': True},
    )
    regla.partes.all().delete()
    fracciones = proporciones([p['importe'] for p in partes])
    for i, (p, fraccion) in enumerate(zip(partes, fracciones), start=1):
        parte = ParteDeDivision.objects.create(
            regla=regla, orden=i, proporcion=fraccion,
            categoria=p.get('categoria'), concepto=(p.get('concepto') or '')[:200],
        )
        activo = p.get('activo', HEREDAR)
        if activo is not HEREDAR:
            costes_activo.asignar(parte, activo)
            parte.save(update_fields=['vehiculo', 'propiedad'])
    return regla
