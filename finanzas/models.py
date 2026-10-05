from django.db import models
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from decimal import Decimal
from datetime import date
### Modulo general de finanzas ###

class RegistroMensual(models.Model):
    usuario = models.ForeignKey(User, on_delete=models.CASCADE)
    anio = models.IntegerField()
    mes = models.IntegerField()

    # Activos
    total_inversiones = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_vehiculos = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    # Pasivos
    total_hipotecas = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_creditos = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_prestamos = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        unique_together = ('usuario', 'anio', 'mes')

    @property
    def total_liquido(self):
        from finanzas.models import SaldoMensualCuenta
        saldos = SaldoMensualCuenta.objects.filter(registro=self)
        return sum(s.saldo for s in saldos)

    @property
    def total_deuda_tarjetas(self):
        from finanzas.models import SaldoMensualTarjeta
        saldos = SaldoMensualTarjeta.objects.filter(registro=self)
        return sum(s.saldo for s in saldos)

    @property
    def patrimonio_total(self):
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
    ('DEPOSITO', 'Depósito'),
    ('OTRO', 'Otro'),
]

from .depositos import FRECUENCIA_CHOICES as DEPOSITO_FRECUENCIA_CHOICES  # noqa: E402


class GrupoInversion(models.Model):
    """Cartera de inversión: agrupación lógica de activos definida por el usuario,
    independiente del fondo del hogar y de la plataforma de compra (Revolut, Uptevia...).
    Permite ver la rentabilidad agregada de una selección de activos como una cartera."""
    usuario = models.ForeignKey(User, on_delete=models.CASCADE, related_name='carteras')
    nombre = models.CharField(max_length=100)
    color = models.CharField(max_length=7, default='#2d6a4f')
    orden = models.IntegerField(default=0)
    fecha_creacion = models.DateField(auto_now_add=True)

    class Meta:
        ordering = ['orden', 'nombre']
        unique_together = ('usuario', 'nombre')

    def __str__(self):
        return self.nombre

    # --- Rentabilidad agregada a NIVEL DE COMPRA ---
    # La cartera agrupa compras concretas (MovimientoInversion tipo COMPRA), con
    # independencia de la plataforma. Se valora sobre las unidades compradas al
    # precio actual del activo. No descuenta ventas (es una agrupación de compras).

    def _compras(self):
        return (
            MovimientoInversion.objects
            .filter(grupo=self, tipo=MovimientoInversion.COMPRA)
            .select_related('inversion', 'inversion__valor_actual')
        )

    @property
    def total_aportado(self):
        total = Decimal('0')
        for m in self._compras():
            total += (m.cantidad * m.precio_unitario) + (m.comision or Decimal('0'))
        return total

    @property
    def valor_cartera(self):
        total = Decimal('0')
        for m in self._compras():
            try:
                precio = m.inversion.valor_actual.valor_unitario
            except AttributeError:
                precio = None
            if precio is not None:
                total += m.cantidad * precio
        return total

    @property
    def rentabilidad(self):
        aportado = self.total_aportado
        if not aportado or aportado <= 0:
            return None
        valor = self.valor_cartera or Decimal('0')
        return round(float((Decimal(str(valor)) - Decimal(str(aportado))) / Decimal(str(aportado)) * 100), 2)

    @property
    def num_activos(self):
        return self._compras().values('inversion_id').distinct().count()

    @property
    def num_compras(self):
        return self._compras().count()


def posicion_desde_movimientos(movimientos, valor_unitario=None):
    """Todas las cifras de un activo en UNA pasada sobre sus movimientos.

    Existe porque `Inversion` expone total_activos, valor_aportado,
    precio_medio_compra, coste_base_actual, ganancia_latente y
    ganancia_realizada como propiedades, y cada una lanza su propia consulta.
    Leerlas todas para un activo son seis consultas; una lista de veinte
    activos se iba a casi trescientas. Cuando ya tienes los movimientos en
    memoria —la lista los carga igualmente para pintar la tabla— esto da lo
    mismo sin tocar la base.

    Reproduce EXACTAMENTE lo que hacen esas propiedades hoy, incluido el
    criterio de precio medio: la media se calcula sobre TODAS las compras y no
    se reduce al vender. Ver la nota de `Inversion.precio_medio_compra`.

    `movimientos` debe venir ordenado por (fecha, id): la ganancia realizada usa
    el precio medio vigente en cada venta, y el orden cambia el resultado.
    """
    unidades_netas = Decimal('0')      # compradas − vendidas: lo que queda
    unidades_compradas = Decimal('0')  # solo compras, nunca se reduce
    invertido = Decimal('0')           # coste de esas compras
    realizada = Decimal('0')

    for m in movimientos:
        if m.tipo == 'COMPRA':
            invertido += m.cantidad * m.precio_unitario
            unidades_compradas += m.cantidad
            unidades_netas += m.cantidad
        elif m.tipo == 'VENTA':
            # Precio medio VIGENTE en el momento de la venta (AVCO histórico).
            pmc_venta = (round(invertido / unidades_compradas, 8)
                         if unidades_compradas > 0 else Decimal('0'))
            realizada += (m.precio_unitario - pmc_venta) * m.cantidad - m.comision
            unidades_netas -= m.cantidad

    pmc = (round(invertido / unidades_compradas, 8)
           if unidades_compradas > 0 else Decimal('0'))
    coste_base = round(unidades_netas * pmc, 2)
    valor_total = (round(Decimal(str(valor_unitario)) * unidades_netas, 2)
                   if valor_unitario is not None else 0)
    latente = valor_total - coste_base
    return {
        'total_activos': unidades_netas,
        'valor_aportado': invertido,
        'precio_medio_compra': pmc,
        'coste_base_actual': coste_base,
        'valor_total_actual': valor_total,
        'ganancia_latente': latente,
        'ganancia_realizada': round(realizada, 2),
        'rentabilidad_latente_pct': (round(float(latente / coste_base * 100), 2)
                                     if coste_base > 0 else None),
    }


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
    fondo = models.ForeignKey(
        'FondoFamiliar', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='inversiones',
        help_text="Fondo de inversión al que pertenece este activo.",
    )
    grupo = models.ForeignKey(
        'GrupoInversion', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='inversiones',
        help_text="Cartera de inversión a la que pertenece este activo.",
    )
    # ── Parámetros específicos de DEPÓSITO ──────────────────────────────────
    # Un depósito nunca forma parte de la cartera de mercado (acciones/ETF...);
    # vive en Inversiones en su propia sección y su valor cuenta como capital
    # (aparece automáticamente en Evolución).
    deposito_tipo_interes = models.DecimalField(
        max_digits=6, decimal_places=3, null=True, blank=True,
        help_text="Tipo de interés NOMINAL anual del depósito (%). Solo para depósitos.",
    )
    deposito_frecuencia = models.CharField(
        max_length=12, choices=DEPOSITO_FRECUENCIA_CHOICES, default='anual', blank=True,
        help_text="Cada cuánto capitaliza (paga) el interés el depósito.",
    )
    deposito_fecha_liquidacion = models.DateField(
        null=True, blank=True,
        help_text="Fecha de liquidación/vencimiento. Si se indica, el valor y el "
                   "interés se calculan hasta esa fecha (deja de acumular después).",
    )
    deposito_saldo_manual = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True,
        help_text="Saldo REAL de hoy del depósito. Si se indica, el valor actual y el "
                   "interés generado se calculan a partir de este saldo (en vez del "
                   "interés teórico), para cuadrar con lo que dice el banco.",
    )
    deposito_saldo_fecha = models.DateField(
        null=True, blank=True,
        help_text="Fecha a la que corresponde el saldo real indicado.",
    )
    fecha_creacion = models.DateField(auto_now_add=True)

    def __str__(self):
        return f"{self.nombre} ({self.ticker})"

    def clean(self):
        super().clean()
        if self.fondo_id and self.usuario_id:
            from core.models import UserProfile
            perfil = UserProfile.objects.filter(user=self.usuario).first()
            if perfil and self.fondo.hogar_id != getattr(perfil, 'hogar_id', None):
                raise ValidationError(
                    "La inversión debe pertenecer a un fondo del mismo hogar que el usuario."
                )

    @property
    def valor_total_actual(self):
        try:
            valor_unitario = self.valor_actual.valor_unitario
        except AttributeError:
            return 0
        # FUENTE ÚNICA: movimientos, no cantidad_actual
        return round(valor_unitario * self.total_activos, 2)

    def sincronizar_cantidad(self):
        """Mantiene cantidad_actual en sync con movimientos (para compatibilidad)."""
        from decimal import Decimal
        self.cantidad_actual = self.total_activos or Decimal('0')
        self.save(update_fields=['cantidad_actual'])


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

    
    @property
    def precio_medio_compra(self):
        """Precio medio ponderado de todas las compras."""
        result = self.movimientos.filter(tipo='COMPRA').aggregate(
            total_invertido=models.Sum(models.F('cantidad') * models.F('precio_unitario')),
            total_unidades=models.Sum('cantidad')
        )
        total_unidades = result['total_unidades'] or Decimal('0')
        total_invertido = result['total_invertido'] or Decimal('0')
        if total_unidades > 0:
            return round(total_invertido / total_unidades, 8)
        return Decimal('0')

    @property
    def coste_base_actual(self):
        """Coste de adquisición de las unidades que aún tienes."""
        return round(self.total_activos * self.precio_medio_compra, 2)

    @property
    def ganancia_latente(self):
        """Valor mercado actual - coste base de la posición abierta."""
        return self.valor_total_actual - self.coste_base_actual

    @property
    def rentabilidad_latente_pct(self):
        """% de rentabilidad sobre la posición abierta actualmente."""
        base = self.coste_base_actual
        if base > 0:
            return round(float((self.valor_total_actual - base) / base * 100), 2)
        return None

    @property
    def ganancia_realizada(self):
        """
        Ganancia/pérdida ya materializada con ventas.
        Usa el PMC vigente en la fecha de cada venta (AVCO histórico).
        """
        movimientos = list(self.movimientos.filter(
            tipo__in=['COMPRA', 'VENTA']
        ).order_by('fecha', 'id'))
        total_invertido = Decimal('0')
        total_unidades = Decimal('0')
        ganancia = Decimal('0')
        for m in movimientos:
            if m.tipo == 'COMPRA':
                total_invertido += m.cantidad * m.precio_unitario
                total_unidades += m.cantidad
            else:
                pmc = round(total_invertido / total_unidades, 8) if total_unidades > 0 else Decimal('0')
                ganancia += (m.precio_unitario - pmc) * m.cantidad - m.comision
        return round(ganancia, 2)

    # ── Motor de depósito ───────────────────────────────────────────────────
    # Para tipo=DEPOSITO, el valor no es unidades×precio sino el capital
    # compuesto al tipo de interés, teniendo en cuenta aportaciones (COMPRA) y
    # retiradas (VENTA) fechadas. La primera aportación es la apertura.

    def _flujos_deposito(self):
        """Flujos con signo para el motor de interés compuesto (COMPRA +, VENTA -)."""
        flujos = []
        for m in self.movimientos.all():
            if m.tipo == 'COMPRA':
                flujos.append((m.fecha, m.cantidad))
            elif m.tipo == 'VENTA':
                flujos.append((m.fecha, -m.cantidad))
        return flujos

    def _sumas_deposito(self, hasta):
        """(aportado_bruto, retirado_bruto) hasta la fecha `hasta`."""
        aportado = Decimal('0')
        retirado = Decimal('0')
        for m in self.movimientos.all():
            if m.fecha > hasta:
                continue
            if m.tipo == 'COMPRA':
                aportado += m.cantidad
            elif m.tipo == 'VENTA':
                retirado += m.cantidad
        return aportado, retirado

    def deposito_estado(self, hasta=None):
        """Estado del depósito a la fecha `hasta` (por defecto: liquidación u hoy).

        Devuelve dict con:
          - valor:    saldo que queda en el depósito (con intereses).
          - aportado: dinero total ingresado (aportaciones).
          - retirado: dinero total retirado.
          - interes:  rendimiento generado = valor + retirado − aportado.
                      Es lo que has ganado, con independencia de que lo hayas
                      sacado o siga dentro: si metes 6.000, genera 66 y lo
                      retiras todo, el saldo queda en 0 y el interés en +66.
          - interes_pct: ese rendimiento en % sobre lo aportado.

        Si hay un saldo REAL indicado, se usa como foto en su fecha (anclaje) y
        a partir de ahí se siguen aplicando retiradas/aportaciones e intereses.
        """
        from .depositos import valor_a_fecha, PERIODOS_ANO

        hoy_ref = self.deposito_fecha_liquidacion or date.today()
        if hasta is None:
            hasta = hoy_ref
        elif self.deposito_fecha_liquidacion and hasta > self.deposito_fecha_liquidacion:
            hasta = self.deposito_fecha_liquidacion

        aportado, retirado = self._sumas_deposito(hasta)

        r = (self.deposito_tipo_interes or Decimal('0')) / Decimal('100')
        m = PERIODOS_ANO.get(self.deposito_frecuencia or 'anual', 1)

        # El saldo real solo ancla el cálculo a partir de SU fecha.
        anclaje = None
        saldo_fecha = self.deposito_saldo_fecha or hoy_ref
        if self.deposito_saldo_manual is not None and hasta >= saldo_fecha:
            anclaje = (saldo_fecha, self.deposito_saldo_manual)

        valor_calc, _ = valor_a_fecha(self._flujos_deposito(), r, m, hasta, anclaje=anclaje)
        # Un depósito no puede quedar en negativo por redondeos de la retirada.
        valor = max(Decimal('0'), round(valor_calc, 2))

        interes = round(valor + retirado - aportado, 2)
        interes_pct = (round(interes / aportado * 100, 2)
                       if aportado and aportado > 0 else None)
        return {'valor': valor, 'aportado': round(aportado, 2),
                'retirado': round(retirado, 2), 'interes': interes,
                'interes_pct': interes_pct, 'hasta': hasta}

    # Compat: (valor, aportado) — aportado = neto (aportado − retirado) para el
    # "dinero que queda ingresado". El desglose completo está en deposito_estado.
    def deposito_valor_y_aportado(self, hasta=None):
        e = self.deposito_estado(hasta)
        return e['valor'], round(e['aportado'] - e['retirado'], 2)

    @property
    def deposito_valor_actual(self):
        return self.deposito_estado()['valor']

    @property
    def deposito_aportado(self):
        return self.deposito_estado()['aportado']

    @property
    def deposito_interes(self):
        return self.deposito_estado()['interes']

    @property
    def deposito_fecha_apertura(self):
        flujos = sorted(self._flujos_deposito(), key=lambda x: x[0])
        return flujos[0][0] if flujos else None

    def deposito_interes_anio(self, anio):
        """Interés generado (devengado) durante el ejercicio `anio`: variación
        del patrimonio realizado (valor + retirado acumulado) descontando las
        aportaciones del año. Base orientativa del rendimiento a declarar.

        Para el ejercicio en curso se corta a HOY: se declara lo devengado
        hasta la fecha, no una proyección a 31 de diciembre."""
        fin = min(date(anio, 12, 31), date.today())
        ini_prev = date(anio - 1, 12, 31)
        e_fin = self.deposito_estado(hasta=fin)
        e_ini = self.deposito_estado(hasta=ini_prev)
        patr_fin = e_fin['valor'] + e_fin['retirado']
        patr_ini = e_ini['valor'] + e_ini['retirado']
        aport_anio = sum(
            (m.cantidad for m in self.movimientos.all()
             if m.tipo == 'COMPRA' and m.fecha.year == anio and m.fecha <= fin),
            Decimal('0'),
        )
        return round(patr_fin - patr_ini - aport_anio, 2)


