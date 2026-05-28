from django.db import models
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from decimal import Decimal
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

### Módulo de Ingresos ###

class TablaIRPF(models.Model):
    """Tramos fiscales por país. Preparado para multi-país."""
    pais = models.CharField(max_length=5, choices=[
        ('ES', 'España'),
        ('US', 'Estados Unidos'),
        ('UK', 'Reino Unido'),
        ('FR', 'Francia'),
        ('DE', 'Alemania'),
        ('PT', 'Portugal'),
        ('IT', 'Italia'),
        ('MX', 'México'),
        ('CO', 'Colombia'),
        ('AR', 'Argentina'),
    ], default='ES')
    tramo_desde = models.DecimalField(max_digits=12, decimal_places=2)
    tramo_hasta = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Dejar vacío para el último tramo (sin límite)")
    porcentaje = models.DecimalField(max_digits=5, decimal_places=2,
        help_text="Tipo impositivo del tramo en %")
    año = models.IntegerField(help_text="Año fiscal de aplicación")

    class Meta:
        ordering = ['pais', 'año', 'tramo_desde']
        unique_together = ('pais', 'año', 'tramo_desde')

    def __str__(self):
        hasta = f"€{self.tramo_hasta}" if self.tramo_hasta else "∞"
        return f"{self.pais} {self.año}: €{self.tramo_desde} - {hasta} → {self.porcentaje}%"


class CotizacionSS(models.Model):
    """Cotización a la Seguridad Social por país."""
    pais = models.CharField(max_length=5, choices=[
        ('ES', 'España'),
    ], default='ES')
    concepto = models.CharField(max_length=100,
        help_text="Ej: Contingencias comunes, Desempleo, Formación...")
    porcentaje_trabajador = models.DecimalField(max_digits=5, decimal_places=2)
    año = models.IntegerField()

    class Meta:
        ordering = ['pais', 'año', 'concepto']

    def __str__(self):
        return f"{self.pais} {self.año}: {self.concepto} → {self.porcentaje_trabajador}%"


class DestinoIngreso(models.Model):
    """Categorías de destino para ingresos no mensuales. Flexible por hogar."""
    PREDEFINIDOS = ['Ahorro', 'Inversión', 'Fondo de emergencia']

    hogar = models.ForeignKey('core.Hogar', on_delete=models.CASCADE, related_name='destinos_ingreso')
    nombre = models.CharField(max_length=100)
    es_predefinido = models.BooleanField(default=False)
    activo = models.BooleanField(default=True)

    class Meta:
        unique_together = ('hogar', 'nombre')

    def __str__(self):
        return self.nombre


PERIODICIDAD_CHOICES = [
    ('diaria', 'Diaria'),
    ('semanal', 'Semanal'),
    ('mensual', 'Mensual'),
    ('trimestral', 'Trimestral'),
    ('semestral', 'Semestral'),
    ('anual', 'Anual'),
    ('puntual', 'Puntual'),
]

MESES_CHOICES = [
    (1, 'Enero'), (2, 'Febrero'), (3, 'Marzo'), (4, 'Abril'),
    (5, 'Mayo'), (6, 'Junio'), (7, 'Julio'), (8, 'Agosto'),
    (9, 'Septiembre'), (10, 'Octubre'), (11, 'Noviembre'), (12, 'Diciembre'),
]


