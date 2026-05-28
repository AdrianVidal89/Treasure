from django.contrib import admin
from .models import CuentaBancaria, SaldoMensualCuenta, RegistroMensual, TarjetaCredito
from .models import Inversion, MovimientoInversion, ValorActualInversion, ResumenInversionesMensual, HistorialValorInversion
from .models import TablaIRPF, CotizacionSS, DestinoIngreso, FuenteIngreso
from .models import CategoriaGasto, PartidaGasto, FondoFamiliar, ReglaReparto

admin.site.register(CuentaBancaria)
admin.site.register(SaldoMensualCuenta)
admin.site.register(RegistroMensual)
admin.site.register(TarjetaCredito)

@admin.register(Inversion)
class InversionAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'ticker', 'tipo', 'plataforma', 'usuario')
    search_fields = ('nombre', 'ticker', 'plataforma')
    list_filter = ('tipo', 'plataforma')

@admin.register(MovimientoInversion)
class MovimientoAdmin(admin.ModelAdmin):
    list_display = ('inversion', 'fecha', 'tipo', 'cantidad', 'precio_unitario')
    list_filter = ('tipo',)

@admin.register(ValorActualInversion)
class ValorActualAdmin(admin.ModelAdmin):
    list_display = ('inversion', 'valor_unitario', 'fecha_actualizacion', 'fuente')

@admin.register(ResumenInversionesMensual)
class ResumenAdmin(admin.ModelAdmin):
    list_display = ('usuario', 'registro', 'total_valor', 'total_rentabilidad')

@admin.register(HistorialValorInversion)
class HistorialAdmin(admin.ModelAdmin):
    list_display = ('inversion', 'fecha', 'valor_unitario', 'fuente')

@admin.register(TablaIRPF)
class TablaIRPFAdmin(admin.ModelAdmin):
    list_display = ('pais', 'tramo_desde', 'tramo_hasta', 'porcentaje')
    list_filter = ('pais',)

@admin.register(CotizacionSS)
class CotizacionSSAdmin(admin.ModelAdmin):
    list_display = ('pais', 'concepto', 'porcentaje_trabajador')

@admin.register(DestinoIngreso)
class DestinoIngresoAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'hogar', 'es_predefinido', 'activo')

@admin.register(FuenteIngreso)
class FuenteIngresoAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'usuario', 'hogar', 'importe_declarado', 'es_bruto', 'modo_entrada', 'periodicidad', 'activo')
    list_filter = ('hogar', 'periodicidad', 'es_bruto')

@admin.register(CategoriaGasto)
class CategoriaGastoAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'tipo', 'hogar', 'es_predefinida', 'activo')
    list_filter = ('tipo', 'hogar')

@admin.register(PartidaGasto)
class PartidaGastoAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'categoria', 'hogar', 'importe', 'periodicidad', 'responsable', 'activo')
    list_filter = ('categoria__tipo', 'hogar')

@admin.register(FondoFamiliar)
class FondoFamiliarAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'hogar', 'modo_aportacion', 'cuenta_asociada', 'color', 'activo')
    list_filter = ('hogar', 'modo_aportacion')

@admin.register(ReglaReparto)
class ReglaRepartoAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'hogar', 'usuario', 'tipo_regla', 'porcentaje', 'importe_fijo', 'fondo', 'activo')
    list_filter = ('hogar', 'tipo_regla')

from .models import AjusteIngresoMensual, SubsobreFondo, IngresoExtraordinario

@admin.register(AjusteIngresoMensual)
class AjusteIngresoMensualAdmin(admin.ModelAdmin):
    list_display = ('fuente', 'año', 'mes', 'importe_real', 'nota', 'creado_en')
    list_filter = ('año', 'mes')

@admin.register(SubsobreFondo)
class SubsobreFondoAdmin(admin.ModelAdmin):
    list_display = ('fondo', 'nombre', 'tipo', 'importe_manual', 'orden', 'activo')
    list_filter = ('fondo', 'tipo')

@admin.register(IngresoExtraordinario)
class IngresoExtraordinarioAdmin(admin.ModelAdmin):
    list_display = ('concepto', 'usuario', 'hogar', 'importe', 'año', 'mes', 'fondo_destino')
    list_filter = ('hogar', 'año', 'mes')
