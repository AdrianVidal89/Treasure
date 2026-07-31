"""Concede la nueva acción 'categorizar_movimientos' a los agentes existentes
que ya tenían permisos de escritura (allowed_tools no vacío), para que puedan
proponer categorizaciones de movimientos bancarios sin tener que reconfigurarlos
a mano. Los agentes de solo lectura (allowed_tools vacío) se mantienen igual.
"""
from django.db import migrations

NUEVA = 'categorizar_movimientos'


def conceder(apps, schema_editor):
    AgenteIA = apps.get_model('asistente_ia', 'AgenteIA')
    for agente in AgenteIA.objects.all():
        tools = list(agente.allowed_tools or [])
        if tools and NUEVA not in tools:
            tools.append(NUEVA)
            agente.allowed_tools = tools
            agente.save(update_fields=['allowed_tools'])


def revertir(apps, schema_editor):
    AgenteIA = apps.get_model('asistente_ia', 'AgenteIA')
    for agente in AgenteIA.objects.all():
        tools = list(agente.allowed_tools or [])
        if NUEVA in tools:
            tools.remove(NUEVA)
            agente.allowed_tools = tools
            agente.save(update_fields=['allowed_tools'])


class Migration(migrations.Migration):

    dependencies = [
        ('asistente_ia', '0004_backfill_allowed_tools_y_hints'),
    ]

    operations = [
        migrations.RunPython(conceder, revertir),
    ]
