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
    fondo = models.ForeignKey(
        'FondoFamiliar', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='inversiones',
        help_text="Fondo de inversión al que pertenece este activo.",
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


class UserProfile(models.Model):
    ROL_CHOICES = [
        ('admin', 'Administrador'),
        ('miembro', 'Miembro'),
        ('viewer', 'Solo lectura'),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE)
    hogar = models.ForeignKey(
        Hogar, on_delete=models.SET_NULL, null=True, blank=True, related_name='miembros'
    )
    rol = models.CharField(max_length=20, choices=ROL_CHOICES, default='miembro')

    idioma = models.CharField(
        max_length=10, choices=[('es', 'Español'), ('en', 'Inglés')], default='es'
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
    
