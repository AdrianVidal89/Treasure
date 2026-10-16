from django.db import models
from django.contrib.auth.models import User

class Hogar(models.Model):
    nombre = models.CharField(max_length=100)
    moneda_principal = models.CharField(
        max_length=5,
        choices=[('EUR', '€ Euro'), ('USD', '$ Dólar'), ('BTC', '₿ Bitcoin')],
        default='EUR'
    )
    creado_por = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name='hogares_creados'
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.nombre


class UserProfile(models.Model):
    ROL_CHOICES = [
        ('admin', 'Administrador'),
        ('miembro', 'Miembro'),
        ('viewer', 'Solo lectura'),
    ]

    TEMA_CHOICES = [
        ('claro', 'Claro'),
        ('black', 'Black'),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE)
    hogar = models.ForeignKey(
        Hogar, on_delete=models.SET_NULL, null=True, blank=True, related_name='miembros'
    )
    rol = models.CharField(max_length=20, choices=ROL_CHOICES, default='miembro')

    idioma = models.CharField(
        max_length=10, choices=[('es', 'Español'), ('en', 'Inglés')], default='es'
    )
    tema = models.CharField(
        max_length=10, choices=TEMA_CHOICES, default='claro',
        verbose_name='Tema de la interfaz'
    )
    moneda = models.CharField(
        max_length=5,
        choices=[('EUR', '€ Euro'), ('USD', '$ Dólar'), ('BTC', '₿ Bitcoin')],
        default='EUR'
    )
    inflacion_referencia = models.FloatField(default=2.0)
    porcentaje_max_endeudamiento = models.FloatField(default=30.0)
    permitir_apis_externas = models.BooleanField(default=False)
    mostrar_alertas = models.BooleanField(default=True)
    recibir_emails = models.BooleanField(default=False)
    avatar = models.ImageField(upload_to='avatars/', null=True, blank=True)

    def __str__(self):
        return f"{self.user.username} ({self.get_rol_display()})"

    @property
    def es_admin(self):
        return self.rol == 'admin'

    @property
    def es_viewer(self):
        return self.rol == 'viewer'


class ConsultaSQL(models.Model):
    """Registro de lo ejecutado en la consola SQL del panel de administración.

    Una consola SQL sin rastro es una caja negra: si algo se rompe, esto es lo
    que permite saber qué se hizo y cuándo."""
    usuario = models.ForeignKey(User, on_delete=models.SET_NULL, null=True,
                                related_name='consultas_sql')
    sql = models.TextField()
    verbo = models.CharField(max_length=20, blank=True, default='')
    aplicado = models.BooleanField(
        default=False,
        help_text='False = solo se probó (se deshizo); True = se aplicó de verdad.')
    filas_afectadas = models.IntegerField(null=True, blank=True)
    error = models.TextField(blank=True, default='')
    ejecutado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-ejecutado_en']
        verbose_name = 'Consulta SQL'
        verbose_name_plural = 'Consultas SQL'

    def __str__(self):
        estado = 'aplicada' if self.aplicado else 'prueba'
        return f"{self.verbo} ({estado}) · {self.ejecutado_en:%d/%m/%Y %H:%M}"


class CopiaDriveConfig(models.Model):
    """Configuración de la copia automática de la base de datos a Google Drive.

    Es un registro único (pk=1): la base de datos es una sola, así que su copia
    también. Los secretos (client secret y refresh token) se guardan cifrados
    con la misma clave Fernet que las claves de IA.
    """
    PERIODICIDAD_CHOICES = [
        ('desactivada', 'Desactivada'),
        ('diaria', 'Cada día'),
        ('semanal', 'Cada semana'),
        ('mensual', 'Cada mes'),
    ]
    DIAS_SEMANA = [
        (0, 'Lunes'), (1, 'Martes'), (2, 'Miércoles'), (3, 'Jueves'),
        (4, 'Viernes'), (5, 'Sábado'), (6, 'Domingo'),
    ]

    # Credenciales OAuth del proyecto de Google Cloud (cliente "TV y dispositivos
    # de entrada limitada"). Si se dejan vacías se usan las del .env.
    client_id = models.CharField(max_length=255, blank=True, default='')
    client_secret_cifrado = models.BinaryField(null=True, blank=True)

    # Cuenta conectada
    refresh_token_cifrado = models.BinaryField(null=True, blank=True)
    cuenta_email = models.CharField(max_length=255, blank=True, default='')
    conectado_en = models.DateTimeField(null=True, blank=True)

    # Conexión en curso (flujo de dispositivo): se guarda aquí para poder
    # consultarla desde otra petición mientras el usuario mete el código.
    device_code = models.CharField(max_length=512, blank=True, default='')
    device_user_code = models.CharField(max_length=64, blank=True, default='')
    device_url = models.CharField(max_length=255, blank=True, default='')
    device_expira = models.DateTimeField(null=True, blank=True)
    device_intervalo = models.PositiveSmallIntegerField(default=5)

    # Destino
    carpeta_ruta = models.CharField(
        max_length=255, default='Treasure/Copias de seguridad',
        help_text='Ruta de carpetas dentro de Mi unidad, separadas por /.')
    carpeta_id = models.CharField(max_length=128, blank=True, default='')
    carpeta_ruta_creada = models.CharField(max_length=255, blank=True, default='')

    # Programación
    periodicidad = models.CharField(max_length=12, choices=PERIODICIDAD_CHOICES, default='desactivada')
    hora = models.PositiveSmallIntegerField(default=3, help_text='Hora del día (0-23).')
    dia_semana = models.PositiveSmallIntegerField(choices=DIAS_SEMANA, default=0)
    dia_mes = models.PositiveSmallIntegerField(default=1, help_text='Día del mes (1-28).')
    conservar = models.PositiveSmallIntegerField(
        default=10, help_text='Copias que se guardan en Drive; las más antiguas se borran. 0 = todas.')

    # Estado
    ultima_copia_en = models.DateTimeField(null=True, blank=True)
    en_curso_desde = models.DateTimeField(null=True, blank=True)
    actualizado_en = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Copia a Google Drive'

    def __str__(self):
        return f"Copia a Drive ({self.get_periodicidad_display()})"

    @classmethod
    def obtener(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def conectado(self):
        return bool(self.refresh_token_cifrado)


class CopiaDriveRegistro(models.Model):
    """Una copia hecha (o intentada): el historial que se ve en Ajustes."""
    ORIGEN_CHOICES = [('auto', 'Automática'), ('manual', 'Manual')]

    fecha = models.DateTimeField(auto_now_add=True)
    origen = models.CharField(max_length=10, choices=ORIGEN_CHOICES, default='auto')
    ok = models.BooleanField(default=False)
    nombre_archivo = models.CharField(max_length=255, blank=True, default='')
    drive_file_id = models.CharField(max_length=128, blank=True, default='')
    tamano = models.BigIntegerField(default=0)
    duracion_seg = models.FloatField(default=0)
    mensaje = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-fecha']

    def __str__(self):
        return f"{self.fecha:%Y-%m-%d %H:%M} {'OK' if self.ok else 'ERROR'} {self.nombre_archivo}"
