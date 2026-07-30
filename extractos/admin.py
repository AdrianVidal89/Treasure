from django.contrib import admin

from .models import ExtractoBancario, MovimientoBancario


@admin.register(ExtractoBancario)
class ExtractoBancarioAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'hogar', 'nombre_banco', 'num_movimientos',
                    'periodo_inicio', 'periodo_fin', 'fecha_importacion')
    list_filter = ('hogar', 'nombre_banco')
    search_fields = ('nombre_banco', 'archivo_nombre')


@admin.register(MovimientoBancario)
class MovimientoBancarioAdmin(admin.ModelAdmin):
    list_display = ('fecha', 'concepto', 'importe', 'categoria',
                    'estado_categorizacion', 'hogar')
    list_filter = ('hogar', 'estado_categorizacion', 'categoria')
    search_fields = ('concepto',)
    date_hierarchy = 'fecha'
