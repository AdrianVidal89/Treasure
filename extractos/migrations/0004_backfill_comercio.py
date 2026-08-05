"""Rellena `comercio` y `concepto_raw` de los movimientos ya importados.

Sin esto, la pantalla «Sin categorizar» agruparía todo el histórico bajo la
clave vacía, porque `comercio` solo se calcula al guardar y los movimientos
antiguos nunca vuelven a pasar por `save()`.

De paso normaliza los patrones de las reglas existentes (creadas por el
asistente IA antes de que los patrones se guardaran ya normalizados),
descartando las que colisionarían con la restricción única (hogar, patron).
"""

from django.db import migrations

from extractos.normalizacion import normalizar_comercio, normalizar_texto

LOTE = 500


def rellenar(apps, schema_editor):
    MovimientoBancario = apps.get_model('extractos', 'MovimientoBancario')

    pendientes = MovimientoBancario.objects.filter(comercio='').only(
        'id', 'concepto', 'concepto_raw',
    )
    buffer = []
    for mov in pendientes.iterator(chunk_size=LOTE):
        mov.comercio = normalizar_comercio(mov.concepto)
        if not mov.concepto_raw:
            mov.concepto_raw = mov.concepto
        buffer.append(mov)
        if len(buffer) >= LOTE:
            MovimientoBancario.objects.bulk_update(buffer, ['comercio', 'concepto_raw'])
            buffer = []
    if buffer:
        MovimientoBancario.objects.bulk_update(buffer, ['comercio', 'concepto_raw'])


def normalizar_reglas(apps, schema_editor):
    ReglaCategorizacion = apps.get_model('extractos', 'ReglaCategorizacion')

    vistos = set()
    for regla in ReglaCategorizacion.objects.all().order_by('pk'):
        patron = normalizar_texto(regla.patron)
        clave = (regla.hogar_id, patron)
        if not patron or clave in vistos:
            # Patrón vacío o duplicado tras normalizar: la regla más antigua ya
            # cubre el caso y mantenerla rompería la restricción única.
            regla.delete()
            continue
        vistos.add(clave)
        if patron != regla.patron:
            regla.patron = patron
            regla.save(update_fields=['patron'])


def sin_cambios(apps, schema_editor):
    """Marcha atrás: los campos rellenados se pierden con el rollback del
    esquema, así que no hay nada que revertir aquí."""


class Migration(migrations.Migration):

    dependencies = [
        ('extractos', '0003_movimientobancario_comercio_and_more'),
    ]

    operations = [
        migrations.RunPython(rellenar, sin_cambios),
        migrations.RunPython(normalizar_reglas, sin_cambios),
    ]