class FuenteIngreso(models.Model):
    TIPO_CHOICES = [
        ('fijo', 'Fijo'),
        ('variable', 'Variable estimado'),
    ]

    MODO_ENTRADA_CHOICES = [
        ('anual', 'Declaro el total anual'),
        ('periodo', 'Declaro por periodo'),
    ]

    REPARTO_CHOICES = [
        (12, '12 pagas'),
        (14, '14 pagas (extras en junio y diciembre)'),
        (15, '15 pagas'),
    ]

    usuario = models.ForeignKey(User, on_delete=models.CASCADE, related_name='fuentes_ingreso')
    hogar = models.ForeignKey('core.Hogar', on_delete=models.CASCADE, related_name='fuentes_ingreso')

    nombre = models.CharField(max_length=150,
        help_text="Ej: Schneider Electric, Guardias hospital, Alquiler piso...")
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES, default='fijo')

    # Entrada flexible
    modo_entrada = models.CharField(max_length=10, choices=MODO_ENTRADA_CHOICES, default='anual')
    importe_declarado = models.DecimalField(max_digits=12, decimal_places=2,
        help_text="Importe tal como lo introduce el usuario (anual o por periodo)")
    es_bruto = models.BooleanField(default=True)
    pais_fiscal = models.CharField(max_length=5, choices=[
        ('ES', 'España'), ('US', 'Estados Unidos'), ('UK', 'Reino Unido'),
        ('FR', 'Francia'), ('DE', 'Alemania'), ('PT', 'Portugal'),
        ('IT', 'Italia'), ('MX', 'México'), ('CO', 'Colombia'), ('AR', 'Argentina'),
    ], default='ES')

    # Si modo_entrada = 'anual': cómo se reparte
    num_pagas = models.IntegerField(default=12,
        help_text="Reparto del anual en pagas.")
    meses_pagas_extras = models.CharField(max_length=50, blank=True, default='6,12',
        help_text="Meses de pagas extras separados por coma. Ej: 6,12 para junio y diciembre")

    # Si modo_entrada = 'periodo': periodicidad y meses
    periodicidad = models.CharField(max_length=20, choices=PERIODICIDAD_CHOICES, default='mensual')
    meses_cobro = models.CharField(max_length=50, blank=True, default='',
        help_text="Meses de cobro separados por coma. Ej: 3,6,9,12 para trimestral")

    # Variabilidad
    porcentaje_variabilidad = models.DecimalField(max_digits=5, decimal_places=2,
        default=Decimal('0'),
        help_text="% de variabilidad. Ej: 40 = +40% sobre la base.")

    incluir_en_mensual = models.BooleanField(default=True,
        help_text="Si False, solo aparece en el ponderado, no en el mensual base.")
    destino = models.ForeignKey(DestinoIngreso, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='fuentes',
        help_text="Destino para ingresos no recurrentes mensuales.")
    activo = models.BooleanField(default=True)
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['usuario', 'nombre']

    def __str__(self):
        return f"{self.nombre} ({self.usuario.username})"

    @property
    def importe_anual_bruto(self):
        """Total anual bruto: fuente de verdad calculada desde lo declarado."""
        if self.modo_entrada == 'anual':
            return self.importe_declarado

        multiplicadores = {
            'diaria': Decimal('365'),
            'semanal': Decimal('52'),
            'mensual': Decimal('12'),
            'trimestral': Decimal('4'),
            'semestral': Decimal('2'),
            'anual': Decimal('1'),
            'puntual': Decimal('1'),
        }
        return round(self.importe_declarado * multiplicadores.get(self.periodicidad, Decimal('1')), 2)

    @property
    def importe_anual_estimado(self):
        """Anual bruto ajustado por variabilidad."""
        base = self.importe_anual_bruto
        if self.porcentaje_variabilidad > 0:
            return round(base * (Decimal('1') + self.porcentaje_variabilidad / Decimal('100')), 2)
        return base


    @property
    def importe_mensual_base(self):
        """Lo que llega a tu cuenta cada mes (sin extras).
        Para modo anual: anual / num_pagas (es lo que cobras 12 veces).
        Para modo periodo mensual: el importe declarado.
        Para otros periodos: 0 (no es recurrente mensual)."""
        if not self.incluir_en_mensual:
            return Decimal('0')

        if self.modo_entrada == 'anual':
            # Cada mes llega anual / num_pagas
            return round(self.importe_declarado / Decimal(str(self.num_pagas)), 2)

        if self.periodicidad == 'mensual':
            return self.importe_declarado

        return Decimal('0')



    @property
    def importe_mensual_ponderado(self):
        """Anual estimado / 12. Reparte todo uniformemente incluidas extras."""
        return round(self.importe_anual_estimado / Decimal('12'), 2)


    @property
    def pagas_extras_meses(self):
        """Lista de meses de pagas extras."""
        if not self.meses_pagas_extras:
            return []
        try:
            return [int(m.strip()) for m in self.meses_pagas_extras.split(',') if m.strip()]
        except ValueError:
            return []

    @property
    def cobro_meses(self):
        """Lista de meses de cobro para ingresos por periodo."""
        if not self.meses_cobro:
            return []
        try:
            return [int(m.strip()) for m in self.meses_cobro.split(',') if m.strip()]
        except ValueError:
            return []

    @property
    def importe_paga_extra(self):
        """Importe de cada paga extra si num_pagas > 12."""
        if self.modo_entrada == 'anual' and self.num_pagas > 12:
            return round(self.importe_declarado / Decimal(str(self.num_pagas)), 2)
        return Decimal('0')

    @property
    def num_pagas_extras(self):
        """Cuántas pagas extras tiene."""
        if self.modo_entrada == 'anual':
            return max(0, self.num_pagas - 12)
        return 0

    @property
    def es_mensual_recurrente(self):
        """True si es un ingreso que llega cada mes."""
        if self.modo_entrada == 'anual':
            return True  # El salario siempre tiene parte mensual
        return self.periodicidad == 'mensual'
    
    @property
    def importe_neto_por_cobro(self):
        """Importe neto que recibes cada vez que cobras.
        Para pagos no mensuales, muestra cuánto recibes en cada pago."""
        if self.modo_entrada == 'anual':
            return round(self.importe_declarado / Decimal(str(self.num_pagas)), 2)
        return self.importe_declarado
        
        
