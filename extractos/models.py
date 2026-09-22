import hashlib
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from django.contrib.auth.models import User
from django.db import models

from finanzas.models import ImputableAActivo

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
        """Lo que traía el archivo del banco.

        Las PARTES de un cobro repartido quedan fuera: no venían en el extracto
        —las creó el usuario al repartir— y sumarlas junto al apunte del que
        salen contaba el mismo dinero dos veces. El apunte original sí cuenta,
        porque es la línea que el banco escribió.
        """
        agregados = self.movimientos.filter(dividido_de__isnull=True).aggregate(
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


class ReglaDivision(models.Model):
    """División aprendida: si el concepto contiene `patron`, el cobro se parte
    en estas partidas.

    Es la hermana de ReglaCategorizacion para los cobros que son varias cosas a
    la vez. En Norauto se pagan de una vez los neumáticos y la revisión: un
    apunte del banco, dos partidas del presupuesto. Sin esto hay que repartirlo
    a mano cada vez que llega uno.

    (No confundir con `finanzas.ReglaReparto`, que reparte los INGRESOS entre
    los fondos. Ésta parte un cobro concreto del extracto; de ahí el nombre.)

    Las partes guardan PROPORCIONES, no importes: la revisión de este año no
    cuesta lo que la del anterior, pero la forma del recibo se repite. Por eso
    la división automática es una suposición, se ve en la lista con sus partes y
    se deshace de un clic —y el total nunca cambia, que es lo que dice el banco.

    Y tiene FECHA DE EFECTO. Un recibo cambia de forma: el del seguro llevaba
    tres coberturas y a partir de enero lleva cuatro. Sin fecha solo caben dos
    salidas malas —o el reparto nuevo reescribe los recibos viejos, que no eran
    así, o no hay forma de corregirlo—. Con ella, un mismo patrón tiene varias
    versiones: cada recibo usa la que estaba vigente el día que se pagó.
    `desde` en blanco es la versión de siempre, la que vale mientras no haya una
    posterior que le gane.
    """

    ORIGEN_CHOICES = ReglaCategorizacion.ORIGEN_CHOICES

    hogar = models.ForeignKey(
        'core.Hogar', on_delete=models.CASCADE, related_name='reglas_division',
    )
    patron = models.CharField(
        max_length=200,
        help_text='Texto que debe contener el concepto (se compara en minúsculas, sin distinguir acentos de mayúsculas).',
    )
    desde = models.DateField(
        null=True, blank=True,
        help_text='Desde qué fecha vale este reparto. En blanco, vale desde siempre.',
    )
    origen = models.CharField(max_length=10, choices=ORIGEN_CHOICES, default='manual')
    veces_aplicada = models.PositiveIntegerField(default=0)
    activo = models.BooleanField(default=True)
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        # De la más reciente a la más antigua: es el orden en el que se busca la
        # versión vigente y el que tiene sentido al leerlas en pantalla.
        ordering = ['patron', models.F('desde').desc(nulls_last=True)]
        constraints = [
            # Dos restricciones y no una porque en SQL dos NULL no son iguales:
            # con una sola sobre las tres columnas se podrían crear dos versiones
            # «de siempre» del mismo patrón.
            models.UniqueConstraint(
                fields=['hogar', 'patron', 'desde'],
                condition=models.Q(desde__isnull=False),
                name='uniq_regla_div_hogar_patron_desde',
            ),
            models.UniqueConstraint(
                fields=['hogar', 'patron'],
                condition=models.Q(desde__isnull=True),
                name='uniq_regla_div_hogar_patron_siempre',
            ),
        ]
        verbose_name = 'Regla de división'
        verbose_name_plural = 'Reglas de división'

    def __str__(self):
        cuando = f' desde {self.desde}' if self.desde else ''
        return f"«{self.patron}»{cuando} → {self.partes.count()} partes"

    def rige_en(self, fecha):
        """¿Vale esta versión para un recibo de esa fecha?"""
        return self.desde is None or fecha >= self.desde

    def save(self, *args, **kwargs):
        # Igual que en ReglaCategorizacion: el patrón se guarda normalizado para
        # que el unique no deje pasar «Norauto» y «NORAUTO» como dos reglas.
        self.patron = normalizar_texto(self.patron)
        super().save(*args, **kwargs)


class ParteDeDivision(ImputableAActivo):
    """Una de las partidas en las que se parte un cobro, con su peso.

    Hereda de ImputableAActivo porque un recibo puede pagar de golpe el seguro
    de dos coches: cada parte va a la ficha que le toca, igual que cuando se
    divide a mano.
    """

    regla = models.ForeignKey(
        ReglaDivision, on_delete=models.CASCADE, related_name='partes',
    )
    orden = models.PositiveSmallIntegerField(default=0)
    # Fracción del cobro, de 0 a 1. Con seis decimales porque un reparto en
    # tercios necesita más de dos para no descuadrar en cobros grandes.
    proporcion = models.DecimalField(max_digits=9, decimal_places=6)
    categoria = models.ForeignKey(
        'finanzas.CategoriaGasto', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='partes_de_division',
    )
    concepto = models.CharField(max_length=200, blank=True, default='')

    class Meta:
        ordering = ['orden']
        verbose_name = 'Parte de una división'
        verbose_name_plural = 'Partes de una división'

    def __str__(self):
        nombre = self.categoria.nombre if self.categoria else 'Sin categorizar'
        return f"{self.proporcion:.1%} → {nombre}"

    @property
    def porcentaje(self):
        """El peso en tanto por ciento, para poder enseñarlo."""
        return round(float(self.proporcion) * 100, 1)


class Etiqueta(models.Model):
    """Corte transversal sobre los movimientos: «Vacaciones Lisboa», «Obra
    casa», «Regalos Navidad».

    No sustituye a la categoría, la cruza: una cena del viaje es *Restaurantes*
    Y *Vacaciones Lisboa*. Sin esto, la única salida para analizar un gasto
    puntual es inventar categorías («Compras», «Otros») que acaban siendo un
    cajón de sastre que no explica nada.
    """

    PALETA = [
        '#2d6a4f', '#3DCD58', '#2c5f7a', '#b7791f', '#b4442e',
        '#9d4edd', '#5f8fb0', '#d4a017', '#e07a5f', '#40916c',
    ]

    hogar = models.ForeignKey('core.Hogar', on_delete=models.CASCADE, related_name='etiquetas')
    nombre = models.CharField(max_length=60)
    color = models.CharField(max_length=7, default='#2d6a4f')
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['nombre']
        constraints = [
            models.UniqueConstraint(fields=['hogar', 'nombre'], name='uniq_etiqueta_hogar_nombre'),
        ]

    def __str__(self):
        return self.nombre

    @classmethod
    def color_sugerido(cls, hogar):
        """Un color distinto al de las etiquetas que ya tiene el hogar, para que
        se distingan en las listas sin tener que elegirlo a mano."""
        usados = set(cls.objects.filter(hogar=hogar).values_list('color', flat=True))
        for color in cls.PALETA:
            if color not in usados:
                return color
        return cls.PALETA[cls.objects.filter(hogar=hogar).count() % len(cls.PALETA)]


class MovimientoBancario(ImputableAActivo):
    """Un apunte observado en el extracto. Se cruza con los datos declarados.

    Hereda de `ImputableAActivo` para poder decir «esta factura es del coche»:
    es lo que permite comparar el mantenimiento declarado de un activo con el
    que de verdad ha pasado por el banco."""

    ESTADO_CHOICES = [
        ('sin_categorizar', 'Sin categorizar'),
        ('por_codigo', 'Categorizado por código'),
        ('por_regla', 'Categorizado por una regla aprendida'),
        ('por_ia', 'Categorizado por IA'),
        ('manual', 'Categorizado manualmente'),
    ]

    # En blanco para los apuntes metidos A MANO: un pago en efectivo no viene de
    # ningún archivo, y obligarlo a colgar de un extracto inventado ensuciaría la
    # lista de extractos importados con uno que nadie subió.
    extracto = models.ForeignKey(
        ExtractoBancario, on_delete=models.CASCADE, related_name='movimientos',
        null=True, blank=True,
    )
    # Lo metió el usuario, no salió de un extracto. Se marca en la fila porque
    # saber qué dato viene del banco y cuál has puesto tú es la diferencia entre
    # fiarte de una cifra y tener que comprobarla.
    manual = models.BooleanField(
        default=False,
        help_text='Apunte introducido a mano (un pago en efectivo), no importado.',
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

    etiquetas = models.ManyToManyField(
        Etiqueta, blank=True, related_name='movimientos',
        help_text='Cortes transversales (un viaje, una obra) que cruzan las categorías.',
    )

    # Dinero que sacas de lo que tenías apartado para cubrir un pago concreto.
    # Ahorras todo el año para la revisión del coche; cuando llega, metes esa
    # reserva en la cuenta y el golpe real del mes es solo lo que la reserva no
    # cubrió. Sin esto, la reposición entraba como un traspaso suelto y el pago
    # aparecía entero: un mes que parecía un desastre cuando no lo fue.
    cubre = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True, related_name='coberturas',
        help_text='Pago al que este movimiento aporta dinero de la reserva.',
    )

    # Lo contrario de `cubre`: un pago de un gasto anual que NO salió de la
    # reserva, porque la hucha ya estaba vacía o no la había. Sin emparejar, la
    # pantalla no puede saberlo y supone que lo tenías apartado: lo saca del mes
    # y le carga su doceava parte. Aquí se dice que no, que ese dinero salió
    # del bolsillo este mes y tiene que contar entero en él.
    sin_reserva = models.BooleanField(
        default=False,
        help_text='Pago de un gasto no mensual que no salió de la reserva: cuenta entero en su mes.',
    )

    # Un cobro puede ser varias cosas a la vez: en Norauto pagas de una vez los
    # neumáticos y la revisión anual, y son dos partidas distintas del
    # presupuesto. Dividirlo crea sus partes como movimientos normales colgando
    # de este, que se queda como el apunte real del banco y deja de contar en
    # los totales para no sumar dos veces el mismo dinero.
    dividido_de = models.ForeignKey(
        'self', on_delete=models.CASCADE, null=True, blank=True, related_name='partes',
        help_text='Si está relleno, esto es una parte de otro movimiento.',
    )
    # Qué parte es, dentro de su movimiento. Existe para el hash: dos partes del
    # mismo importe y concepto son legítimas, y sin algo que las distinga
    # chocarían contra el unique (hogar, hash). El pk no sirve porque todavía no
    # existe cuando se calcula el hash en el primer save().
    orden_parte = models.PositiveSmallIntegerField(default=0)

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

    @property
    def computo(self):
        """Cómo entra este movimiento en los totales: 'resta' (gasto), 'suma'
        (ingreso) o 'neutro' (ni una cosa ni la otra).

        Manda la categoría, porque es donde el usuario declara su criterio: un
        traspaso entre cuentas propias, el pago de la tarjeta o un reintegro
        salen en negativo pero no son gasto. Solo cuando el movimiento no tiene
        categoría se cae al signo del importe, que es lo único que se sabe.

        `es_traspaso` va DESPUÉS de la categoría, aunque el orden diga poco a
        primera vista. Es una deducción de la importación —el concepto habla de
        transferencia y menciona a alguien del hogar—, y una deducción no puede
        ganarle a lo que el usuario declara. Mirándolo antes, un movimiento
        marcado como traspaso se quedaba en neutro pasara lo que pasara:
        ponerle «Otros ingresos» no hacía nada, ni quitarle la categoría
        tampoco, y no había forma de sacarlo de ahí. Una transferencia de un
        tercero que se llama como tú es un ingreso, y decirlo tiene que bastar.
        """
        from finanzas.models import COMPUTO_NEUTRO, COMPUTO_RESTA, COMPUTO_SUMA

        if self.categoria_id and self.categoria:
            return self.categoria.computo
        if self.es_traspaso:
            return COMPUTO_NEUTRO
        return COMPUTO_SUMA if self.es_ingreso else COMPUTO_RESTA

    @property
    def es_pago_provision(self):
        """Es uno de los pagos a plazos de un gasto que no es mensual.

        El IBI se provisiona a 43 €/mes pero se paga de golpe en junio. Sin
        marcarlo, junio parece un mes desastroso y los otros once, excelentes:
        el pago hay que sacarlo de la comparación mensual y llevarlo a la del
        año, que es donde encaja."""
        return bool(
            self.partida_conciliada_id
            and self.partida_conciliada
            and self.partida_conciliada.periodicidad != 'mensual'
        )

    @property
    def esta_dividido(self):
        """Se ha repartido en partes, así que el que cuenta es cada parte.

        El apunte original se conserva —es lo que dice el banco, y es lo que
        evita que reimportar el extracto lo duplique— pero deja de sumar, o el
        mismo dinero contaría dos veces.

        Lo mira sobre el prefetch si lo hay: esto se consulta para CADA
        movimiento al calcular los totales, y un `.exists()` por fila devolvía
        la pantalla a las nueve mil consultas de las que acaba de salir.
        """
        cache = getattr(self, '_prefetched_objects_cache', None)
        if cache is not None and 'partes' in cache:
            return bool(cache['partes'])
        return self.partes.exists()

    @property
    def es_parte(self):
        return self.dividido_de_id is not None

    def _coberturas(self):
        cache = getattr(self, '_prefetched_objects_cache', None)
        if cache is not None and 'coberturas' in cache:
            return cache['coberturas']
        return self.coberturas.all()

    @property
    def cubierto_por_reserva(self):
        """Cuánto de este pago salió de dinero apartado, en positivo.

        Una PARTE hereda su porción de lo que se puso sobre el cobro entero.
        La hucha se empareja con el recibo de Norauto —que es lo que hay en el
        banco y lo que uno mira—, no con cada línea de dentro; pero el que
        cuenta en el presupuesto es cada línea, así que el dinero tiene que
        bajar hasta ellas. Se reparte a prorrata del importe de cada una, que
        es la única forma de repartirlo sin inventarse un criterio.
        """
        propio = sum((abs(c.importe) for c in self._coberturas()), Decimal('0'))
        if not self.es_parte:
            return propio

        padre = self.dividido_de
        del_padre = sum((abs(c.importe) for c in padre._coberturas()), Decimal('0'))
        if not del_padre:
            return propio

        hermanas = padre.partes.all()
        total = sum((abs(p.importe) for p in hermanas), Decimal('0'))
        if not total:
            return propio
        porcion = (del_padre * abs(self.importe) / total).quantize(Decimal('0.01'))
        return propio + porcion

    @property
    def pagado_sin_reserva(self):
        """Se dijo que este pago no salió de la reserva, en él o en su cobro.

        Como con la reserva, se marca donde uno mira —el recibo de Norauto o la
        línea de la revisión—, y una parte hereda lo que se dijera del cobro."""
        if self.sin_reserva:
            return True
        return bool(self.es_parte and self.dividido_de and self.dividido_de.sin_reserva)

    @property
    def se_saca_del_mes(self):
        """Un pago anual que el mes en el que cae no carga: se reparte en el año.

        Es el que no se ha emparejado con la reserva ni se ha dicho que se pagó
        sin ella. Sin saber de dónde salió el dinero se supone que estaba
        apartado, que es lo que se hace al provisionar."""
        return bool(
            self.es_pago_provision and self.cuenta_como_gasto
            and not self.cubierto_por_reserva and not self.pagado_sin_reserva
        )

    @property
    def impacto_real(self):
        """Lo que de verdad pesó en el mes: el pago menos lo que puso la reserva.

        Va en las mismas unidades que `-importe`, es decir, el gasto en
        positivo. Eso importa porque una devolución dentro de una categoría de
        gasto llega en positivo y tiene que seguir RESTANDO de su categoría: con
        un abs() aquí, devolver 30 € se contaba como gastar 30 €.

        Cubrir de más no convierte un gasto en ingreso: se queda en cero."""
        bruto = -self.importe
        cubierto = self.cubierto_por_reserva
        if bruto <= 0 or not cubierto:
            return bruto
        return max(bruto - cubierto, Decimal('0'))

    @property
    def es_cobertura(self):
        return self.cubre_id is not None

    @property
    def es_neutro(self):
        from finanzas.models import COMPUTO_NEUTRO

        # Una reposición de la reserva no es ingreso: es dinero tuyo cambiando
        # de sitio. Lo que hace es rebajar el pago que cubre.
        if self.es_cobertura:
            return True
        return self.computo == COMPUTO_NEUTRO

    @property
    def cuenta_como_gasto(self):
        from finanzas.models import COMPUTO_RESTA

        return self.computo == COMPUTO_RESTA and not self.esta_dividido

    @property
    def cuenta_como_ingreso(self):
        from finanzas.models import COMPUTO_SUMA

        # Sacar dinero de la hucha no es ganarlo: entra en la cuenta, sí, pero
        # es ahorro tuyo volviendo al flujo para pagar algo concreto. Contarlo
        # como ingreso inflaría el mes y, si el pago está imputado a un coche,
        # haría que el coche pareciera que renta.
        if self.es_cobertura:
            return False

        return self.computo == COMPUTO_SUMA and not self.esta_dividido

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
    def calcular_hash(fecha, concepto, importe, saldo, parte_de=None, orden=None):
        base = '|'.join([
            str(fecha),
            (concepto or '').strip().lower(),
            MovimientoBancario._importe_canonico(importe),
            MovimientoBancario._importe_canonico(saldo),
        ])
        # Las partes de un movimiento dividido no vienen del banco, así que no
        # hay nada que deduplicar entre ellas: dos partes del mismo importe y
        # concepto son legítimas y el unique (hogar, hash) las rechazaría. Se
        # les añade de quién son parte y cuál es.
        if parte_de is not None:
            base += f'|parte:{parte_de}:{orden}'
        return hashlib.sha256(base.encode('utf-8')).hexdigest()

    def save(self, *args, **kwargs):
        if not self.comercio:
            self.comercio = normalizar_comercio(self.concepto)
        if self.manual:
            # Un apunte a mano no se deduplica contra nada: no va a llegar en
            # ningún extracto. Y dos cafés de 1,50 € el mismo día son dos cafés,
            # no un duplicado: con el hash normal el segundo chocaría contra el
            # unique (hogar, hash) y no se podría guardar. Se le da una huella
            # propia la primera vez y ya no se toca.
            if not self.hash_dedupe:
                self.hash_dedupe = uuid4().hex
        else:
            # El hash se recalcula siempre a partir de los valores actuales: si
            # solo se rellenara cuando está vacío, editar el concepto o el
            # importe dejaría un hash obsoleto y la deduplicación de futuras
            # importaciones compararía contra datos que ya no existen.
            self.hash_dedupe = self.calcular_hash(
                self.fecha, self.concepto, self.importe, self.saldo,
                parte_de=self.dividido_de_id, orden=self.orden_parte,
            )
        update_fields = kwargs.get('update_fields')
        if update_fields is not None:
            kwargs['update_fields'] = set(update_fields) | {'comercio', 'hash_dedupe'}
        super().save(*args, **kwargs)
