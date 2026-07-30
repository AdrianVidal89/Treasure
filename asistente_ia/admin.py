from django.contrib import admin

from .models import (
    ConfiguracionIA, AgenteIA, ConversacionIA, MensajeIA, AccionPropuestaIA,
)


@admin.register(ConfiguracionIA)
class ConfiguracionIAAdmin(admin.ModelAdmin):
    list_display = ('hogar', 'proveedor_activo', 'actualizado_en')


@admin.register(AgenteIA)
class AgenteIAAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'hogar', 'es_predeterminado', 'activo', 'allowed_tools')
    list_filter = ('es_predeterminado', 'activo')
    search_fields = ('nombre',)


@admin.register(ConversacionIA)
class ConversacionIAAdmin(admin.ModelAdmin):
    list_display = ('titulo', 'usuario', 'hogar', 'agente', 'actualizado_en')
    list_filter = ('hogar',)


@admin.register(MensajeIA)
class MensajeIAAdmin(admin.ModelAdmin):
    list_display = ('conversacion', 'rol', 'creado_en')
    list_filter = ('rol',)


@admin.register(AccionPropuestaIA)
class AccionPropuestaIAAdmin(admin.ModelAdmin):
    list_display = ('tipo_accion', 'conversacion', 'estado', 'creado_en', 'resuelto_en')
    list_filter = ('tipo_accion', 'estado')