class AportacionRecurrente(models.Model):
    """Regla de aportación periódica (p. ej. mensual) a un activo — pensada
    sobre todo para depósitos/planes con aportaciones regulares. No crea nada
    automáticamente: calcula qué meses están pendientes de registrar y el
    usuario decide cuándo generarlos (control total, sin tareas en segundo
    plano)."""
    inversion = models.ForeignKey(
        Inversion, on_delete=models.CASCADE, related_name='aportaciones_recurrentes',
    )
    importe = models.DecimalField(max_digits=12, decimal_places=2)
    dia_mes = models.IntegerField(
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(28)],
        help_text="Día del mes de la aportación (1-28, para que exista en todos los meses).",
    )
    fecha_inicio = models.DateField(help_text="Mes de la primera aportación.")
    fecha_fin = models.DateField(
        null=True, blank=True,
        help_text="Último mes de aportación. Vacío = sigue indefinidamente (hasta hoy).",
    )
    activa = models.BooleanField(default=True)
    fecha_creacion = models.DateField(auto_now_add=True)

    class Meta:
        ordering = ['-activa', '-fecha_inicio']

    def __str__(self):
        return f"{self.importe}€/mes ({self.dia_mes}) — {self.inversion.nombre}"

    def meses_pendientes(self, hasta=None):
        """Lista de (año, mes) entre fecha_inicio y el límite (fecha_fin u hoy)
        que todavía no tienen un movimiento generado por esta regla."""
        hasta = hasta or date.today()
        limite = min(self.fecha_fin, hasta) if self.fecha_fin else hasta
        if (self.fecha_inicio.year, self.fecha_inicio.month) > (limite.year, limite.month):
            return []

        generados = set(
            self.movimientos_generados.values_list('fecha__year', 'fecha__month')
        )
        pendientes = []
        anio, mes = self.fecha_inicio.year, self.fecha_inicio.month
        while (anio, mes) <= (limite.year, limite.month):
            if (anio, mes) not in generados:
                pendientes.append((anio, mes))
            mes += 1
            if mes > 12:
                mes = 1
                anio += 1
        return pendientes


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
    cantidad = models.DecimalField(max_digits=20, decimal_places=8)
    precio_unitario = models.DecimalField(max_digits=20, decimal_places=8)
    comision = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    grupo = models.ForeignKey(
        'GrupoInversion', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='movimientos_compra',
        help_text="Cartera de inversión a la que pertenece esta compra.",
    )
    origen_recurrente = models.ForeignKey(
        'AportacionRecurrente', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='movimientos_generados',
        help_text="Regla de aportación recurrente que generó este movimiento (si aplica).",
    )

    def valor_total(self):
        return (self.cantidad * self.precio_unitario) + self.comision

    def clean(self):
        if not self.inversion_id:
            return
        if self.tipo == self.VENTA:
            total_comprado = MovimientoInversion.objects.filter(
                inversion=self.inversion, tipo=self.COMPRA
            ).aggregate(models.Sum('cantidad'))['cantidad__sum'] or 0
            total_vendido = MovimientoInversion.objects.filter(
                inversion=self.inversion, tipo=self.VENTA
            ).exclude(id=self.id).aggregate(models.Sum('cantidad'))['cantidad__sum'] or 0
            if self.cantidad > (total_comprado - total_vendido):
                raise ValidationError("No se pueden vender más unidades de las que se han comprado.")


class ValorActualInversion(models.Model):
    inversion = models.OneToOneField(Inversion, on_delete=models.CASCADE, related_name='valor_actual')
    valor_unitario = models.DecimalField(max_digits=20, decimal_places=8)
    fecha_actualizacion = models.DateTimeField(auto_now=True)
    fuente = models.CharField(max_length=100, blank=True, null=True)

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
    cantidad_activos = models.DecimalField(max_digits=20, decimal_places=8,
        help_text="Cantidad de activos en esa fecha")
    fecha = models.DateField()
    fuente = models.CharField(max_length=100, blank=True, null=True)

    class Meta:
        unique_together = ('inversion', 'fecha')

    def __str__(self):
        return f"{self.inversion} @ {self.fecha} → {self.valor_unitario} x {self.cantidad_activos}"


class PrecioHistorico(models.Model):
    """Cierre diario de un ticker, descargado de Yahoo Finance.

    Es una caché: el histórico de cotizaciones no cambia, así que se descarga
    una vez y la gráfica de evolución de la cartera lo lee de aquí. Va por
    ticker (no por inversión) porque el mismo valor puede estar en varias
    posiciones (p. ej. SU.PA en Revolut y en Uptevia).
    """
    ticker = models.CharField(max_length=20, db_index=True)
    fecha = models.DateField()
    cierre = models.DecimalField(max_digits=20, decimal_places=8)

    class Meta:
        unique_together = ('ticker', 'fecha')
        ordering = ['ticker', 'fecha']

    def __str__(self):
        return f"{self.ticker} @ {self.fecha} → {self.cierre}"


### Módulo de Ingresos ###