### Modulo de Gastos ###

TIPO_GASTO_CHOICES = [
    ('fijo', 'Gasto Fijo'),
    ('anual', 'Gasto Anual (provision)'),
    ('variable', 'Gasto Variable'),
]

PERIODICIDAD_GASTO_CHOICES = [
    ('mensual', 'Mensual'),
    ('bimensual', 'Bimensual'),
    ('trimestral', 'Trimestral'),
    ('semestral', 'Semestral'),
    ('anual', 'Anual'),
]

class CategoriaGasto(models.Model):
    hogar = models.ForeignKey('core.Hogar', on_delete=models.CASCADE, related_name='categorias_gasto')
    nombre = models.CharField(max_length=100)
    tipo = models.CharField(max_length=20, choices=TIPO_GASTO_CHOICES)
    es_predefinida = models.BooleanField(default=False)
    activo = models.BooleanField(default=True)

    class Meta:
        ordering = ['tipo', 'nombre']
        unique_together = ('hogar', 'nombre')

    def __str__(self):
        return f"{self.nombre} ({self.get_tipo_display()})"


class PartidaGasto(models.Model):
    hogar = models.ForeignKey('core.Hogar', on_delete=models.CASCADE, related_name='partidas_gasto')
    categoria = models.ForeignKey(CategoriaGasto, on_delete=models.CASCADE, related_name='partidas')
    responsable = models.ForeignKey(User, on_delete=models.SET_NULL,
    null=True, blank=True, related_name='gastos_asignados',
    help_text="Si vacio, es un gasto compartido del hogar.")
    nombre = models.CharField(max_length=150)
    importe = models.DecimalField(max_digits=12, decimal_places=2,
        help_text="Importe por periodo declarado")
    periodicidad = models.CharField(max_length=20, choices=PERIODICIDAD_GASTO_CHOICES, default='mensual',
        help_text="Cada cuanto se paga este gasto")
    mes_pago = models.IntegerField(choices=MESES_CHOICES, null=True, blank=True,
        help_text="Para gastos no mensuales: mes principal de pago.")
    activo = models.BooleanField(default=True)
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['categoria', 'nombre']

    def __str__(self):
        return f"{self.nombre} - {self.importe}"

    @property
    def importe_mensual(self):
        """Impacto mensual real: convierte cualquier periodicidad a mensual."""
        divisores = {
            'mensual': Decimal('1'),
            'bimensual': Decimal('2'),
            'trimestral': Decimal('3'),
            'semestral': Decimal('6'),
            'anual': Decimal('12'),
        }
        divisor = divisores.get(self.periodicidad, Decimal('1'))
        return round(self.importe / divisor, 2)

    @property
    def importe_anual(self):
        """Coste anual total."""
        multiplicadores = {
            'mensual': Decimal('12'),
            'bimensual': Decimal('6'),
            'trimestral': Decimal('4'),
            'semestral': Decimal('2'),
            'anual': Decimal('1'),
        }
        mult = multiplicadores.get(self.periodicidad, Decimal('12'))
        return round(self.importe * mult, 2)
        
        ### Modulo de Distribucion y Ahorro ###

MODO_APORTACION_CHOICES = [
    ('igual', 'A partes iguales'),
    ('proporcional', 'Proporcional al ingreso'),
    ('fijo', 'Importe fijo por persona'),
]

TIPO_FONDO_CHOICES = [
    ('comun', 'Fondo común del hogar'),
    ('ahorro', 'Ahorro'),
    ('inversion', 'Inversión'),
    ('emergencia', 'Fondo de emergencia'),
    ('objetivo', 'Objetivo concreto'),
    ('otro', 'Otro'),
]

