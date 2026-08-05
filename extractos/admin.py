from django.contrib import admin

from .models import ExtractoBancario, MovimientoBancario, ReglaCategorizacion


@admin.register(ExtractoBancario)
class ExtractoBancarioAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'hogar', 'nombre_banco', 'num_movimientos',
                    'periodo_inicio', 'periodo_fin', 'fecha_importacion')
    list_filter = ('hogar', 'nombre_banco')
    search_fields = ('nombre_banco', 'archivo_nombre')


@admin.register(MovimientoBancario)
class MovimientoBancarioAdmin(admin.ModelAdmin):
    list_display = ('fecha', 'concepto', 'comercio', 'importe', 'categoria',
                    'estado_categorizacion', 'es_traspaso', 'hogar')
    list_filter = ('hogar', 'estado_categorizacion', 'es_traspaso', 'categoria')
    search_fields = ('concepto', 'comercio')
    date_hierarchy = 'fecha'


@admin.register(ReglaCategorizacion)
class ReglaCategorizacionAdmin(admin.ModelAdmin):
    list_display = ('patron', 'categoria', 'origen', 'veces_aplicada', 'activo', 'hogar')
    list_filter = ('hogar', 'origen', 'activo')
    search_fields = ('patron',)