class TablaIRPF(models.Model):
    pais = models.CharField(max_length=5, choices=[
        ('ES', 'España'), ('US', 'Estados Unidos'), ('UK', 'Reino Unido'),
        ('FR', 'Francia'), ('DE', 'Alemania'), ('PT', 'Portugal'),
        ('IT', 'Italia'), ('MX', 'México'), ('CO', 'Colombia'), ('AR', 'Argentina'),
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
    pais = models.CharField(max_length=5, choices=[('ES', 'España')], default='ES')
    concepto = models.CharField(max_length=100,
        help_text="Ej: Contingencias comunes, Desempleo, Formación...")
    porcentaje_trabajador = models.DecimalField(max_digits=5, decimal_places=2)
    año = models.IntegerField()

    class Meta:
        ordering = ['pais', 'año', 'concepto']

    def __str__(self):
        return f"{self.pais} {self.año}: {self.concepto} → {self.porcentaje_trabajador}%"


class DestinoIngreso(models.Model):
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
    ('diaria', 'Diaria'), ('semanal', 'Semanal'), ('mensual', 'Mensual'),
    ('trimestral', 'Trimestral'), ('semestral', 'Semestral'),
    ('anual', 'Anual'), ('puntual', 'Puntual'),
]

MESES_CHOICES = [
    (1, 'Enero'), (2, 'Febrero'), (3, 'Marzo'), (4, 'Abril'),
    (5, 'Mayo'), (6, 'Junio'), (7, 'Julio'), (8, 'Agosto'),
    (9, 'Septiembre'), (10, 'Octubre'), (11, 'Noviembre'), (12, 'Diciembre'),
]


class ImputableAActivo(models.Model):
    """Gasto que pertenece a un activo concreto: esta casa, este coche.

    Es lo que permite responder «¿cuánto me cuesta tener el coche?», que la
    categoría sola no contesta: el seguro, la ITV y la gasolina caen en bloques
    distintos del presupuesto y, con dos coches, ni siquiera se distinguen entre
    sí.

    Son dos claves foráneas y no una relación genérica porque el activo se
    consulta y se agrega constantemente (totales por vehículo, por propiedad) y
    las genéricas no dejan hacer `select_related` ni agregados directos. A
    cambio, un tercer tipo de activo exigiría un tercer campo; cuando llegue,
    ese será el momento de generalizar, no antes.
    """

    propiedad = models.ForeignKey(
        'finanzas.Propiedad', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='%(class)s_imputados',
        help_text='Propiedad a la que pertenece este gasto.',
    )
    vehiculo = models.ForeignKey(
        'finanzas.Vehiculo', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='%(class)s_imputados',
        help_text='Vehículo al que pertenece este gasto.',
    )

    class Meta:
        abstract = True

    @property
    def activo_imputado(self):
        """El activo al que se imputa, sea del tipo que sea (o None)."""
        return self.vehiculo or self.propiedad

    @property
    def clave_activo(self):
        """Identificador único para los selectores: «vehiculo:3», «propiedad:1»."""
        if self.vehiculo_id:
            return f'vehiculo:{self.vehiculo_id}'
        if self.propiedad_id:
            return f'propiedad:{self.propiedad_id}'
        return ''


class FuenteIngreso(ImputableAActivo):
    """Una fuente de ingreso del hogar.

    Puede imputarse a un activo (el alquiler ES de ese piso): así la ficha de
    la propiedad no solo dice lo que cuesta, sino lo que deja."""


    TIPO_CHOICES = [('fijo', 'Fijo'), ('variable', 'Variable estimado')]
    MODO_ENTRADA_CHOICES = [('anual', 'Declaro el total anual'), ('periodo', 'Declaro por periodo')]
    REPARTO_CHOICES = [(12, '12 pagas'), (14, '14 pagas (extras en junio y diciembre)'), (15, '15 pagas')]

    usuario = models.ForeignKey(User, on_delete=models.CASCADE, related_name='fuentes_ingreso')
    hogar = models.ForeignKey('core.Hogar', on_delete=models.CASCADE, related_name='fuentes_ingreso')
    nombre = models.CharField(max_length=150,
        help_text="Ej: Schneider Electric, Guardias hospital, Alquiler piso...")
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES, default='fijo')
    modo_entrada = models.CharField(max_length=10, choices=MODO_ENTRADA_CHOICES, default='anual')
    importe_declarado = models.DecimalField(max_digits=12, decimal_places=2,
        help_text="Importe tal como lo introduce el usuario (anual o por periodo)")
    es_bruto = models.BooleanField(default=True)
    pais_fiscal = models.CharField(max_length=5, choices=[
        ('ES', 'España'), ('US', 'Estados Unidos'), ('UK', 'Reino Unido'),
        ('FR', 'Francia'), ('DE', 'Alemania'), ('PT', 'Portugal'),
        ('IT', 'Italia'), ('MX', 'México'), ('CO', 'Colombia'), ('AR', 'Argentina'),
    ], default='ES')
    num_pagas = models.IntegerField(default=12, help_text="Reparto del anual en pagas.")
    meses_pagas_extras = models.CharField(max_length=50, blank=True, default='6,12',
        help_text="Meses de pagas extras separados por coma. Ej: 6,12 para junio y diciembre")
    periodicidad = models.CharField(max_length=20, choices=PERIODICIDAD_CHOICES, default='mensual')
    meses_cobro = models.CharField(max_length=50, blank=True, default='',
        help_text="Meses de cobro separados por coma. Ej: 3,6,9,12 para trimestral")
    porcentaje_variabilidad = models.DecimalField(max_digits=5, decimal_places=2,
        default=Decimal('0'), help_text="% de variabilidad. Ej: 40 = +40% sobre la base.")
    incluir_en_mensual = models.BooleanField(default=True,
        help_text="Si False, solo aparece en el ponderado, no en el mensual base.")
    incluir_en_distribucion = models.BooleanField(default=True,
        help_text="Si se desmarca, el ingreso sigue declarado (cuenta para el total "
                  "anual y para el IRPF) pero NO entra en el reparto mensual del "
                  "hogar: ni en el dinero a distribuir, ni en la proporción con la "
                  "que se cubren los gastos comunes. Para ingresos que existen pero "
                  "se gestionan aparte, como el alquiler de un piso.")
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
        if self.modo_entrada == 'anual':
            return self.importe_declarado
        multiplicadores = {
            'diaria': Decimal('365'), 'semanal': Decimal('52'), 'mensual': Decimal('12'),
            'trimestral': Decimal('4'), 'semestral': Decimal('2'),
            'anual': Decimal('1'), 'puntual': Decimal('1'),
        }
        return round(self.importe_declarado * multiplicadores.get(self.periodicidad, Decimal('1')), 2)

    @property
    def importe_anual_estimado(self):
        base = self.importe_anual_bruto
        if self.porcentaje_variabilidad > 0:
            return round(base * (Decimal('1') + self.porcentaje_variabilidad / Decimal('100')), 2)
        return base

    @property
    def importe_mensual_base(self):
        if not self.incluir_en_mensual:
            return Decimal('0')
        if self.modo_entrada == 'anual':
            return round(self.importe_declarado / Decimal(str(self.num_pagas)), 2)
        if self.periodicidad == 'mensual':
            return self.importe_declarado
        return Decimal('0')

    @property
    def importe_mensual_ponderado(self):
        return round(self.importe_anual_estimado / Decimal('12'), 2)

    @property
    def pagas_extras_meses(self):
        if not self.meses_pagas_extras:
            return []
        try:
            return [int(m.strip()) for m in self.meses_pagas_extras.split(',') if m.strip()]
        except ValueError:
            return []

    @property
    def cobro_meses(self):
        if not self.meses_cobro:
            return []
        try:
            return [int(m.strip()) for m in self.meses_cobro.split(',') if m.strip()]
        except ValueError:
            return []

    @property
    def importe_paga_extra(self):
        if self.modo_entrada == 'anual' and self.num_pagas > 12:
            return round(self.importe_declarado / Decimal(str(self.num_pagas)), 2)
        return Decimal('0')

    @property
    def num_pagas_extras(self):
        if self.modo_entrada == 'anual':
            return max(0, self.num_pagas - 12)
        return 0

    @property
    def es_mensual_recurrente(self):
        if self.modo_entrada == 'anual':
            return True
        return self.periodicidad == 'mensual'

    @property
    def importe_neto_por_cobro(self):
        if self.modo_entrada == 'anual':
            return round(self.importe_declarado / Decimal(str(self.num_pagas)), 2)
        return self.importe_declarado


### Modulo de Gastos ###

# El `tipo` de una categoría es su AGRUPACIÓN SUPERIOR: es lo que agrupa el
# presupuesto y contra lo que se compara el gasto real observado en los
# extractos. La categoría concreta (Alimentacion, Ocio...) se conserva siempre;
# el tipo solo dice en qué bloque del presupuesto cae.
TIPO_GASTO_CHOICES = [
    ('fijo', 'Gasto Fijo'),
    ('anual', 'Gasto Anual (provision)'),
    ('variable', 'Gasto Variable'),
    ('discrecional', 'Gasto Discrecional'),
    ('ingreso', 'Ingreso'),
    ('traspaso', 'Traspaso entre cuentas'),
]

# Orden de presentación (el alfabético de la clave no sirve: 'anual' iría antes
# que 'fijo'). Se usa para ordenar bloques y selectores en toda la app.
ORDEN_TIPOS = ['fijo', 'anual', 'variable', 'discrecional', 'ingreso', 'traspaso']

# Los cuatro bloques que forman el presupuesto de GASTO. Ingresos y traspasos
# tienen categorías propias pero no son gasto y no entran en esa comparación.
TIPOS_GASTO = ('fijo', 'anual', 'variable', 'discrecional')

ETIQUETAS_TIPO = {
    'fijo': 'Fijos',
    'anual': 'Fijos anuales',
    'variable': 'Variables',
    'discrecional': 'Discrecionales',
    'ingreso': 'Ingresos',
    'traspaso': 'Traspasos',
}

# Mientras el `tipo` dice en qué BLOQUE DEL PRESUPUESTO cae una categoría, el
# `computo` dice CÓMO ENTRA EN LOS TOTALES del análisis de movimientos: si lo
# que cae en ella se cuenta como gasto, como ingreso o no se cuenta.
#
# Sin esto, el análisis solo miraba el signo del importe, así que un traspaso
# entre cuentas propias (o cualquier movimiento que no es gasto real: un
# reintegro, el pago de la tarjeta...) engordaba el gasto del mes.
COMPUTO_RESTA = 'resta'
COMPUTO_SUMA = 'suma'
COMPUTO_NEUTRO = 'neutro'

COMPUTO_CHOICES = [
    (COMPUTO_RESTA, 'Resta (cuenta como gasto)'),
    (COMPUTO_SUMA, 'Suma (cuenta como ingreso)'),
    (COMPUTO_NEUTRO, 'Neutra (no cuenta ni como gasto ni como ingreso)'),
]

ETIQUETAS_COMPUTO = {
    COMPUTO_RESTA: 'Resta',
    COMPUTO_SUMA: 'Suma',
    COMPUTO_NEUTRO: 'Neutra',
}