class FondoFamiliar(models.Model):
    """Fondos o sobres donde se destina el dinero libre."""
    hogar = models.ForeignKey('core.Hogar', on_delete=models.CASCADE, related_name='fondos')
    nombre = models.CharField(max_length=100,
        help_text="Ej: Fondo comun, Ahorro piso, Inversion, Emergencia...")
    tipo_fondo = models.CharField(
        max_length=20, choices=TIPO_FONDO_CHOICES, default='comun',
        help_text="Categoría del fondo para reporting (ahorro, inversión, etc.)",
    )
    modo_aportacion = models.CharField(max_length=20, choices=MODO_APORTACION_CHOICES, default='proporcional')
    color = models.CharField(max_length=7, default='#a259ff')
    cuenta_asociada = models.CharField(
        max_length=150, blank=True, default='',
        help_text="Nombre o descripcion de la cuenta bancaria asociada a este fondo. "
                  "Ej: Revolut conjunta, BBVA ahorro..."
    )
    orden = models.IntegerField(default=0)
    activo = models.BooleanField(default=True)

    class Meta:
        ordering = ['orden', 'nombre']
        unique_together = ('hogar', 'nombre')

    def __str__(self):
        return f"{self.nombre} ({self.get_tipo_fondo_display()})"

class ReglaReparto(models.Model):
    """Asignacion de dinero a un fondo: puede ser % o importe fijo."""
    TIPO_REGLA_CHOICES = [
        ('porcentaje', 'Porcentaje del dinero libre'),
        ('fijo', 'Importe fijo mensual'),
    ]

    hogar = models.ForeignKey('core.Hogar', on_delete=models.CASCADE, related_name='reglas_reparto')
    fondo = models.ForeignKey(FondoFamiliar, on_delete=models.CASCADE, related_name='reglas',
        null=True, blank=True,
        help_text="Fondo al que se destina. Si vacio, es dinero sin asignar a fondo.")
    nombre = models.CharField(max_length=100)
    tipo_regla = models.CharField(max_length=20, choices=TIPO_REGLA_CHOICES, default='porcentaje')
    PERIODICIDAD_REGLA_CHOICES = [
        ('mensual', 'Aplica cada mes sobre ingreso mensual'),
        ('anual', 'Aplica sobre ingresos anuales / pagas extras'),
    ]

    periodicidad_regla = models.CharField(
        max_length=10,
        choices=PERIODICIDAD_REGLA_CHOICES,
        default='mensual',
        help_text="Mensual: opera sobre el libre mensual. "
                  "Anual: opera sobre pagas extras e ingresos extraordinarios del mes.",
    )
    porcentaje = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'),
        help_text="Porcentaje del dinero libre (solo si tipo=porcentaje)")
    importe_fijo = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'),
        help_text="Importe fijo mensual (solo si tipo=fijo)")
        
    usuario = models.ForeignKey(
        'auth.User', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='reglas_reparto',
        help_text="Si se especifica, esta regla aplica solo a este miembro. "
                  "Si vacio, aplica al total del hogar."
    ) 
    color = models.CharField(max_length=7, default='#a259ff')
    orden = models.IntegerField(default=0)
    activo = models.BooleanField(default=True)

    class Meta:
        ordering = ['orden', 'nombre']

    def __str__(self):
        if self.tipo_regla == 'porcentaje':
            return f"{self.nombre} ({self.porcentaje}%)"
        return f"{self.nombre} ({self.importe_fijo} EUR/mes)"

# ─────────────────────────────────────────────────────────────────────────────
# AÑADIR AL FINAL DE finanzas/models.py (dentro del módulo de Distribución)
# ─────────────────────────────────────────────────────────────────────────────


class AjusteIngresoMensual(models.Model):
    """
    Override puntual del importe de una FuenteIngreso para un mes concreto.

    INMUTABILIDAD: nunca modifica FuenteIngreso.importe_declarado.
    Este registro es el apunte de corrección mensual. Si se borra,
    el motor de distribución vuelve al valor base de la fuente.

    Uso típico: Irene tiene guardias variables → cada mes se declara
    el importe real cobrado aquí antes de calcular la distribución.
    """
    fuente = models.ForeignKey(
        'FuenteIngreso',
        on_delete=models.CASCADE,
        related_name='ajustes_mensuales',
    )
    año = models.IntegerField()
    mes = models.IntegerField(help_text='Número de mes 1-12')
    importe_real = models.DecimalField(
        max_digits=12, decimal_places=2,
        help_text='Importe neto real cobrado este mes (ya neto, sin recalcular IRPF)',
    )
    nota = models.CharField(
        max_length=255, blank=True, default='',
        help_text="Ej: 'Solo 3 guardias este mes'",
    )
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-año', '-mes']
        unique_together = ('fuente', 'año', 'mes')

    def __str__(self):
        return f"{self.fuente.nombre} {self.mes}/{self.año}: {self.importe_real}€"


