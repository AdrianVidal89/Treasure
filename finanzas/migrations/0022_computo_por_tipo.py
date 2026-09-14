"""Cómputo inicial de las categorías existentes, deducido de su bloque.

Hasta ahora el análisis de extractos decidía por el signo del importe, así que
los traspasos entre cuentas propias engordaban el gasto del mes. Con el cómputo
esa decisión pasa a la categoría: las de traspaso nacen neutras, las de ingreso
suman y el resto restan. A partir de aquí es editable por el usuario.
"""

from django.db import migrations

COMPUTO_POR_TIPO = {
    'fijo': 'resta',
    'anual': 'resta',
    'variable': 'resta',
    'discrecional': 'resta',
    'ingreso': 'suma',
    'traspaso': 'neutro',
}


def aplicar(apps, schema_editor):
    CategoriaGasto = apps.get_model('finanzas', 'CategoriaGasto')
    for tipo, computo in COMPUTO_POR_TIPO.items():
        if computo == 'resta':
            continue  # ya es el valor por defecto de la columna
        CategoriaGasto.objects.filter(tipo=tipo).update(computo=computo)


def revertir(apps, schema_editor):
    # El cómputo se deduce del tipo, así que no hay nada que restaurar.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('finanzas', '0021_categoriagasto_computo'),
    ]

    operations = [
        migrations.RunPython(aplicar, revertir),
    ]