# Cómputo que le corresponde a cada bloque mientras el usuario no diga otra
# cosa. Es solo el valor de partida: el cómputo es editable por categoría.
COMPUTO_POR_TIPO = {
    'fijo': COMPUTO_RESTA,
    'anual': COMPUTO_RESTA,
    'variable': COMPUTO_RESTA,
    'discrecional': COMPUTO_RESTA,
    'ingreso': COMPUTO_SUMA,
    'traspaso': COMPUTO_NEUTRO,
}


def computo_por_defecto(tipo):
    return COMPUTO_POR_TIPO.get(tipo, COMPUTO_RESTA)

# Cada cuánto se paga un gasto, y a cuántos meses hay que repartirlo. Hay cosas
# que duran varios años —unos neumáticos, una caldera— y presupuestarlas «al
# año» obliga a inventarse una cifra: lo honesto es decir lo que cuestan y cada
# cuánto toca, y que el programa saque la cuota.
MESES_POR_PERIODICIDAD = {
    'mensual': 1, 'bimensual': 2, 'trimestral': 3, 'semestral': 6,
    'anual': 12, 'bienal': 24, 'trienal': 36, 'quinquenal': 60,
}

PERIODICIDAD_GASTO_CHOICES = [
    ('mensual', 'Mensual'), ('bimensual', 'Bimensual'),
    ('trimestral', 'Trimestral'), ('semestral', 'Semestral'), ('anual', 'Anual'),
    ('bienal', 'Cada 2 años'), ('trienal', 'Cada 3 años'),
    ('quinquenal', 'Cada 5 años'),
    # La lista de arriba cubre lo corriente, pero la vida útil de una cosa no
    # viene en años redondos: unos neumáticos duran 36 meses en un coche y 50
    # en otro que hace menos kilómetros. Con esta opción se escribe el número.
    ('personalizada', 'Cada N meses…'),
    # Un gasto que pasa UNA vez y se sabe con antelación —la boda de abril, el
    # calentador que hay que cambiar—. No es presupuesto de gasto: es dinero
    # que se aparta desde ahora hasta el mes del pago, en la reserva de los
    # fijos anuales. Ver `PartidaGasto.es_puntual`.
    ('puntual', 'Puntual (una sola vez)'),
]

# Tope de la periodicidad personalizada: cincuenta años. No es una limitación
# real de nada, solo evita que un dedazo (500 en vez de 50) deje una cuota de
# céntimos que no se entiende de dónde sale.
MAXIMO_MESES_PERIODO = 600


def normalizar_periodicidad(periodicidad, meses):
    """Devuelve `(periodicidad, meses_personalizados)` ya en limpio.

    Una periodicidad escrita a mano que coincide con una de las de siempre se
    guarda con SU nombre: así la pantalla dice «Anual» y no «Cada 12 meses», y
    —más importante— los doce sitios que preguntan `periodicidad != 'mensual'`
    para sacar los pagos anuales del mes siguen funcionando cuando alguien
    escribe un 1.
    """
    if periodicidad != 'personalizada':
        return periodicidad, None
    try:
        meses = int(meses)
    except (TypeError, ValueError):
        meses = 1
    meses = max(1, min(meses, MAXIMO_MESES_PERIODO))
    for nombre, cuantos in MESES_POR_PERIODICIDAD.items():
        if cuantos == meses:
            return nombre, None
    return 'personalizada', meses


class CategoriaPredefinidaDescartada(models.Model):
    """Categoría de fábrica que este hogar ha eliminado a conciencia.

    Las predefinidas se recrean en cada visita (así los hogares antiguos van
    recibiendo las nuevas), lo que hacía imposible borrar una: reaparecía sola
    en la siguiente pantalla. Guardar el nombre descartado es lo que hace que
    «eliminar» signifique eliminar. Vuelve a crearla a mano y la lápida se
    retira."""

    hogar = models.ForeignKey(
        'core.Hogar', on_delete=models.CASCADE, related_name='categorias_descartadas',
    )
    nombre = models.CharField(max_length=100)
    descartada_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('hogar', 'nombre')
        verbose_name = 'Categoría predefinida descartada'
        verbose_name_plural = 'Categorías predefinidas descartadas'

    def __str__(self):
        return f"{self.nombre} (descartada)"


class CategoriaGasto(models.Model):
    hogar = models.ForeignKey('core.Hogar', on_delete=models.CASCADE, related_name='categorias_gasto')
    nombre = models.CharField(max_length=100)
    tipo = models.CharField(max_length=20, choices=TIPO_GASTO_CHOICES)
    computo = models.CharField(
        max_length=10, choices=COMPUTO_CHOICES, default=COMPUTO_RESTA,
        help_text='Cómo entran sus movimientos en los totales: restan (gasto), '
                  'suman (ingreso) o son neutros (ni una cosa ni la otra).',
    )
    es_predefinida = models.BooleanField(default=False)
    activo = models.BooleanField(default=True)

    class Meta:
        ordering = ['tipo', 'nombre']
        unique_together = ('hogar', 'nombre')

    def __str__(self):
        return f"{self.nombre} ({self.get_tipo_display()})"

    @property
    def es_gasto(self):
        return self.tipo in TIPOS_GASTO

    @property
    def es_neutra(self):
        """No entra en los totales: traspasos entre cuentas propias y
        cualquier otra categoría que el usuario haya marcado como neutra."""
        return self.computo == COMPUTO_NEUTRO

    @property
    def etiqueta_computo(self):
        return ETIQUETAS_COMPUTO.get(self.computo, self.computo)

    @property
    def orden_tipo(self):
        """Posición del bloque para ordenar (Fijos → Anuales → Variables → …)."""
        try:
            return ORDEN_TIPOS.index(self.tipo)
        except ValueError:
            return len(ORDEN_TIPOS)


