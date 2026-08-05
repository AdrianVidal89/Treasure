"""Recoloca las categorías predefinidas en los bloques nuevos.

Al introducir el bloque «Discrecionales» y las categorías de ingreso/traspaso,
los hogares que ya existían siguen teniendo Ocio, Restaurantes, Ropa,
Suscripciones y Tecnología / Software en Fijos o Variables. Sin este ajuste, el
presupuesto y la conciliación contra el extracto compararían bloques distintos.

Solo se tocan las categorías marcadas como predefinidas: si el usuario creó a
mano una categoría con ese nombre, su criterio manda y se deja como está.
"""

from django.db import migrations

TIPOS_CORREGIDOS = {
    'Ocio': ('discrecional', 'variable'),
    'Restaurantes': ('discrecional', 'variable'),
    'Ropa': ('discrecional', 'variable'),
    'Suscripciones': ('discrecional', 'fijo'),
    'Tecnologia / Software': ('discrecional', 'variable'),
}

CATEGORIAS_NUEVAS = [
    ('ingreso', 'Nomina'),
    ('ingreso', 'Intereses'),
    ('ingreso', 'Devoluciones'),
    ('ingreso', 'Otros ingresos'),
    ('traspaso', 'Traspaso entre cuentas'),
]


def recolocar(apps, schema_editor):
    CategoriaGasto = apps.get_model('finanzas', 'CategoriaGasto')
    Hogar = apps.get_model('core', 'Hogar')

    for nombre, (nuevo, _antiguo) in TIPOS_CORREGIDOS.items():
        CategoriaGasto.objects.filter(
            nombre=nombre, es_predefinida=True,
        ).exclude(tipo=nuevo).update(tipo=nuevo)

    # Las categorías de ingreso y traspaso hacen falta desde ya: la importación
    # de extractos las busca por nombre para clasificar los movimientos.
    for hogar in Hogar.objects.all():
        for tipo, nombre in CATEGORIAS_NUEVAS:
            CategoriaGasto.objects.get_or_create(
                hogar=hogar, nombre=nombre,
                defaults={'tipo': tipo, 'es_predefinida': True, 'activo': True},
            )


def deshacer(apps, schema_editor):
    """Devuelve las recolocadas a su bloque anterior. Las categorías nuevas se
    dejan: borrarlas arrastraría los movimientos ya clasificados en ellas."""
    CategoriaGasto = apps.get_model('finanzas', 'CategoriaGasto')
    for nombre, (nuevo, antiguo) in TIPOS_CORREGIDOS.items():
        CategoriaGasto.objects.filter(
            nombre=nombre, es_predefinida=True, tipo=nuevo,
        ).update(tipo=antiguo)


class Migration(migrations.Migration):

    dependencies = [
        ('finanzas', '0015_alter_categoriagasto_tipo'),
        ('core', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(recolocar, deshacer),
    ]
