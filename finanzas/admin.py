from django.contrib import admin
from .models import CuentaBancaria, SaldoMensualCuenta, RegistroMensual, TarjetaCredito

admin.site.register(CuentaBancaria)
admin.site.register(SaldoMensualCuenta)
admin.site.register(RegistroMensual)
admin.site.register(TarjetaCredito)