class PartidaGasto(ImputableAActivo):
    """Un gasto declarado del presupuesto.

    Normalmente cuelga de una categoría («Luz», «Restaurantes»). Pero hay
    bloques —los discrecionales, sobre todo— donde el usuario sabe cuánto
    quiere gastar EN TOTAL y no en qué se va a repartir: ahí la partida cuelga
    del BLOQUE entero y `categoria` queda vacía. Antes, la única salida era
    inventarse una categoría cajón de sastre y darle todo el presupuesto, lo que
    dejaba esa categoría pareciendo que le sobraba dinero y al resto sin límite
    ninguno.
    """
    hogar = models.ForeignKey('core.Hogar', on_delete=models.CASCADE, related_name='partidas_gasto')
    categoria = models.ForeignKey(CategoriaGasto, on_delete=models.CASCADE, related_name='partidas',
        null=True, blank=True,
        help_text="Vacío si el presupuesto es del bloque entero y no de una categoría concreta.")
    bloque = models.CharField(max_length=20, choices=TIPO_GASTO_CHOICES, blank=True, default='',
        help_text="Bloque al que pertenece cuando no hay categoría. "
                  "Con categoría manda el bloque de la categoría.")
    responsable = models.ForeignKey(User, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='gastos_asignados',
        help_text="Si vacio, es un gasto compartido del hogar.")
    nombre = models.CharField(max_length=150)
    importe = models.DecimalField(max_digits=12, decimal_places=2,
        help_text="Importe por periodo declarado")
    periodicidad = models.CharField(max_length=20, choices=PERIODICIDAD_GASTO_CHOICES, default='mensual',
        help_text="Cada cuanto se paga este gasto")
    meses_personalizados = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text='Solo con periodicidad «Cada N meses»: cuántos meses dura o '
                  'cada cuántos toca pagarlo.')
    mes_pago = models.IntegerField(choices=MESES_CHOICES, null=True, blank=True,
        help_text="Para gastos no mensuales: mes principal de pago.")
    # Un anual que se paga en varias veces —el IBI fraccionado en junio y en
    # noviembre—: [{"mes": 6, "importe": "120.00"}, {"mes": 11, …}]. Vacío es
    # lo normal, un solo pago en `mes_pago`. Cuando hay plazos, `mes_pago`
    # guarda el primero, para que las pantallas que solo miran ese campo
    # sigan enseñando un mes con sentido.
    plazos = models.JSONField(default=list, blank=True,
        help_text="Anuales pagados en varias veces: mes e importe de cada plazo.")
    # Los que duran más de un año —unos neumáticos cada tres— no se pagan
    # «en octubre» sino en octubre DE UN AÑO. Con el año, `mes_pago` +
    # `anio_pago` es una fecha en la que tocó o tocará, y las demás salen
    # sumando el periodo hacia delante y hacia atrás.
    anio_pago = models.PositiveSmallIntegerField(null=True, blank=True,
        help_text="Solo gastos de más de un año: año en que toca (o tocó) un pago.")
    # Años en los que el usuario dijo «esto ya está pagado» aunque el dinero
    # no cuadre con lo declarado: el seguro vino 10 € más barato y no hay
    # nada más que pagar.
    anios_dados_por_pagados = models.JSONField(default=list, blank=True,
        help_text="Años en que esta partida se dio por pagada aunque no cuadre el importe.")
    # Solo los puntuales: el mes desde el que se empieza a apartar. La cuota
    # sale de repartir el importe entre los meses que van de aquí al del pago,
    # ambos incluidos; se fija al crearlo para que no cambie cada mes que pasa.
    ahorro_desde = models.DateField(null=True, blank=True,
        help_text="Gastos puntuales: mes desde el que se aparta dinero para él.")
    activo = models.BooleanField(default=True)
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fondo_asignado = models.ForeignKey(
        'FondoFamiliar', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='gastos_asignados', help_text='Fondo común que cubre este gasto')

    class Meta:
        ordering = ['categoria', 'nombre']

    def __str__(self):
        return f"{self.nombre} - {self.importe}"

    @property
    def plazos_de_pago(self):
        """`[(mes, importe)]` de un anual fraccionado, o vacío si se paga de una vez.

        Los importes se reescalan al importe declarado: si alguien sube el IBI
        de 240 a 260 desde Gastos, los plazos siguen repartiéndolo en la misma
        proporción en vez de sumar lo de antes. El último plazo se queda con
        el céntimo que sobre del redondeo, para que la suma cuadre siempre.
        """
        if self.meses_periodo != 12 or len(self.plazos or []) < 2:
            return []
        try:
            crudos = sorted((int(p['mes']), Decimal(str(p['importe']))) for p in self.plazos)
        except (KeyError, TypeError, ValueError, ArithmeticError):
            return []
        total = sum(i for _, i in crudos)
        if total <= 0:
            n = len(crudos)
            crudos = [(m, Decimal('1')) for m, _ in crudos]
            total = Decimal(n)
        salida, acumulado = [], Decimal('0')
        for k, (mes, importe) in enumerate(crudos):
            if k == len(crudos) - 1:
                parte = self.importe - acumulado
            else:
                parte = (self.importe * importe / total).quantize(Decimal('0.01'))
            acumulado += parte
            salida.append((mes, parte))
        return salida

    @property
    def es_puntual(self):
        """Un gasto de una sola vez, planificado con antelación.

        No entra en el presupuesto de gastos (no es algo que se gaste cada
        año), pero sí en lo que se aparta para los fijos anuales: de
        `ahorro_desde` al mes del pago, `importe_mensual` cada mes. Su pago,
        emparejado como «pago anual», se saca del mes como cualquier anual.
        """
        return self.periodicidad == 'puntual'

    def ahorra_en(self, anio, mes):
        """¿Se aparta dinero para este puntual en ese mes?"""
        if not self.es_puntual or not (self.mes_pago and self.anio_pago):
            return False
        inicio = self._inicio_ahorro()
        t = anio * 12 + mes - 1
        return inicio <= t <= self.anio_pago * 12 + self.mes_pago - 1

    def _inicio_ahorro(self):
        desde = self.ahorro_desde or (self.fecha_creacion.date() if self.fecha_creacion else date.today())
        return desde.year * 12 + desde.month - 1

    @property
    def es_plurianual(self):
        return not self.es_puntual and self.meses_periodo > 12

    def _ancla(self):
        """El mes absoluto (año·12 + mes-1) de un pago conocido, o None."""
        if not (self.es_plurianual and self.mes_pago and self.anio_pago):
            return None
        return self.anio_pago * 12 + self.mes_pago - 1

    def _desde(self, hoy=None):
        """El primer mes absoluto en el que puede tocar un pago.

        Una fecha que ya pasó es un pago que hubo: los anteriores se cuentan
        hacia atrás, para que los años pasados digan lo que tocaba. Una fecha
        FUTURA es «el próximo, en abril de 2029», y contar hacia atrás desde
        ella inventaba un pago en abril de 2026 que salía como atrasado.
        """
        ancla = self._ancla()
        if ancla is None:
            return None
        hoy = hoy or date.today()
        return ancla if ancla > hoy.year * 12 + hoy.month - 1 else None

    def vencimientos_en(self, anio, hoy=None):
        """Meses de `anio` en los que toca pagar un gasto de varios años."""
        if self.es_puntual:
            return [self.mes_pago] if self.mes_pago and self.anio_pago == anio else []
        ancla = self._ancla()
        if ancla is None:
            return []
        n = self.meses_periodo
        inicio, fin = anio * 12, anio * 12 + 11
        t = ancla + -(-(inicio - ancla) // n) * n   # el primero desde enero
        desde = self._desde(hoy)
        if desde is not None:
            t = max(t, desde)
        meses = []
        while t <= fin:
            meses.append(t % 12 + 1)
            t += n
        return meses

    def proximo_pago(self, hoy):
        """`(año, mes)` del siguiente pago de un gasto de varios años, desde el
        mes de `hoy` incluido; None si no se sabe cuándo toca."""
        ancla = self._ancla()
        if self.es_puntual:
            return (self.anio_pago, self.mes_pago) if self.mes_pago and self.anio_pago else None
        if ancla is None:
            return None
        ahora = hoy.year * 12 + hoy.month - 1
        t = ancla + -(-(ahora - ancla) // self.meses_periodo) * self.meses_periodo
        t = max(t, ancla) if ancla > ahora else t
        return t // 12, t % 12 + 1

    @property
    def meses_pago_display(self):
        """«Junio»; fraccionado, «Junio y noviembre»; de varios años, «Octubre de 2028»."""
        if (self.es_plurianual or self.es_puntual) and self.mes_pago:
            proximo = self.proximo_pago(date.today())
            if proximo:
                return f"{dict(MESES_CHOICES)[proximo[1]]} de {proximo[0]}"
        plazos = self.plazos_de_pago
        if not plazos:
            return self.get_mes_pago_display() if self.mes_pago else ''
        nombres = [dict(MESES_CHOICES)[m].lower() for m, _ in plazos]
        texto = ', '.join(nombres[:-1]) + ' y ' + nombres[-1]
        return texto[0].upper() + texto[1:]

    @property
    def tipo_bloque(self):
        """El bloque del presupuesto al que cuenta esta partida.

        Con categoría manda la categoría; sin ella, el bloque declarado. Es el
        único sitio donde se decide, para que ninguna pantalla tenga que
        acordarse de mirar los dos campos."""
        if self.categoria_id and self.categoria:
            return self.categoria.tipo
        return self.bloque

    @property
    def es_del_bloque(self):
        """Presupuesto declarado para el bloque entero, sin desglosar."""
        return not self.categoria_id and bool(self.bloque)

    @property
    def meses_periodo(self):
        """A cuántos meses se reparte este gasto.

        Es el único sitio donde se traduce la periodicidad a meses, para que
        nadie tenga que acordarse de que «trienal» son treinta y seis."""
        if self.periodicidad == 'personalizada':
            return max(int(self.meses_personalizados or 1), 1)
        if self.es_puntual:
            # Los meses que se aparta: del primero al del pago, ambos incluidos.
            if not (self.mes_pago and self.anio_pago):
                return 1
            return max(self.anio_pago * 12 + self.mes_pago - 1 - self._inicio_ahorro() + 1, 1)
        return MESES_POR_PERIODICIDAD.get(self.periodicidad, 1)

    def get_periodicidad_display(self):
        """«Cada 50 meses» en lugar de «Cada N meses…».

        Django pone este método solo si el modelo no lo trae ya, así que
        definirlo aquí arregla de una vez las veinte plantillas que lo
        llaman, en vez de que cada una tenga que acordarse del caso raro."""
        if self.periodicidad == 'personalizada':
            return f'Cada {self.meses_periodo} meses'
        if self.es_puntual:
            return 'Puntual'
        return dict(PERIODICIDAD_GASTO_CHOICES).get(self.periodicidad, self.periodicidad)

    @property
    def importe_mensual(self):
        return round(self.importe / Decimal(self.meses_periodo), 2)

    @property
    def importe_anual(self):
        """Lo que cuesta AL AÑO. Unos neumáticos de 470 € cada tres años son
        156,67 € al año, no 470."""
        return round(self.importe * Decimal('12') / Decimal(self.meses_periodo), 2)


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
]


class FondoFamiliar(models.Model):
    hogar = models.ForeignKey('core.Hogar', on_delete=models.CASCADE, related_name='fondos')
    nombre = models.CharField(max_length=100,
        help_text="Ej: Fondo comun, Ahorro piso, Inversion, Emergencia...")
    tipo_fondo = models.CharField(max_length=20, choices=TIPO_FONDO_CHOICES, default='comun',
        help_text="Categoría del fondo para reporting (ahorro, inversión, etc.)")
    modo_aportacion = models.CharField(max_length=20, choices=MODO_APORTACION_CHOICES, default='proporcional')
    color = models.CharField(max_length=7, default='#a259ff')
    cuenta_asociada = models.CharField(max_length=150, blank=True, default='',
        help_text="Nombre o descripcion de la cuenta bancaria asociada a este fondo.")
    propietario = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='fondos_propios',
        help_text="Titular del fondo. Determina quién declara sus rendimientos en el "
                   "Informe Hacienda. Vacío = compartido del hogar.",
    )
    orden = models.IntegerField(default=0)
    activo = models.BooleanField(default=True)

    class Meta:
        ordering = ['orden', 'nombre']
        unique_together = ('hogar', 'nombre')

    def __str__(self):
        return f"{self.nombre} ({self.get_tipo_fondo_display()})"

    @property
    def titular_nombre(self):
        """Nombre del titular del fondo; 'Compartido' si no tiene propietario."""
        if not self.propietario_id:
            return 'Compartido'
        return self.propietario.first_name or self.propietario.username

    # --- Integración con módulo de inversiones ---

    @property
    def valor_cartera(self):
        if self.tipo_fondo != 'inversion':
            return None
        return sum(
            (inv.valor_total_actual or Decimal('0'))
            for inv in self.inversiones.exclude(tipo='DEPOSITO').select_related('valor_actual').all()
        )

    @property
    def total_aportado_cartera(self):
        if self.tipo_fondo != 'inversion':
            return None
        return sum(
            (inv.valor_aportado or Decimal('0'))
            for inv in self.inversiones.exclude(tipo='DEPOSITO').all()
        )

    @property
    def rentabilidad_cartera(self):
        aportado = self.total_aportado_cartera
        if aportado is None or aportado <= 0:
            return None
        valor = self.valor_cartera or 0
        return round(float((valor - aportado) / aportado * 100), 2)

    @property
    def num_activos_vinculados(self):
        if self.tipo_fondo != 'inversion':
            return 0
        return self.inversiones.count()


class ReglaReparto(models.Model):
    TIPO_REGLA_CHOICES = [
        ('porcentaje', 'Porcentaje del dinero libre'),
        ('fijo', 'Importe fijo mensual'),
    ]
    PERIODICIDAD_REGLA_CHOICES = [
        ('mensual', 'Aplica cada mes sobre ingreso mensual'),
        ('anual', 'Aplica sobre ingresos anuales / pagas extras'),
    ]

    hogar = models.ForeignKey('core.Hogar', on_delete=models.CASCADE, related_name='reglas_reparto')
    fondo = models.ForeignKey(FondoFamiliar, on_delete=models.CASCADE, related_name='reglas',
        null=True, blank=True,
        help_text="Fondo al que se destina. Si vacio, es dinero sin asignar a fondo.")
    nombre = models.CharField(max_length=100)
    tipo_regla = models.CharField(max_length=20, choices=TIPO_REGLA_CHOICES, default='porcentaje')
    periodicidad_regla = models.CharField(max_length=10, choices=PERIODICIDAD_REGLA_CHOICES, default='mensual',
        help_text="Mensual: opera sobre el libre mensual. Anual: opera sobre pagas extras e ingresos extraordinarios del mes.")
    porcentaje = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'),
        help_text="Porcentaje del dinero libre (solo si tipo=porcentaje)")
    importe_fijo = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'),
        help_text="Importe fijo mensual (solo si tipo=fijo)")
    usuario = models.ForeignKey('auth.User', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='reglas_reparto',
        help_text="Si se especifica, esta regla aplica solo a este miembro. Si vacio, aplica al total del hogar.")
    solo_mes = models.IntegerField(null=True, blank=True, choices=MESES_CHOICES,
        help_text='Si se especifica, esta regla SOLO se aplica en ese mes. Vacio = todos los meses.')
    color = models.CharField(max_length=7, default='#a259ff')
    orden = models.IntegerField(default=0)
    activo = models.BooleanField(default=True)

    class Meta:
        ordering = ['orden', 'nombre']

    def __str__(self):
        if self.tipo_regla == 'porcentaje':
            return f"{self.nombre} ({self.porcentaje}%)"
        return f"{self.nombre} ({self.importe_fijo} EUR/mes)"


