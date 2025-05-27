from django.contrib import admin
from .models import CuentaBancaria, SaldoMensualCuenta, RegistroMensual, TarjetaCredito
from django.contrib import admin
from .models import Inversion, MovimientoInversion, ValorActualInversion, ResumenInversionesMensual, HistorialValorInversion

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
