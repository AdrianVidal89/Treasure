from django.db import models
from django.contrib.auth.models import User


class RegistroMensual(models.Model):
    usuario = models.ForeignKey(User, on_delete=models.CASCADE)
    anio = models.IntegerField()
    mes = models.IntegerField()
    total_inversiones = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_vehiculos = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_hipotecas = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('usuario', 'anio', 'mes')

MONEDAS = [
    ('EUR', '€ Euro'),
    ('USD', '$ Dólar'),
    ('BTC', '₿ Bitcoin'),
]

class CuentaBancaria(models.Model):
    usuario = models.ForeignKey(User, on_delete=models.CASCADE)
    nombre = models.CharField(max_length=100)
    moneda = models.CharField(max_length=3, choices=MONEDAS, default='EUR')
    activa = models.BooleanField(default=True)

class SaldoMensualCuenta(models.Model):
    cuenta = models.ForeignKey(CuentaBancaria, on_delete=models.CASCADE)
    registro = models.ForeignKey(RegistroMensual, on_delete=models.CASCADE)
    saldo = models.DecimalField(max_digits=12, decimal_places=2)

class CuentaCredito(models.Model):
    usuario = models.ForeignKey(User, on_delete=models.CASCADE)
    nombre = models.CharField(max_length=100)
    entidad = models.CharField(max_length=100, blank=True)
    activa = models.BooleanField(default=True)

class DeudaMensualCredito(models.Model):
    cuenta = models.ForeignKey(CuentaCredito, on_delete=models.CASCADE)
    registro = models.ForeignKey(RegistroMensual, on_delete=models.CASCADE)
    deuda = models.DecimalField(max_digits=12, decimal_places=2)

class PrestamoSimple(models.Model):
    usuario = models.ForeignKey(User, on_delete=models.CASCADE)
    nombre = models.CharField(max_length=100)
    entidad = models.CharField(max_length=100, blank=True)
    total_pendiente = models.DecimalField(max_digits=12, decimal_places=2)
    registro = models.ForeignKey(RegistroMensual, on_delete=models.CASCADE)
