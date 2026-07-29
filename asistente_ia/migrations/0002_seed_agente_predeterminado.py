from django.db import migrations

from asistente_ia.seeds import (
    INSTRUCCIONES_AGENTE_PREDETERMINADO,
    NOMBRE_AGENTE_PREDETERMINADO,
)


def crear_agentes_predeterminados(apps, schema_editor):
    Hogar = apps.get_model('core', 'Hogar')
    AgenteIA = apps.get_model('asistente_ia', 'AgenteIA')
    for hogar in Hogar.objects.all():
        AgenteIA.objects.get_or_create(
            hogar=hogar, es_predeterminado=True,
            defaults={
                'nombre': NOMBRE_AGENTE_PREDETERMINADO,
                'descripcion': (
                    'Agente especializado en finanzas del hogar, gestión patrimonial '
                    'y análisis de activos.'
                ),
                'instrucciones': INSTRUCCIONES_AGENTE_PREDETERMINADO,
                'activo': True,
            },
        )


def eliminar_agentes_predeterminados(apps, schema_editor):
    AgenteIA = apps.get_model('asistente_ia', 'AgenteIA')
    AgenteIA.objects.filter(
        es_predeterminado=True, nombre=NOMBRE_AGENTE_PREDETERMINADO
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('asistente_ia', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(crear_agentes_predeterminados, eliminar_agentes_predeterminados),
    ]
