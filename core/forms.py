from django import forms
from django.contrib.auth.models import User
from django.contrib.auth.forms import UserCreationForm
from .models import UserProfile

class UserRegisterForm(UserCreationForm):
    first_name = forms.CharField(label='Nombre', max_length=30)
    email = forms.EmailField(label='Correo electrónico', required=True)

    class Meta:
        model = User
        fields = ['username', 'first_name', 'email', 'password1', 'password2']
        help_texts = {
            'username': '',
            'password1': '',
            'password2': '',
        }

class UserProfileForm(forms.ModelForm):
    class Meta:
        model = UserProfile
        fields = [
            'idioma', 'moneda', 'inflacion_referencia',
            'porcentaje_max_endeudamiento',
            'permitir_apis_externas',
            'mostrar_alertas', 'recibir_emails',
            'avatar'
        ]
