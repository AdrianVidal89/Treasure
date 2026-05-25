# Generated manually — 2026-05-25
# Adds: AjusteIngresoMensual, SubsobreFondo

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('finanzas', '0004_remove_fuenteingreso_importe_and_more'),
    ]

    operations = [
        # ── AjusteIngresoMensual ──────────────────────────────────────────────
        migrations.CreateModel(
            name='AjusteIngresoMensual',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                    serialize=False, verbose_name='ID')),
                ('año', models.IntegerField()),
                ('mes', models.IntegerField(
                    help_text='Número de mes 1-12')),
                ('importe_real', models.DecimalField(
                    max_digits=12, decimal_places=2,
                    help_text='Importe neto real cobrado este mes (ya neto)')),
                ('nota', models.CharField(
                    max_length=255, blank=True, default='',
                    help_text='Ej: Solo 3 guardias este mes')),
                ('creado_en', models.DateTimeField(auto_now_add=True)),
                ('fuente', models.ForeignKey(
                    to='finanzas.fuenteingreso',
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='ajustes_mensuales')),
            ],
            options={
                'ordering': ['-año', '-mes'],
                'unique_together': {('fuente', 'año', 'mes')},
            },
        ),

        # ── SubsobreFondo ─────────────────────────────────────────────────────
        migrations.CreateModel(
            name='SubsobreFondo',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                    serialize=False, verbose_name='ID')),
                ('nombre', models.CharField(
                    max_length=100,
                    help_text='Ej: Ocio, Restaurantes, Alimentación...')),
                ('tipo', models.CharField(
                    max_length=20,
                    choices=[
                        ('gasto_fijo',     'Cubre gasto fijo del hogar'),
                        ('gasto_variable', 'Cubre gasto variable del hogar'),
                        ('discrecional',   'Gasto discrecional'),
                        ('libre',          'Sin asignación / libre'),
                    ],
                    default='discrecional')),
                ('importe_manual', models.DecimalField(
                    max_digits=10, decimal_places=2, null=True, blank=True,
                    help_text='Importe mensual si no hay partidas vinculadas')),
                ('orden', models.IntegerField(default=0)),
                ('activo', models.BooleanField(default=True)),
                ('fondo', models.ForeignKey(
                    to='finanzas.fondofamiliar',
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='subsobres')),
                ('partidas_vinculadas', models.ManyToManyField(
                    to='finanzas.partidagasto',
                    blank=True,
                    help_text='El importe se calcula sumando estas partidas')),
            ],
            options={
                'ordering': ['fondo', 'orden', 'nombre'],
            },
        ),
    ]
