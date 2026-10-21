"""Recordar a qué activo va un pagador: hacia atrás y hacia delante.

Al imputar un movimiento a un piso o a un coche se cambia ESE movimiento y se
pregunta lo demás por separado:

* hacia el pasado: los otros movimientos del mismo pagador ya importados;
* hacia el futuro: una `ReglaActivo` que imputa sola lo que se importe después.

«El mismo pagador» no es el comercio a secas: se quitan los meses y palabras
como «cuota» o «alquiler» (`clave_pagador`), y si el concepto lleva un número
largo que se repite en otros movimientos del pagador —el del préstamo— se usa
también, para no mezclar dos hipotecas del mismo banco.
"""

from finanzas import costes_activo

from .models import MovimientoBancario, ReglaActivo
from .normalizacion import clave_pagador, referencias


def _mismo_pagador(hogar, patron):
    """Los movimientos del hogar cuyo comercio da la misma clave de pagador."""
    comercios = (
        MovimientoBancario.objects.filter(hogar=hogar)
        .exclude(comercio='').values_list('comercio', flat=True).distinct()
    )
    iguales = [c for c in comercios if clave_pagador(c) == patron]
    return MovimientoBancario.objects.filter(hogar=hogar, comercio__in=iguales)


def criterio(hogar, mov):
    """(patron, referencia) con los que se reconoce al pagador de `mov`.

    La referencia solo cuenta si aparece también en otro movimiento del mismo
    pagador: un número que cambia cada mes (la referencia de una transferencia)
    no sirve para reconocer nada, y exigirlo haría que la regla no casara nunca.
    """
    patron = clave_pagador(mov.comercio)
    if not patron:
        return '', ''
    propias = referencias(mov.concepto)
    if not propias:
        return patron, ''
    vistas = set()
    for concepto in _mismo_pagador(hogar, patron).exclude(pk=mov.pk).values_list('concepto', flat=True):
        vistas |= referencias(concepto)
    comunes = sorted(propias & vistas)
    return patron, (comunes[0] if comunes else '')


def _encaja(concepto, referencia):
    return not referencia or referencia in referencias(concepto)


def similares(hogar, mov):
    """Los otros movimientos ya importados del mismo pagador."""
    patron, referencia = criterio(hogar, mov)
    if not patron:
        return []
    return [
        m for m in _mismo_pagador(hogar, patron).exclude(pk=mov.pk)
        if _encaja(m.concepto, referencia)
    ]


def _que_casa(reglas, concepto):
    """La regla que se aplicaría a un concepto: la que lleva referencia gana a
    la genérica, porque es más concreta."""
    for regla in sorted(reglas, key=lambda r: not r.referencia):
        if _encaja(concepto, regla.referencia):
            return regla
    return None


def regla_de(hogar, mov):
    """La regla que se aplicaría hoy a este movimiento, si la hay.

    No se busca por el criterio exacto: una regla guardada con la primera
    hipoteca no tiene número de préstamo (aún no se repetía) y sigue siendo la
    que imputa las siguientes."""
    patron = clave_pagador(mov.comercio)
    if not patron:
        return None
    return _que_casa(ReglaActivo.objects.filter(hogar=hogar, patron=patron), mov.concepto)


def recordar(hogar, mov, activo):
    """Guarda (o cambia) la regla para que lo que se importe después vaya a
    `activo`. Devuelve la regla, o None si el movimiento no tiene pagador
    reconocible."""
    patron, referencia = criterio(hogar, mov)
    if not patron:
        return None
    regla, _ = ReglaActivo.objects.get_or_create(hogar=hogar, patron=patron, referencia=referencia)
    costes_activo.asignar(regla, activo)
    regla.save()
    return regla


def olvidar(hogar, mov):
    """Borra la regla de este pagador. Devuelve cuántas se han borrado."""
    regla = regla_de(hogar, mov)
    if not regla:
        return 0
    regla.delete()
    return 1


def aplicar_reglas(hogar, extracto):
    """Imputa los movimientos recién importados que encajen en una regla.

    Solo toca los que no tienen activo: si el usuario ya ha elegido, manda él.
    Una regla con referencia gana a la que no la tiene, porque es más concreta.
    """
    por_patron = {}
    for regla in ReglaActivo.objects.filter(hogar=hogar).select_related('propiedad', 'vehiculo'):
        if regla.activo_imputado:
            por_patron.setdefault(regla.patron, []).append(regla)
    if not por_patron:
        return 0

    asignados = 0
    usadas = {}
    nuevos = extracto.movimientos.filter(
        propiedad__isnull=True, vehiculo__isnull=True, dividido_de__isnull=True,
    ).exclude(comercio='')
    for m in nuevos:
        regla = _que_casa(por_patron.get(clave_pagador(m.comercio), ()), m.concepto)
        if regla:
            costes_activo.asignar(m, regla.activo_imputado)
            m.save(update_fields=['propiedad', 'vehiculo'])
            usadas[regla.pk] = usadas.get(regla.pk, 0) + 1
            asignados += 1
    for regla in ReglaActivo.objects.filter(pk__in=usadas):
        regla.veces_aplicada += usadas[regla.pk]
        regla.save(update_fields=['veces_aplicada'])
    return asignados