class AjusteIngresoMensual(models.Model):
    fuente = models.ForeignKey('FuenteIngreso', on_delete=models.CASCADE, related_name='ajustes_mensuales')
    año = models.IntegerField()
    mes = models.IntegerField(help_text='Número de mes 1-12')
    importe_real = models.DecimalField(max_digits=12, decimal_places=2,
        help_text='Importe neto real cobrado este mes (ya neto, sin recalcular IRPF)')
    nota = models.CharField(max_length=255, blank=True, default='',
        help_text="Ej: 'Solo 3 guardias este mes'")
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-año', '-mes']
        unique_together = ('fuente', 'año', 'mes')

    def __str__(self):
        return f"{self.fuente.nombre} {self.mes}/{self.año}: {self.importe_real}€"


class SubsobreFondo(models.Model):
    TIPO_CHOICES = [
        ('gasto_fijo', 'Cubre gasto fijo del hogar'),
        ('gasto_variable', 'Cubre gasto variable del hogar'),
        ('discrecional', 'Gasto discrecional'),
        ('libre', 'Sin asignación / libre'),
    ]

    fondo = models.ForeignKey('FondoFamiliar', on_delete=models.CASCADE, related_name='subsobres')
    nombre = models.CharField(max_length=100,
        help_text='Ej: Ocio, Restaurantes, Alimentación, Ropa, Suscripciones...')
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES, default='discrecional')
    partidas_vinculadas = models.ManyToManyField('PartidaGasto', blank=True,
        help_text='Si vinculas partidas, el importe se calcula sumando su importe_mensual.')
    importe_manual = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True,
        help_text='Importe mensual fijo si no sigue a un bloque ni a unas partidas.')
    # Seguir un bloque del presupuesto en vez de teclear una cifra. Es el caso
    # del «Aporte Gastos Anuales»: lo que se aparta cada mes para los gastos
    # anuales ES la provisión de ese bloque, y si se teclea a mano deja de
    # cuadrar en cuanto se añade un gasto anual nuevo.
    bloque = models.CharField(
        max_length=20, blank=True, default='',
        choices=[(t, ETIQUETAS_TIPO[t]) for t in TIPOS_GASTO],
        help_text='Si se indica, el importe es lo que suma ese bloque del presupuesto, '
                  'y se actualiza solo al añadir o cambiar gastos.',
    )
    fondo_destino = models.ForeignKey('FondoFamiliar', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='subsobres_entrantes',
        help_text="Si se indica, el importe de este sobre se transfiere a otro fondo.")
    solo_mes = models.IntegerField(null=True, blank=True, choices=MESES_CHOICES,
        help_text='Si se especifica, este sobre SOLO aplica en ese mes. Vacio = todos los meses.')
    orden = models.IntegerField(default=0)
    activo = models.BooleanField(default=True)

    class Meta:
        ordering = ['fondo', 'orden', 'nombre']

    def __str__(self):
        return f"{self.fondo.nombre} → {self.nombre}"

    def importe_en(self, limites_bloque=None):
        """Lo que mueve este movimiento al mes, de más automático a más manual.

        `limites_bloque` es el `{bloque: importe/mes}` del presupuesto ya
        calculado. Se pasa desde fuera porque el motor de distribución recorre
        todos los subsobres y consultarlo dentro sería una consulta por cada uno.

        Antes, un movimiento HACIA OTRO FONDO devolvía el importe tecleado y
        punto: ni miraba las partidas vinculadas, aunque el propio campo promete
        que lo hace. Es lo que dejaba el «Aporte Gastos Anuales» clavado en la
        cifra del día que se creó mientras el bloque de anuales seguía subiendo.
        """
        if self.bloque:
            if limites_bloque is None:
                from . import presupuesto
                limites_bloque = presupuesto.por_bloque(self.fondo.hogar_id)
            return limites_bloque.get(self.bloque) or Decimal('0')

        partidas = self.partidas_vinculadas.filter(activo=True)
        if partidas.exists():
            return sum(p.importe_mensual for p in partidas)
        return self.importe_manual or Decimal('0')

    @property
    def importe_calculado(self):
        return self.importe_en()

    @property
    def sigue_al_presupuesto(self):
        """¿El importe lo pone el presupuesto o lo tecleó el usuario?"""
        return bool(self.bloque) or self.partidas_vinculadas.filter(activo=True).exists()

    @property
    def es_transferencia(self):
        return self.fondo_destino_id is not None


class IngresoExtraordinario(models.Model):
    hogar = models.ForeignKey('core.Hogar', on_delete=models.CASCADE, related_name='ingresos_extraordinarios')
    usuario = models.ForeignKey(User, on_delete=models.CASCADE, related_name='ingresos_extraordinarios')
    concepto = models.CharField(max_length=200,
        help_text="Ej: Bonus anual, Devolución IRPF, Venta coche...")
    importe = models.DecimalField(max_digits=12, decimal_places=2)
    es_neto = models.BooleanField(default=True,
        help_text="True = ya neto, False = bruto (se aplicarán retenciones)")
    año = models.IntegerField()
    mes = models.IntegerField(help_text='Mes en que se recibe (1-12)')
    fondo_destino = models.ForeignKey('FondoFamiliar', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='ingresos_extraordinarios',
        help_text="Si se asigna, este ingreso alimenta directamente el fondo.")
    nota = models.CharField(max_length=255, blank=True, default='')
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-año', '-mes', '-creado_en']

    def __str__(self):
        return f"{self.concepto} ({self.mes}/{self.año}) -- €{self.importe}"


# ─── Módulo Evolución ─────────────────────────────────────────────────────────

class SaldoRealFondo(models.Model):
    fondo = models.ForeignKey('FondoFamiliar', on_delete=models.CASCADE, related_name='saldos_reales')
    año = models.IntegerField()
    mes = models.IntegerField(help_text='1-12')
    saldo = models.DecimalField(max_digits=14, decimal_places=2)
    nota = models.CharField(max_length=255, blank=True, default='')
    registrado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-año', '-mes', 'fondo']
        unique_together = ('fondo', 'año', 'mes')

    def __str__(self):
        return f"{self.fondo.nombre} {self.mes}/{self.año}: €{self.saldo}"


class IngresoRealMes(models.Model):
    hogar = models.ForeignKey('core.Hogar', on_delete=models.CASCADE, related_name='ingresos_reales')
    año = models.IntegerField()
    mes = models.IntegerField()
    importe = models.DecimalField(max_digits=12, decimal_places=2)
    nota = models.CharField(max_length=255, blank=True, default='')
    registrado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-año', '-mes']
        unique_together = ('hogar', 'año', 'mes')

    def __str__(self):
        return f"Ingresos {self.mes}/{self.año} - {self.hogar.nombre}: €{self.importe}"


class CierreMensual(models.Model):
    """Foto de un mes ya CERRADO.

    Evolución es un registro histórico, no una vista en vivo: lo que pasó en
    julio no puede cambiar porque hoy subas el sueldo. Cuando un mes queda
    atrás, las cifras que Evolución consume del motor de distribución se
    congelan aquí y ya no se recalculan. El mes en curso —y los futuros— se
    siguen calculando en vivo.
    """
    hogar = models.ForeignKey('core.Hogar', on_delete=models.CASCADE, related_name='cierres_mensuales')
    año = models.IntegerField()
    mes = models.IntegerField(help_text='Número de mes 1-12')
    ingreso = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'),
        help_text='Ingreso neto del hogar ese mes, tal y como quedó al cerrarlo.')
    gastos = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'),
        help_text='Total de gastos del mes al cerrarlo.')
    inversion = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'),
        help_text='Importe que ese mes salió hacia fondos de inversión.')
    # Nulos = cierre tomado antes de que existieran estos campos: para esos
    # meses se sigue usando el valor en vivo (no se puede inventar el pasado).
    ahorro = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
        help_text='Importe que ese mes salió hacia fondos de ahorro.')
    libre = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
        help_text='Dinero que quedó libre ese mes, sin asignar a ningún fondo.')
    ingreso_previsto = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
        help_text='Ingreso base presupuestado para ese mes (sin ajustes ni extras), '
                  'tal y como estaba al cerrarlo. Es el "según el plan" con el que se '
                  'compara lo que de verdad entró.')
    congelado_en = models.DateTimeField(auto_now_add=True)
    actualizado_en = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-año', '-mes']
        unique_together = ('hogar', 'año', 'mes')
        verbose_name = 'Cierre mensual'
        verbose_name_plural = 'Cierres mensuales'

    def __str__(self):
        return f"Cierre {self.mes}/{self.año} - {self.hogar.nombre}: €{self.ingreso}"


# ─── Catálogo de Tickers ──────────────────────────────────────────────────────

class TickerCatalogo(models.Model):
    """Cache local de tickers buscados via Yahoo Finance API."""
    symbol = models.CharField(max_length=20, unique=True, db_index=True)
    nombre = models.CharField(max_length=200)
    exchange = models.CharField(max_length=50, blank=True, default='')
    tipo_activo = models.CharField(max_length=30, blank=True, default='',
        help_text="EQUITY, ETF, CRYPTOCURRENCY, MUTUALFUND...")
    moneda = models.CharField(max_length=10, blank=True, default='')
    buscado_en = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['symbol']

    def __str__(self):
        return f"{self.symbol} — {self.nombre} ({self.exchange})"


# ─── Módulo Inmuebles ─────────────────────────────────────────────────────────

class Vehiculo(models.Model):
    """Un coche, una moto… Existe para poder imputarle gastos y saber lo que
    cuesta mantenerlo, no para valorar patrimonio."""

    TIPO_CHOICES = [
        ('coche', 'Coche'),
        ('moto', 'Moto'),
        ('furgoneta', 'Furgoneta'),
        ('bici', 'Bicicleta'),
        ('otro', 'Otro'),
    ]

    hogar = models.ForeignKey('core.Hogar', on_delete=models.CASCADE, related_name='vehiculos')
    nombre = models.CharField(max_length=120, help_text='Ej: Golf de Ana, Furgo del trabajo…')
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES, default='coche')
    marca_modelo = models.CharField(max_length=120, blank=True)
    matricula = models.CharField(max_length=20, blank=True)

    fecha_compra = models.DateField(null=True, blank=True)
    precio_compra = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text='Lo que costó, para poder repartir su coste real de uso.',
    )
    valor_actual = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text='Valoración de mercado estimada a día de hoy.',
    )

    color = models.CharField(max_length=7, default='#2c5f7a')
    notas = models.CharField(max_length=300, blank=True)
    activo = models.BooleanField(default=True)
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['nombre']
        verbose_name = 'Vehículo'
        verbose_name_plural = 'Vehículos'

    def __str__(self):
        return self.nombre

    @property
    def clave_activo(self):
        return f'vehiculo:{self.pk}'

    @property
    def etiqueta_tipo(self):
        return self.get_tipo_display()

    @property
    def depreciacion_anual(self):
        """Lo que pierde de valor al año desde que se compró. No es un gasto que
        pase por el banco, pero es dinero: un coche que cuesta 60 €/mes de
        mantenimiento y se deprecia 2.000 €/año no cuesta 60 €/mes."""
        if not (self.precio_compra and self.valor_actual and self.fecha_compra):
            return None
        anios = (date.today() - self.fecha_compra).days / Decimal('365.25')
        if anios <= 0:
            return None
        return ((self.precio_compra - self.valor_actual) / anios).quantize(Decimal('0.01'))


