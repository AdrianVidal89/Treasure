"""Backfill para preservar el comportamiento actual tras introducir
`AgenteIA.allowed_tools` y los campos `*_api_key_hint`:

- Antes de este cambio, CUALQUIER agente podía proponer las 4 acciones de
  escritura originales (no había distinción de permisos por agente). Para no
  revocar silenciosamente esa capacidad a los agentes ya existentes, se les
  concede aquí el conjunto curado completo. Los agentes creados a partir de
  ahora arrancan en `[]` (solo lectura) por defecto, según el formulario.
- Los hints de "últimos 4 caracteres" se calculan descifrando la clave ya
  guardada, una sola vez, para que la UI no se quede en blanco hasta que el
  usuario reintroduzca la clave.
"""
from django.db import migrations

TIPOS_ORIGINALES = [
    'crear_gasto', 'editar_gasto', 'crear_categoria_gasto', 'crear_ingreso', 'editar_ingreso', 'crear_agente',
]


def backfill_allowed_tools(apps, schema_editor):
    AgenteIA = apps.get_model('asistente_ia', 'AgenteIA')
    AgenteIA.objects.filter(allowed_tools=[]).update(allowed_tools=TIPOS_ORIGINALES)


def revertir_allowed_tools(apps, schema_editor):
    AgenteIA = apps.get_model('asistente_ia', 'AgenteIA')
    AgenteIA.objects.all().update(allowed_tools=[])


def backfill_hints(apps, schema_editor):
    from asistente_ia.crypto import descifrar

    ConfiguracionIA = apps.get_model('asistente_ia', 'ConfiguracionIA')
    for config in ConfiguracionIA.objects.all():
        cambiado = False
        for proveedor in ('anthropic', 'openai', 'gemini'):
            valor_cifrado = getattr(config, f'{proveedor}_api_key_cifrada')
            if not valor_cifrado:
                continue
            try:
                clave = descifrar(valor_cifrado)
            except Exception:
                clave = None
            if clave:
                setattr(config, f'{proveedor}_api_key_hint', clave[-4:])
                cambiado = True
        if cambiado:
            config.save()


def revertir_hints(apps, schema_editor):
    ConfiguracionIA = apps.get_model('asistente_ia', 'ConfiguracionIA')
    ConfiguracionIA.objects.update(
        anthropic_api_key_hint='', openai_api_key_hint='', gemini_api_key_hint='',
    )


class Migration(migrations.Migration):

    dependencies = [
        ('asistente_ia', '0003_agenteia_allowed_tools_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill_allowed_tools, revertir_allowed_tools),
        migrations.RunPython(backfill_hints, revertir_hints),
    ]
