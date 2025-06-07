from django.db import models
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError

### Modulo general de finanzas ###

class RegistroMensual(models.Model):
    usuario = models.ForeignKey(User, on_delete=models.CASCADE)
    anio = models.IntegerField()
    mes = models.IntegerField()

    # Activos
    total_inversiones = models.DecimalField(max_digits=12, decimal_places=2, default=0)  # ← desde módulo bolsa
    total_vehiculos = models.DecimalField(max_digits=12, decimal_places=2, default=0)    # ← desde módulo vehículos

    # Pasivos
    total_hipotecas = models.DecimalField(max_digits=12, decimal_places=2, default=0)    # ← desde módulo hipotecario
    total_creditos = models.DecimalField(max_digits=12, decimal_places=2, default=0)     # ← autogenerado desde saldo tarjetas
    total_prestamos = models.DecimalField(max_digits=12, decimal_places=2, default=0)    # ← desde módulo préstamos simples

    class Meta:
        unique_together = ('usuario', 'anio', 'mes')

    @property
    def total_liquido(self):
        """Suma de saldos de cuentas bancarias."""
        from finanzas.models import SaldoMensualCuenta
        saldos = SaldoMensualCuenta.objects.filter(registro=self)
        return sum(s.saldo for s in saldos)

    @property
    def total_deuda_tarjetas(self):
        """Suma de saldos en tarjetas de crédito."""
        from finanzas.models import SaldoMensualTarjeta
        saldos = SaldoMensualTarjeta.objects.filter(registro=self)
        return sum(s.saldo for s in saldos)

    @property
    def patrimonio_total(self):
        """Patrimonio neto = activos - pasivos"""
        activos = self.total_liquido + self.total_inversiones + self.total_vehiculos
        pasivos = self.total_hipotecas + self.total_creditos + self.total_prestamos
        return activos - pasivos

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

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['cuenta', 'registro'], name='unique_saldo_por_cuenta_y_registro')
        ]

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

class TarjetaCredito(models.Model):
    usuario = models.ForeignKey(User, on_delete=models.CASCADE)
    nombre = models.CharField(max_length=100)
    entidad = models.CharField(max_length=100, blank=True)
    activa = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.nombre} ({self.entidad})"

class SaldoMensualTarjeta(models.Model):
    tarjeta = models.ForeignKey(TarjetaCredito, on_delete=models.CASCADE)
    registro = models.ForeignKey(RegistroMensual, on_delete=models.CASCADE)
    saldo = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        unique_together = ('tarjeta', 'registro')

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.actualizar_total_creditos()

    def actualizar_total_creditos(self):
        total = SaldoMensualTarjeta.objects.filter(
            registro=self.registro
        ).aggregate(models.Sum('saldo'))['saldo__sum'] or 0

        self.registro.total_creditos = total
        self.registro.save()
    
    def delete(self, *args, **kwargs):
        registro = self.registro
        super().delete(*args, **kwargs)
        # Recalcular después de eliminar
        total = SaldoMensualTarjeta.objects.filter(
            registro=registro
        ).aggregate(models.Sum('saldo'))['saldo__sum'] or 0

        registro.total_creditos = total
        registro.save()

### Sub-Modulo Inversiones ###


TIPOS_INVERSION = [
    ('ACCION', 'Acción'),
    ('CRIPTO', 'Criptomoneda'),
    ('FONDO', 'Fondo'),
    ('ETF', 'ETF'),
    ('OTRO', 'Otro'),
]