class Propiedad(models.Model):
    TIPO_CHOICES = [
        ('vivienda', 'Vivienda'),
        ('local', 'Local comercial'),
        ('terreno', 'Terreno'),
        ('garaje', 'Garaje / Plaza'),
        ('otro', 'Otro inmueble'),
    ]

    hogar = models.ForeignKey(
        'core.Hogar', on_delete=models.CASCADE, related_name='propiedades'
    )
    nombre = models.CharField(max_length=200, help_text='Ej: Piso calle Mayor, Local Getafe…')
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES, default='vivienda')
    descripcion = models.CharField(max_length=500, blank=True)

    # Datos de adquisición
    fecha_compra = models.DateField()
    precio_compra = models.DecimalField(
        max_digits=12, decimal_places=2,
        help_text='Precio escriturado (sin gastos de compra)'
    )
    gastos_compra = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0'),
        help_text='ITP/AJD, notaría, registro, gestoría…'
    )

    # Estado actual (se actualiza mensualmente)
    valor_actual = models.DecimalField(
        max_digits=12, decimal_places=2,
        help_text='Valoración actual de mercado'
    )
    deuda_hipotecaria = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0'),
        help_text='Capital pendiente de hipoteca a día de hoy'
    )

    # Estimación de costes de venta
    gastos_venta_pct = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('3.0'),
        help_text='% costes de venta: agencia, notaría, gestoría…'
    )
    es_residencia_habitual = models.BooleanField(
        default=False,
        help_text='Puede influir en exenciones fiscales al vender'
    )

    # ── Alquiler ──────────────────────────────────────────────────────────
    # Un piso alquilado no solo cuesta: deja. Y lo que deja tributa en el IRPF
    # de quien es su dueño, no del hogar: si el piso es de Irene, el alquiler
    # es renta de Irene y la cuota sale de su declaración.
    alquilada = models.BooleanField(default=False, help_text='Está alquilada y da un ingreso.')
    propietario = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='propiedades_propias',
        help_text='Titular a efectos del IRPF. Vacío: a partes iguales entre los miembros del hogar.',
    )
    # Reducción del rendimiento neto positivo cuando se alquila como vivienda
    # habitual del inquilino (art. 23.2 LIRPF). Desde la Ley 12/2023, el 50 %
    # general, 60/70/90 % en casos concretos; los contratos de antes del 26 de
    # mayo de 2023 conservan el 60 %. Un local, un garaje o un alquiler de
    # temporada no tienen reducción: 0.
    REDUCCION_CHOICES = [
        (0, 'Sin reducción (local, garaje, temporada, turístico)'),
        (50, '50 % · vivienda habitual del inquilino (general)'),
        (60, '60 % · contrato anterior a mayo de 2023 o vivienda rehabilitada'),
        (70, '70 % · primer alquiler a joven de 18-35 años en zona tensionada'),
        (90, '90 % · bajada de renta de al menos un 5 % en zona tensionada'),
    ]
    reduccion_alquiler_pct = models.PositiveSmallIntegerField(
        default=50, choices=REDUCCION_CHOICES,
        help_text='Reducción del rendimiento neto del alquiler en el IRPF.',
    )
    # Para la amortización (3 % al año de lo que es construcción, no suelo):
    # el recibo del IBI separa el valor catastral del suelo y el de la
    # construcción. Sin el dato no se amortiza y el impuesto sale por arriba.
    pct_construccion = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text='% del valor catastral que es construcción (lo dice el recibo del IBI).',
    )
    # La cuota de la hipoteca es capital + intereses, y solo los intereses se
    # deducen. Los extractos no los separan: se dicen aquí, al año.
    intereses_hipoteca_anuales = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text='Intereses de la hipoteca pagados en el año (los da el banco).',
    )
    # El gasto anual creado desde la ficha con el IRPF del alquiler: para
    # actualizarlo en vez de crear otro, y para que su pago no se cuente como
    # gasto deducible del propio alquiler.
    # Desde cuándo está alquilada. Vacío: desde el primer cobro imputado. Es el
    # inicio del balance «desde que la alquilas»: lo de antes no es del
    # alquiler.
    alquilada_desde = models.DateField(
        null=True, blank=True,
        help_text='Inicio del alquiler. Vacío: el mes del primer cobro imputado.',
    )
    partida_irpf = models.ForeignKey(
        'PartidaGasto', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    # Lo que se pidió prestado al comprar. Con él sale el dinero que pusiste
    # de tu bolsillo (coste de compra − préstamo), que es contra lo que se
    # mide la rentabilidad «sobre tu dinero». La deuda de hoy no sirve: baja
    # cada mes y no dice cuánto pusiste.
    hipoteca_inicial = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text='Importe del préstamo hipotecario al comprar. Vacío o 0: sin hipoteca.',
    )
    # Base de la amortización del 3 % en el IRPF del alquiler, en euros: lo
    # que es construcción (no suelo). Si se dice, manda sobre `pct_construccion`.
    valor_construccion = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text='Valor de la construcción (sin suelo) para la amortización del 3 %.',
    )
    # El tipo marginal de IRPF con que tributa el alquiler. Vacío: se calcula
    # con los tramos sobre la nómina del titular (Fuentes de ingreso).
    tipo_marginal_pct = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text='Tipo marginal del IRPF. Vacío: se calcula con la nómina del titular.',
    )
    # Lo que rendiría el dinero fuera del piso: contra eso se compara
    # amortizar la hipoteca o mantener la propiedad. Vacío: 3 %.
    tipo_referencia_pct = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text='Rentabilidad alternativa del dinero (%). Vacío: 3 %.',
    )
    # El último escenario del simulador de alquiler (para las no alquiladas):
    # al volver a la ficha sale lo que se puso. Ver `rentabilidad.simular`.
    simulacion_alquiler = models.JSONField(default=dict, blank=True)

    color = models.CharField(max_length=7, default='#e67e22')
    activo = models.BooleanField(default=True)
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['nombre']

    def __str__(self):
        return f"{self.nombre} ({self.get_tipo_display()})"

    @property
    def titular_nombre(self):
        if not self.propietario_id:
            return 'A partes iguales'
        return self.propietario.first_name or self.propietario.username

    @property
    def clave_activo(self):
        """Identificador para los selectores de imputación, igual que en
        Vehiculo: un solo desplegable en toda la interfaz."""
        return f'propiedad:{self.pk}'

    @property
    def tiene_hipoteca_declarada(self):
        from . import hipoteca_propiedad
        return bool(hipoteca_propiedad.activas(self))

    @property
    def deuda_actual(self):
        """Lo que se debe hoy: del cuadro de sus hipotecas si están
        declaradas; si no, el número puesto a mano."""
        from . import hipoteca_propiedad
        deuda = hipoteca_propiedad.deuda(self)
        return self.deuda_hipotecaria if deuda is None else deuda

    @property
    def patrimonio_neto(self):
        return self.valor_actual - self.deuda_actual

    @property
    def coste_base(self):
        """Precio de compra + gastos de adquisición (base para calcular plusvalía)."""
        return self.precio_compra + self.gastos_compra

    @property
    def ganancia_bruta(self):
        return self.valor_actual - self.coste_base

    def calcular_plusvalia(self):
        """IRPF 2024 — tramos de ganancias patrimoniales del ahorro."""
        ganancia = max(Decimal('0'), self.ganancia_bruta)
        if ganancia == 0:
            return Decimal('0')
        tramos = [
            (Decimal('6000'),    Decimal('0.19')),
            (Decimal('44000'),   Decimal('0.21')),
            (Decimal('150000'),  Decimal('0.23')),
            (Decimal('1000000'), Decimal('0.27')),
        ]
        tax = Decimal('0')
        remaining = ganancia
        for limit, rate in tramos:
            chunk = min(remaining, limit)
            tax += chunk * rate
            remaining -= chunk
            if remaining <= 0:
                break
        return tax.quantize(Decimal('0.01'))

    def calcular_neto_venta(self):
        """Capital que quedaría libre hoy si se vendiera al valor actual.

        `neto` descuenta deuda, gastos de venta y la plusvalía del IRPF.
        `liberable` es lo que de verdad quedaría para usar: en la vivienda
        habitual la plusvalía puede quedar exenta si se reinvierte en otra
        (art. 38 LIRPF), así que ahí se dice aparte y no se resta."""
        deuda = self.deuda_actual
        gastos = (self.valor_actual * self.gastos_venta_pct / Decimal('100')).quantize(Decimal('0.01'))
        plusvalia = self.calcular_plusvalia()
        neto = self.valor_actual - deuda - gastos - plusvalia
        liberable = self.valor_actual - deuda - gastos - (Decimal('0') if self.es_residencia_habitual else plusvalia)
        return {
            'id': self.pk,
            'nombre': self.nombre,
            'valor_actual': round(float(self.valor_actual), 2),
            'deuda': round(float(deuda), 2),
            'deuda_del_cuadro': self.tiene_hipoteca_declarada,
            'ganancia_bruta': round(float(self.ganancia_bruta), 2),
            'plusvalia': round(float(plusvalia), 2),
            'plusvalia_exenta_si_reinviertes': self.es_residencia_habitual and plusvalia > 0,
            'gastos_venta': round(float(gastos), 2),
            'gastos_venta_pct': float(self.gastos_venta_pct),
            'neto': round(float(neto), 2),
            'liberable': round(float(liberable), 2),
        }


