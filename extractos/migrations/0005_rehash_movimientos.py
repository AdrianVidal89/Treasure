"""Recalcula `hash_dedupe` con la fórmula que normaliza los importes.

Los hashes guardados usaban la representación textual del Decimal tal cual, así
que un mismo apunte importado desde un CSV y desde un Excel («-1700.00» frente
a «-1700») generaba dos hashes distintos y entraba por duplicado. Sin este
recálculo, los movimientos ya guardados seguirían con el hash antiguo y volverían
a duplicarse en la próxima importación del mismo periodo.

Si dos filas existentes pasan a compartir hash, son duplicados reales que la
restricción única habría impedido. No se borra nada: se deja el hash antiguo en
la fila más reciente para que el usuario decida, y así la migración nunca
destruye datos ni falla por IntegrityError.
"""

from django.db import migrations

from extractos.models import MovimientoBancario

LOTE = 500


def rehashear(apps, schema_editor):
    Movimiento = apps.get_model('extractos', 'MovimientoBancario')

    vistos = set()
    buffer = []
    consulta = Movimiento.objects.all().order_by('hogar_id', 'pk').only(
        'id', 'hogar_id', 'fecha', 'concepto', 'importe', 'saldo', 'hash_dedupe',
    )
    for mov in consulta.iterator(chunk_size=LOTE):
        nuevo = MovimientoBancario.calcular_hash(
            mov.fecha, mov.concepto, mov.importe, mov.saldo,
        )
        clave = (mov.hogar_id, nuevo)
        if clave in vistos:
            # Colisión: es un duplicado real. Se respeta el hash actual para no
            # borrar el apunte ni romper la restricción única.
            vistos.add((mov.hogar_id, mov.hash_dedupe))
            continue
        vistos.add(clave)
        if nuevo != mov.hash_dedupe:
            mov.hash_dedupe = nuevo
            buffer.append(mov)
        if len(buffer) >= LOTE:
            Movimiento.objects.bulk_update(buffer, ['hash_dedupe'])
            buffer = []
    if buffer:
        Movimiento.objects.bulk_update(buffer, ['hash_dedupe'])


def sin_vuelta_atras(apps, schema_editor):
    """No se revierte: recalcular con la fórmula antigua reintroduciría los
    duplicados que esta migración evita."""


class Migration(migrations.Migration):

    dependencies = [
        ('extractos', '0004_backfill_comercio'),
    ]

    operations = [
        migrations.RunPython(rehashear, sin_vuelta_atras),
    ]
