from django.db import models
from django.contrib.auth.models import User

class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)

    idioma = models.CharField(max_length=10, choices=[('es', 'Español'), ('en', 'Inglés')], default='es')
    moneda = models.CharField(max_length=5, choices=[('EUR', '€ Euro'), ('USD', '$ Dólar'), ('BTC', '₿ Bitcoin')], default='EUR')
    inflacion_referencia = models.FloatField(default=2.0)
    porcentaje_max_endeudamiento = models.FloatField(default=30.0)
    permitir_apis_externas = models.BooleanField(default=False)
    mostrar_alertas = models.BooleanField(default=True)
    recibir_emails = models.BooleanField(default=False)
    avatar = models.ImageField(upload_to='avatars/', null=True, blank=True)

    def __str__(self):
        return f"Perfil de {self.user.username}"
