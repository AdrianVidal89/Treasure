from django.contrib import admin
from .models import CuentaBancaria, SaldoMensualCuenta, RegistroMensual, TarjetaCredito
from django.contrib import admin
from .models import Inversion, MovimientoInversion, ValorActualInversion, ResumenInversionesMensual, HistorialValorInversion
from .models import TablaIRPF, CotizacionSS, DestinoIngreso, FuenteIngreso

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
    search_fields = ('inversion__nombre',)

@admin.register(ValorActualInversion)
class ValorActualAdmin(admin.ModelAdmin):
    list_display = ('inversion', 'valor_unitario', 'fecha_actualizacion', 'fuente')

@admin.register(ResumenInversionesMensual)
class ResumenAdmin(admin.ModelAdmin):
    list_display = ('usuario', 'registro', 'total_valor', 'total_rentabilidad')
    list_filter = ('registro',)

@admin.register(HistorialValorInversion)
class HistorialValorInversionAdmin(admin.ModelAdmin):
    list_display = ('inversion', 'fecha', 'valor_unitario', 'fuente')
    list_filter = ('fecha', 'fuente')
    search_fields = ('inversion__nombre',)

@admin.register(TablaIRPF)
class TablaIRPFAdmin(admin.ModelAdmin):
    list_display = ('pais', 'año', 'tramo_desde', 'tramo_hasta', 'porcentaje')
    list_filter = ('pais', 'año')

@admin.register(CotizacionSS)
class CotizacionSSAdmin(admin.ModelAdmin):
    list_display = ('pais', 'año', 'concepto', 'porcentaje_trabajador')
    list_filter = ('pais', 'año')

@admin.register(DestinoIngreso)
class DestinoIngresoAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'hogar', 'es_predefinido', 'activo')
    list_filter = ('hogar',)

@admin.register(FuenteIngreso)
class FuenteIngresoAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'usuario', 'hogar', 'importe', 'es_bruto', 'periodicidad', 'activo')
    list_filter = ('hogar', 'periodicidad', 'es_bruto')
    search_fields = ('nombre', 'usuario__username')