class SubsobreFondo(models.Model):
    """
    Partición interna de un FondoFamiliar en 'sobres' con presupuesto asignado.

    Permite responder: del dinero que entra al Fondo Común,
    ¿cuánto va a alimentación, cuánto a ocio, cuánto a suscripciones?

    El importe puede calcularse automáticamente desde PartidaGasto vinculadas
    (ej: todas las partidas tipo 'variable' del hogar) o declararse manualmente
    (ej: 200€/mes discrecionales).
    """
    TIPO_CHOICES = [
        ('gasto_fijo',     'Cubre gasto fijo del hogar'),
        ('gasto_variable', 'Cubre gasto variable del hogar'),
        ('discrecional',   'Gasto discrecional'),
        ('libre',          'Sin asignación / libre'),
    ]

    fondo = models.ForeignKey(
        'FondoFamiliar',
        on_delete=models.CASCADE,
        related_name='subsobres',
    )
    nombre = models.CharField(
        max_length=100,
        help_text='Ej: Ocio, Restaurantes, Alimentación, Ropa, Suscripciones...',
    )
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES, default='discrecional')

    # Fuente del importe: vinculado a partidas O manual
    partidas_vinculadas = models.ManyToManyField(
        'PartidaGasto',
        blank=True,
        help_text='Si vinculas partidas, el importe se calcula sumando su importe_mensual.',
    )
    importe_manual = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text='Importe mensual fijo si no hay partidas vinculadas.',
    )
    fondo_destino = models.ForeignKey(
        'FondoFamiliar',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='subsobres_entrantes',
        help_text="Si se indica, el importe de este sobre se transfiere a otro fondo "
                  "(ej: del Fondo Común → Ahorro Piso). Mutuamente excluyente con partidas.",
    )

    orden = models.IntegerField(default=0)
    activo = models.BooleanField(default=True)

    class Meta:
        ordering = ['fondo', 'orden', 'nombre']

    def __str__(self):
        return f"{self.fondo.nombre} → {self.nombre}"

    @property
    def importe_calculado(self):
        """
        Prioridad:
          1. Si tiene fondo_destino → usa importe_manual (es una transferencia)
          2. Si tiene partidas vinculadas → suma sus importe_mensual
          3. Si no → importe_manual o 0
        """
        if self.fondo_destino_id:
            return self.importe_manual or Decimal('0')
        partidas = self.partidas_vinculadas.filter(activo=True)
        if partidas.exists():
            return sum(p.importe_mensual for p in partidas)
        return self.importe_manual or Decimal('0')
        
        
 # ─────────────────────────────────────────────────────────────────────────────
# AÑADIR AL FINAL DE finanzas/models.py
# (Si ya pegaste el addon anterior con AjusteIngresoMensual y SubsobreFondo,
#  solo añade IngresoExtraordinario -- el campo tipo_fondo se añade via migración)
# ─────────────────────────────────────────────────────────────────────────────


class IngresoExtraordinario(models.Model):
    """
    Ingreso puntual fuera del ciclo normal de FuenteIngreso.

    Cubre: bonus, devolución Hacienda, herencia, venta de algo,
    ingreso freelance puntual, regalo, etc.

    El usuario declara cuánto recibió, en qué mes, y opcionalmente
    lo asigna a un fondo (ahorro, inversión, fondo común...).
    """
    hogar = models.ForeignKey(
        'core.Hogar', on_delete=models.CASCADE,
        related_name='ingresos_extraordinarios',
    )
    usuario = models.ForeignKey(
        User, on_delete=models.CASCADE,
        related_name='ingresos_extraordinarios',
    )
    concepto = models.CharField(
        max_length=200,
        help_text="Ej: Bonus anual, Devolución IRPF, Venta coche...",
    )
    importe = models.DecimalField(max_digits=12, decimal_places=2)
    es_neto = models.BooleanField(
        default=True,
        help_text="True = ya neto, False = bruto (se aplicarán retenciones)",
    )
    año = models.IntegerField()
    mes = models.IntegerField(help_text='Mes en que se recibe (1-12)')

    # Destino opcional
    fondo_destino = models.ForeignKey(
        'FondoFamiliar', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='ingresos_extraordinarios',
        help_text="Si se asigna, este ingreso alimenta directamente el fondo.",
    )

    nota = models.CharField(max_length=255, blank=True, default='')
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-año', '-mes', '-creado_en']

    def __str__(self):
        return f"{self.concepto} ({self.mes}/{self.año}) -- €{self.importe}"
