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

from .models import Hogar

class CrearUsuarioDesdeAdminForm(forms.Form):
    username = forms.CharField(label='Nombre de usuario', max_length=150)
    first_name = forms.CharField(label='Nombre', max_length=30)
    email = forms.EmailField(label='Correo electrónico')
    password = forms.CharField(label='Contraseña', widget=forms.PasswordInput)
    hogar = forms.ModelChoiceField(
        queryset=Hogar.objects.all(),
        required=False,
        label='Hogar',
        empty_label='— Sin asignar —'
    )
    rol = forms.ChoiceField(
        choices=UserProfile.ROL_CHOICES,
        initial='miembro',
        label='Rol'
    )

    def clean_username(self):
        username = self.cleaned_data['username']
        if User.objects.filter(username=username).exists():
            raise forms.ValidationError('Este nombre de usuario ya existe.')
        return username

    def clean_email(self):
        email = self.cleaned_data['email']
        if User.objects.filter(email=email).exists():
            raise forms.ValidationError('Ya existe un usuario con este email.')
        return email