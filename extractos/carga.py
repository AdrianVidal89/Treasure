"""Cargar los movimientos de un hogar con sus relaciones, sin el coste del ORM.

Las pantallas de Extractos, el Dashboard y el cuadre necesitan el histórico
ENTERO —la media de los meses de antes, el ritmo del mes, lo pagado en anuales—
y para cada movimiento sus etiquetas, sus partes, lo que puso la reserva y lo
que te devolvieron. Con `prefetch_related` eso eran pocas consultas, pero Django
monta un QuerySet por movimiento y relación para guardar el resultado: con tres
años importados, once mil clones y dos segundos solo en eso, más que todo lo
demás junto.

Aquí se hace lo mismo a mano: una consulta para los hijos (partes, coberturas y
reembolsos son movimientos del mismo hogar) y otra para las etiquetas, y el
resultado se deja en la misma caché que usaría el prefetch. El modelo ya lee de
ahí (`_coberturas`, `_reembolsos`, `esta_dividido`) y las plantillas siguen
pudiendo hacer `m.etiquetas.all`.
"""

from collections import defaultdict

from django.db.models import Q

from .models import MovimientoBancario

RELACIONADOS = ('categoria', 'partida_conciliada')
# Las relaciones de un movimiento con OTRO movimiento. Con select_related cada
# fila traía cuatro movimientos de columnas (casi siempre vacías) y el coste de
# montarlos; el otro movimiento casi siempre está ya en la lista, así que se
# enlaza desde ahí y solo se piden los que falten.
A_OTRO_MOVIMIENTO = ('cubre', 'reembolsa', 'dividido_de')


class Precargada(list):
    """Lo que devuelve `m.partes.all()` cuando la relación viene precargada.

    Una lista con lo poco de QuerySet que se usa sobre una relación ya cargada.
    Cualquier otra cosa —un `.filter()`— falla en alto en vez de ir a la base
    de datos sin el filtro de la relación y devolver lo que no es."""

    def all(self):
        return self

    def count(self):
        return len(self)

    def exists(self):
        return bool(self)

    def first(self):
        return self[0] if self else None


def precargar(movimientos):
    """Rellena en `movimientos` (y en sus cobros padre) las relaciones que usa
    el panel. Devuelve la misma lista."""
    if not movimientos:
        return movimientos
    hogares = {m.hogar_id for m in movimientos}

    # Partes, coberturas y reembolsos son movimientos del mismo hogar que
    # apuntan a otro: una sola consulta los trae todos.
    partes, coberturas, reembolsos = defaultdict(list), defaultdict(list), defaultdict(list)
    hijos = MovimientoBancario.objects.filter(hogar_id__in=hogares).filter(
        Q(dividido_de__isnull=False) | Q(cubre__isnull=False) | Q(reembolsa__isnull=False),
    )
    for h in hijos:
        if h.dividido_de_id:
            partes[h.dividido_de_id].append(h)
        if h.cubre_id:
            coberturas[h.cubre_id].append(h)
        if h.reembolsa_id:
            reembolsos[h.reembolsa_id].append(h)

    etiquetas = defaultdict(list)
    Through = MovimientoBancario.etiquetas.through
    for fila in (Through.objects.filter(movimientobancario__hogar_id__in=hogares)
                 .select_related('etiqueta').order_by('etiqueta__nombre')):
        etiquetas[fila.movimientobancario_id].append(fila.etiqueta)

    def rellenar(m, con_etiquetas=True):
        cache = m.__dict__.setdefault('_prefetched_objects_cache', {})
        cache['partes'] = Precargada(partes.get(m.pk, ()))
        cache['coberturas'] = Precargada(coberturas.get(m.pk, ()))
        cache['reembolsos'] = Precargada(reembolsos.get(m.pk, ()))
        if con_etiquetas:
            cache['etiquetas'] = Precargada(etiquetas.get(m.pk, ()))

    por_id = {m.pk: m for m in movimientos}
    faltan = {
        getattr(m, f'{campo}_id') for m in movimientos for campo in A_OTRO_MOVIMIENTO
    } - set(por_id) - {None}
    fuera = (
        list(MovimientoBancario.objects.filter(pk__in=faltan).select_related(*RELACIONADOS))
        if faltan else []
    )
    for m in fuera:
        por_id[m.pk] = m
        # Un cobro padre que no está en la lista (el detalle de UN extracto)
        # también necesita sus partes y coberturas: de ahí saca cada parte su
        # porción de la reserva.
        rellenar(m, con_etiquetas=False)

    for m in movimientos:
        rellenar(m)
        for campo in A_OTRO_MOVIMIENTO:
            otro_id = getattr(m, f'{campo}_id')
            if otro_id is not None and otro_id in por_id:
                MovimientoBancario._meta.get_field(campo).set_cached_value(m, por_id[otro_id])
    return movimientos


def cargar(queryset):
    """La lista de movimientos del `queryset`, con todo lo que el panel lee."""
    return precargar(list(queryset.select_related(*RELACIONADOS)))