class Hipoteca(models.Model):
    """Un préstamo hipotecario de una propiedad, con sus condiciones.

    Con ellas se genera el cuadro de amortización (`finanzas/amortizacion.py`)
    y de él salen la deuda de hoy, los intereses y el capital del año: dejan
    de ser números puestos a mano. Una propiedad puede tener más de una
    (una segunda hipoteca, un préstamo para la reforma…).

    Si no se conoce la historia —o un variable ha tenido revisiones que no
    se tienen— basta un punto de partida: el saldo a una fecha (lo dice el
    recibo o el certificado del banco) y, si se sabe, la cuota. El cuadro
    empieza ahí.
    """
    MODALIDAD_CHOICES = [
        ('fijo', 'Fijo'),
        ('variable', 'Variable'),
        ('mixto', 'Mixto'),
    ]
    INDICE_CHOICES = [
        ('euribor_12m', 'Euríbor a 12 meses'),
        ('irph', 'IRPH'),
        ('otro', 'Otro'),
    ]

    propiedad = models.ForeignKey(Propiedad, on_delete=models.CASCADE, related_name='hipotecas')
    nombre = models.CharField(max_length=100, default='Hipoteca')
    entidad = models.CharField(max_length=100, blank=True)

    # Origen
    capital_inicial = models.DecimalField(max_digits=12, decimal_places=2)
    fecha_firma = models.DateField()
    plazo_meses = models.PositiveIntegerField(help_text='Plazo total en meses (30 años = 360).')
    dia_cobro = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text='Día del mes en que se cobra. Vacío: el de la firma.',
    )

    # Tipo de interés
    modalidad = models.CharField(max_length=10, choices=MODALIDAD_CHOICES, default='fijo')
    tipo_inicial_pct = models.DecimalField(
        max_digits=6, decimal_places=3,
        help_text='TIN fijo, o el del tramo fijo inicial en variable y mixto.',
    )
    meses_tramo_fijo = models.PositiveIntegerField(
        null=True, blank=True, help_text='Meses al tipo inicial antes de pasar a índice + diferencial.',
    )
    indice = models.CharField(max_length=20, choices=INDICE_CHOICES, default='euribor_12m', blank=True)
    diferencial_pct = models.DecimalField(max_digits=6, decimal_places=3, null=True, blank=True)
    revision_meses = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text='Cada cuántos meses se revisa el tipo (6 o 12).',
    )

    # Punto de partida conocido (opcional)
    saldo_conocido = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    saldo_conocido_fecha = models.DateField(null=True, blank=True)
    cuota_conocida = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text='Cuota mensual en esa fecha. Vacío: se calcula con el plazo que queda.',
    )

    # Comisión por amortización anticipada. Vacío: lo máximo que permite la
    # Ley 5/2019 según la modalidad (ver `amortizacion.py`).
    comision_pct_inicial = models.DecimalField(max_digits=5, decimal_places=3, null=True, blank=True)
    comision_meses_iniciales = models.PositiveIntegerField(null=True, blank=True)
    comision_pct_despues = models.DecimalField(max_digits=5, decimal_places=3, null=True, blank=True)

    # Solo informativo: [{"concepto", "rebaja_pct", "coste_anual"}]
    bonificaciones = models.JSONField(default=list, blank=True)

    # Dónde se apunta la cuota, para conciliarla con lo que pasa por el banco.
    partida = models.ForeignKey(
        'PartidaGasto', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    activa = models.BooleanField(default=True)
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['fecha_firma', 'pk']

    def __str__(self):
        return f'{self.nombre} · {self.propiedad.nombre}'

    def clean(self):
        from django.core.exceptions import ValidationError
        errores = {}
        if self.capital_inicial is not None and self.capital_inicial <= 0:
            errores['capital_inicial'] = 'El capital tiene que ser mayor que cero.'
        if self.plazo_meses is not None and self.plazo_meses <= 0:
            errores['plazo_meses'] = 'El plazo tiene que ser de al menos un mes.'
        if self.dia_cobro is not None and not 1 <= self.dia_cobro <= 31:
            errores['dia_cobro'] = 'Un día del mes, de 1 a 31.'
        if self.modalidad in ('variable', 'mixto') and self.diferencial_pct is None:
            errores['diferencial_pct'] = 'Un variable necesita el diferencial sobre el índice.'
        if self.modalidad == 'mixto' and not self.meses_tramo_fijo:
            errores['meses_tramo_fijo'] = 'Un mixto necesita la duración del tramo fijo.'
        if (self.saldo_conocido is None) != (self.saldo_conocido_fecha is None):
            errores['saldo_conocido'] = 'El punto de partida necesita saldo y fecha.'
        if errores:
            raise ValidationError(errores)

    def como_prestamo(self, indice_futuro=None):
        """El préstamo en el formato del motor (el mismo que recibe el JS)."""
        def f(valor):
            return None if valor is None else float(valor)

        p = {
            'capital': float(self.capital_inicial),
            'inicio': self.fecha_firma.isoformat(),
            'plazo_meses': self.plazo_meses,
            'dia_cobro': self.dia_cobro,
            'modalidad': self.modalidad,
            'tipo_inicial': float(self.tipo_inicial_pct),
            'meses_tramo_fijo': self.meses_tramo_fijo,
            'diferencial': f(self.diferencial_pct),
            'revision_meses': self.revision_meses or 12,
            'indice_futuro': f(indice_futuro),
            'revisiones': [
                {'fecha': r.fecha_desde.isoformat(), 'tipo': float(r.tipo_pct)}
                for r in self.revisiones.all()
            ],
            'amortizaciones': [
                {'fecha': a.fecha.isoformat(), 'importe': float(a.importe), 'modo': a.modo,
                 'comision': f(a.comision_pagada)}
                for a in self.amortizaciones.all()
            ],
            'comision': {
                'pct_inicial': f(self.comision_pct_inicial),
                'meses_iniciales': self.comision_meses_iniciales,
                'pct_despues': f(self.comision_pct_despues),
            },
        }
        if self.saldo_conocido is not None and self.saldo_conocido_fecha:
            p['ancla'] = {
                'fecha': self.saldo_conocido_fecha.isoformat(),
                'saldo': float(self.saldo_conocido),
                'cuota': f(self.cuota_conocida),
            }
        return p

    def cuadro(self, indice_futuro=None):
        from . import amortizacion
        clave = ('_cuadro', indice_futuro)
        cache = self.__dict__.setdefault('_cuadros', {})
        if clave not in cache:
            cache[clave] = amortizacion.cuadro(self.como_prestamo(indice_futuro))
        return cache[clave]

    def saldo_a(self, dia=None):
        """Capital pendiente un día (hoy si no se dice)."""
        import datetime
        from . import amortizacion
        dia = dia or datetime.date.today()
        return Decimal(str(amortizacion.saldo_a(self.como_prestamo(), self.cuadro(), dia)))

    def del_anio(self, anio):
        """Cuotas, intereses y capital del año natural, según el cuadro."""
        from . import amortizacion
        return amortizacion.del_anio(self.cuadro(), anio)

    def tipo_actual(self, dia=None):
        import datetime
        from . import amortizacion
        dia = dia or datetime.date.today()
        filas = self.cuadro()
        previas = [f for f in filas if f['fecha'] <= dia]
        fila = previas[-1] if previas else (filas[0] if filas else None)
        return fila['tipo'] if fila else float(self.tipo_inicial_pct)


class RevisionTipo(models.Model):
    """El tipo que aplica desde una fecha: cada revisión de un variable, o el
    «tipo actual» que se quiera fijar a mano."""
    hipoteca = models.ForeignKey(Hipoteca, on_delete=models.CASCADE, related_name='revisiones')
    fecha_desde = models.DateField()
    tipo_pct = models.DecimalField(max_digits=6, decimal_places=3)
    valor_indice_pct = models.DecimalField(
        max_digits=6, decimal_places=3, null=True, blank=True,
        help_text='Valor del índice usado en la revisión (informativo).',
    )

    class Meta:
        ordering = ['fecha_desde']
        unique_together = ('hipoteca', 'fecha_desde')

    def __str__(self):
        return f'{self.hipoteca} · {self.tipo_pct} % desde {self.fecha_desde:%d/%m/%Y}'


class AmortizacionAnticipada(models.Model):
    """Una amortización anticipada ya hecha."""
    MODO_CHOICES = [
        ('cuota', 'Reducir cuota'),
        ('plazo', 'Reducir plazo'),
    ]
    hipoteca = models.ForeignKey(Hipoteca, on_delete=models.CASCADE, related_name='amortizaciones')
    fecha = models.DateField()
    importe = models.DecimalField(max_digits=12, decimal_places=2)
    modo = models.CharField(max_length=5, choices=MODO_CHOICES, default='plazo')
    comision_pagada = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text='La comisión que se pagó. Vacío: se estima con la del préstamo.',
    )

    class Meta:
        ordering = ['fecha', 'pk']

    def __str__(self):
        return f'{self.hipoteca} · {self.importe} € el {self.fecha:%d/%m/%Y}'


class HistorialPropiedad(models.Model):
    propiedad = models.ForeignKey(
        Propiedad, on_delete=models.CASCADE, related_name='historial'
    )
    año = models.IntegerField()
    mes = models.IntegerField(help_text='1-12')
    valor_mercado = models.DecimalField(max_digits=12, decimal_places=2)
    deuda_hipotecaria = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0')
    )
    nota = models.CharField(max_length=255, blank=True, default='')
    registrado_en = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-año', '-mes']
        unique_together = ('propiedad', 'año', 'mes')

    def __str__(self):
        return f"{self.propiedad.nombre} {self.mes}/{self.año}: €{self.valor_mercado}"


class EstudioVehiculo(models.Model):
    """Un estudio del comparador de coche guardado para volver a él.

    Comprar un coche se decide en semanas, no en una tarde: se prueba el X2 en
    leasing, luego comprado, luego otro modelo, y hace falta tener los números
    de cada intento a mano para compararlos. Se guarda la configuración entera
    —para poder abrirla y seguir tocándola— y un resumen con las cifras que
    salieron, para compararlos en la lista sin tener que abrir cada uno.

    Es del HOGAR: el coche se decide en casa, y cualquiera de los dos tiene que
    poder abrir lo que estudió el otro.
    """
    hogar = models.ForeignKey('core.Hogar', on_delete=models.CASCADE, related_name='estudios_vehiculo')
    usuario = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='estudios_vehiculo')
    nombre = models.CharField(max_length=120)
    datos = models.JSONField(default=dict, help_text='Configuración completa del comparador.')
    resumen = models.JSONField(default=dict, blank=True,
                               help_text='Cifras de cada opción al guardarlo, para la lista.')
    creado = models.DateTimeField(auto_now_add=True)
    actualizado = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-actualizado']
        verbose_name = 'Estudio de vehículo'
        verbose_name_plural = 'Estudios de vehículo'

    def __str__(self):
        return self.nombre

    def como_dict(self):
        return {
            'id': self.pk,
            'nombre': self.nombre,
            'datos': self.datos,
            'resumen': self.resumen,
            'autor': self.usuario.get_username() if self.usuario else '',
            'actualizado': self.actualizado.strftime('%d/%m/%Y %H:%M'),
        }
