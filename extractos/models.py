import hashlib

from django.contrib.auth.models import User
from django.db import models


class ExtractoBancario(models.Model):
    """Un lote de importación: el CSV de un banco/cuenta subido por el usuario."""

    hogar = models.ForeignKey('core.Hogar', on_delete=models.CASCADE, related_name='extractos')
    usuario = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='extractos_importados',
    )
    nombre_banco = models.CharField(max_length=120, blank=True)
    cuenta = models.ForeignKey(
        'finanzas.CuentaBancaria', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='extractos',
    )
    archivo_nombre = models.CharField(max_length=255, blank=True)
    fecha_importacion = models.DateTimeField(auto_now_add=True)
    periodo_inicio = models.DateField(null=True, blank=True)
    periodo_fin = models.DateField(null=True, blank=True)
    num_movimientos = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-fecha_importacion']
        verbose_name = 'Extracto bancario'
        verbose_name_plural = 'Extractos bancarios'

    def __str__(self):
        etiqueta = self.nombre_banco or self.archivo_nombre or 'Extracto'
        return f"{etiqueta} · {self.num_movimientos} mov."

    def _totales(self):
        agregados = self.movimientos.aggregate(
            ingresos=models.Sum('importe', filter=models.Q(importe__gte=0)),
            gastos=models.Sum('importe', filter=models.Q(importe__lt=0)),
        )
        return agregados

    @property
    def total_ingresos(self):
        return self._totales()['ingresos'] or 0

    @property
    def total_gastos(self):
        return self._totales()['gastos'] or 0

    @property
    def saldo_neto(self):
        return self.total_ingresos + self.total_gastos


class MovimientoBancario(models.Model):
    """Un apunte observado en el extracto. Se cruza con los datos declarados."""

    ESTADO_CHOICES = [
        ('sin_categorizar', 'Sin categorizar'),
        ('por_codigo', 'Categorizado por código'),
        ('por_ia', 'Categorizado por IA'),
        ('manual', 'Categorizado manualmente'),
    ]

    extracto = models.ForeignKey(
        ExtractoBancario, on_delete=models.CASCADE, related_name='movimientos',
    )
    # Denormalizado para consultas y para la restricción de deduplicación por hogar.
    hogar = models.ForeignKey('core.Hogar', on_delete=models.CASCADE, related_name='movimientos_bancarios')

    fecha = models.DateField()
    concepto = models.CharField(max_length=300)
    importe = models.DecimalField(
        max_digits=12, decimal_places=2,
        help_text='Positivo = ingreso, negativo = gasto',
    )
    saldo = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    categoria = models.ForeignKey(
        'finanzas.CategoriaGasto', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='movimientos_bancarios',
    )
    estado_categorizacion = models.CharField(
        max_length=20, choices=ESTADO_CHOICES, default='sin_categorizar',
    )
    # Cruce con lo declarado (conciliación). La IA rellenará esto más adelante.
    partida_conciliada = models.ForeignKey(
        'finanzas.PartidaGasto', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='movimientos_conciliados',
    )

    hash_dedupe = models.CharField(max_length=64, db_index=True, editable=False)
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-fecha', '-id']
        verbose_name = 'Movimiento bancario'
        verbose_name_plural = 'Movimientos bancarios'
        constraints = [
            models.UniqueConstraint(fields=['hogar', 'hash_dedupe'], name='uniq_mov_hogar_hash'),
        ]

    def __str__(self):
        return f"{self.fecha} · {self.concepto[:40]} · {self.importe}"

    @property
    def es_ingreso(self):
        return self.importe is not None and self.importe >= 0

    @staticmethod
    def calcular_hash(fecha, concepto, importe, saldo):
        base = f"{fecha}|{(concepto or '').strip().lower()}|{importe}|{saldo}"
        return hashlib.sha256(base.encode('utf-8')).hexdigest()

    def save(self, *args, **kwargs):
        if not self.hash_dedupe:
            self.hash_dedupe = self.calcular_hash(
                self.fecha, self.concepto, self.importe, self.saldo,
            )
        super().save(*args, **kwargs)
