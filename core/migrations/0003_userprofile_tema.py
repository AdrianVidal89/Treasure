from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0002_userprofile_rol_hogar_userprofile_hogar'),
    ]

    operations = [
        migrations.AddField(
            model_name='userprofile',
            name='tema',
            field=models.CharField(
                choices=[('claro', 'Claro'), ('black', 'Black')],
                default='claro',
                max_length=10,
                verbose_name='Tema de la interfaz',
            ),
        ),
    ]
