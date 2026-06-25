# Migración vacía — sus operaciones están cubiertas por:
#   0005_inversion_fondo (AddField fondo)
#   0006_tickercatalogo_alter_fondofamiliar_cuenta_asociada_and_more (CreateModel TickerCatalogo + AlterField)
# Se mantiene como marcador para que 0007_merge pueda depender de ella.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("finanzas", "0004_subsobrefondo_solo_mes"),
    ]

    operations = [
    ]