class Inversion(models.Model):
    usuario = models.ForeignKey(User, on_delete=models.CASCADE)
    nombre = models.CharField(max_length=100)
    ticker = models.CharField(max_length=20, blank=True, null=True)
    tipo = models.CharField(max_length=30, choices=TIPOS_INVERSION)
    moneda = models.CharField(max_length=10, default="EUR")
    plataforma = models.CharField(max_length=100, blank=True, null=True)
    cantidad_actual = models.DecimalField(
        max_digits=20, decimal_places=8, default=0,
        help_text="Cantidad actual de activos"
    )
    actualizable = models.BooleanField(
        default=True,
        help_text="Si está marcado, se actualizará el valor automáticamente vía API"
    )
    fecha_creacion = models.DateField(auto_now_add=True)

    def __str__(self):
        return f"{self.nombre} ({self.ticker})"
    
    @property
    def valor_total_actual(self):
        try:
            valor_unitario = self.valor_actual.valor_unitario
        except AttributeError:
            return 0

        return round(valor_unitario * self.cantidad_actual, 2)
    
    @property
    def total_activos(self):
        return self.movimientos.aggregate(
            total=models.Sum(
                models.Case(
                    models.When(tipo='COMPRA', then='cantidad'),
                    models.When(tipo='VENTA', then=-1 * models.F('cantidad')),
                    default=0,
                    output_field=models.DecimalField()
                )
            )
        )['total'] or 0

    @property
    def valor_aportado(self):
        return self.movimientos.filter(tipo='COMPRA').aggregate(
            total=models.Sum(models.F('cantidad') * models.F('precio_unitario'))
        )['total'] or 0

class MovimientoInversion(models.Model):
    COMPRA = 'COMPRA'
    VENTA = 'VENTA'
    DIVIDENDO = 'DIVIDENDO'
    TIPOS_MOVIMIENTO = [
        (COMPRA, 'Compra'),
        (VENTA, 'Venta'),
        (DIVIDENDO, 'Dividendo'),
    ]

    inversion = models.ForeignKey(Inversion, on_delete=models.CASCADE, related_name='movimientos')
    fecha = models.DateField()
    tipo = models.CharField(max_length=20, choices=TIPOS_MOVIMIENTO)
    cantidad = models.DecimalField(max_digits=20, decimal_places=8)  # Ej: 0.5 BTC
    precio_unitario = models.DecimalField(max_digits=20, decimal_places=8)  # Precio por unidad
    comision = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    def valor_total(self):
        return (self.cantidad * self.precio_unitario) + self.comision
    
    def clean(self):
        # No validar si todavía no hay inversión asociada
        if not self.inversion_id:
            return

        if self.tipo == self.VENTA:
            total_comprado = MovimientoInversion.objects.filter(
                inversion=self.inversion,
                tipo=self.COMPRA
            ).aggregate(models.Sum('cantidad'))['cantidad__sum'] or 0

            total_vendido = MovimientoInversion.objects.filter(
                inversion=self.inversion,
                tipo=self.VENTA
            ).exclude(id=self.id).aggregate(models.Sum('cantidad'))['cantidad__sum'] or 0

            if self.cantidad > (total_comprado - total_vendido):
                raise ValidationError("No se pueden vender más unidades de las que se han comprado.")

class ValorActualInversion(models.Model):
    inversion = models.OneToOneField(Inversion, on_delete=models.CASCADE, related_name='valor_actual')
    valor_unitario = models.DecimalField(max_digits=20, decimal_places=8)
    fecha_actualizacion = models.DateTimeField(auto_now=True)
    fuente = models.CharField(max_length=100, blank=True, null=True)  # Ej: CoinGecko, YahooFinance, Manual

    def __str__(self):
        return f"{self.inversion} → {self.valor_unitario} ({self.fecha_actualizacion.date()})"

class ResumenInversionesMensual(models.Model):
    usuario = models.ForeignKey(User, on_delete=models.CASCADE)
    registro = models.ForeignKey(RegistroMensual, on_delete=models.CASCADE)
    total_valor = models.DecimalField(max_digits=20, decimal_places=2)
    total_aportado = models.DecimalField(max_digits=20, decimal_places=2)
    total_rentabilidad = models.DecimalField(max_digits=20, decimal_places=2)
    variacion_mensual = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        unique_together = ('usuario', 'registro')

class HistorialValorInversion(models.Model):
    inversion = models.ForeignKey(Inversion, on_delete=models.CASCADE, related_name='historial_valores')
    valor_unitario = models.DecimalField(max_digits=20, decimal_places=8)
    cantidad_activos = models.DecimalField(
        max_digits=20, decimal_places=8,
        help_text="Cantidad de activos en esa fecha"
    )
    fecha = models.DateField()
    fuente = models.CharField(max_length=100, blank=True, null=True)

    class Meta:
        unique_together = ('inversion', 'fecha')

    def __str__(self):
        return f"{self.inversion} @ {self.fecha} → {self.valor_unitario} x {self.cantidad_activos}"
