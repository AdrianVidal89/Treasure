import hashlib
from decimal import Decimal, InvalidOperation

from django.contrib.auth.models import User
from django.db import models

from .normalizacion import normalizar_comercio, normalizar_texto


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


class ReglaCategorizacion(models.Model):
    """Regla aprendida: si el concepto de un movimiento contiene `patron`,
    se asigna `categoria`. Se crea cuando el usuario (o la IA bajo su
    aprobación) categoriza movimientos, de modo que futuras importaciones
    apliquen automáticamente el mismo criterio.

    El origen indica quién la creó, solo a efectos informativos/auditoría."""

    ORIGEN_CHOICES = [
        ('manual', 'Manual'),
        ('ia', 'Asistente IA'),
    ]

    hogar = models.ForeignKey('core.Hogar', on_delete=models.CASCADE, related_name='reglas_categorizacion')
    patron = models.CharField(
        max_length=200,
        help_text='Texto que debe contener el concepto (se compara en minúsculas, sin distinguir acentos de mayúsculas).',
    )
    categoria = models.ForeignKey(
        'finanzas.CategoriaGasto', on_delete=models.CASCADE, related_name='reglas_categorizacion',
    )
    origen = models.CharField(max_length=10, choices=ORIGEN_CHOICES, default='manual')
    veces_aplicada = models.PositiveIntegerField(default=0)
    activo = models.BooleanField(default=True)
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-creado_en']
        constraints = [
            models.UniqueConstraint(fields=['hogar', 'patron'], name='uniq_regla_cat_hogar_patron'),
        ]
        verbose_name = 'Regla de categorización'
        verbose_name_plural = 'Reglas de categorización'

    def __str__(self):
        return f"«{self.patron}» → {self.categoria.nombre}"

    def save(self, *args, **kwargs):
        # Los patrones se guardan ya normalizados para que la restricción de
        # unicidad (hogar, patron) no deje pasar «Mercadona» y «MERCADONA» como
        # dos reglas distintas, y para que el matching no tenga que normalizar
        # en cada comparación.
        self.patron = normalizar_texto(self.patron)
        super().save(*args, **kwargs)


class MovimientoBancario(models.Model):
    """Un apunte observado en el extracto. Se cruza con los datos declarados."""

    ESTADO_CHOICES = [
        ('sin_categorizar', 'Sin categorizar'),
        ('por_codigo', 'Categorizado por código'),
        ('por_regla', 'Categorizado por una regla aprendida'),
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
    # Descripción original tal cual venía del banco (el concepto es editable por
    # el usuario y además se recorta a 300 caracteres, así que sin esto se
    # perdería el texto de partida).
    concepto_raw = models.TextField(blank=True)
    # Clave normalizada del comercio, usada para agrupar movimientos «similares»
    # y para proponer el patrón al aprender una regla. Se calcula en save().
    comercio = models.CharField(max_length=120, blank=True, db_index=True)
    importe = models.DecimalField(
        max_digits=12, decimal_places=2,
        help_text='Positivo = ingreso, negativo = gasto',
    )
    saldo = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    # Traspaso entre cuentas propias: no es gasto ni ingreso real, así que se
    # excluye de los KPIs y de la conciliación.
    es_traspaso = models.BooleanField(default=False)

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
    def _importe_canonico(valor):
        """Importe con exactamente dos decimales para el hash.

        El mismo apunte llega con distinta representación según el formato del
        archivo (el CSV trae «-1,700.00» → Decimal('-1700.00') y el Excel trae
        el número → Decimal('-1700')). Sin normalizar, el mismo movimiento
        exportado en CSV y en Excel producía dos hashes distintos y se colaba
        por duplicado."""
        if valor is None or valor == '':
            return ''
        try:
            return str(Decimal(str(valor)).quantize(Decimal('0.01')))
        except (InvalidOperation, ValueError, TypeError):
            return str(valor)

    @staticmethod
    def calcular_hash(fecha, concepto, importe, saldo):
        base = '|'.join([
            str(fecha),
            (concepto or '').strip().lower(),
            MovimientoBancario._importe_canonico(importe),
            MovimientoBancario._importe_canonico(saldo),
        ])
        return hashlib.sha256(base.encode('utf-8')).hexdigest()

    def save(self, *args, **kwargs):
        if not self.comercio:
            self.comercio = normalizar_comercio(self.concepto)
        # El hash se recalcula siempre a partir de los valores actuales: si solo
        # se rellenara cuando está vacío, editar el concepto o el importe
        # dejaría un hash obsoleto y la deduplicación de futuras importaciones
        # compararía contra datos que ya no existen.
        self.hash_dedupe = self.calcular_hash(
            self.fecha, self.concepto, self.importe, self.saldo,
        )
        update_fields = kwargs.get('update_fields')
        if update_fields is not None:
            kwargs['update_fields'] = set(update_fields) | {'comercio', 'hash_dedupe'}
        super().save(*args, **kwargs)
