from django import forms
from .models import SaldoMensualCuenta, CuentaBancaria
import datetime
from .models import TarjetaCredito
from .models import SaldoMensualTarjeta


class CuentaBancariaForm(forms.ModelForm):
    class Meta:
        model = CuentaBancaria
        fields = ['nombre', 'moneda', 'activa']
        widgets = {
            'nombre': forms.TextInput(attrs={'class': 'form-control'}),
            'moneda': forms.Select(attrs={'class': 'form-control'}),
            'activa': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

class SaldoMensualCuentaForm(forms.ModelForm):
    mes = forms.ChoiceField(choices=[(i, i) for i in range(1, 13)], label="Mes")
    anio = forms.ChoiceField(label="Año")

    class Meta:
        model = SaldoMensualCuenta
        fields = ['saldo']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        hoy = datetime.date.today()

        # Años: del actual hacia atrás hasta 5 años
        self.fields['anio'].choices = [(a, a) for a in range(hoy.year, hoy.year - 6, -1)]

        # Preselección por defecto
        if not self.initial.get('mes'):
            self.initial['mes'] = hoy.month
        if not self.initial.get('anio'):
            self.initial['anio'] = hoy.year

class TarjetaCreditoForm(forms.ModelForm):
    class Meta:
        model = TarjetaCredito
        fields = ['nombre', 'entidad', 'activa']
        widgets = {
            'nombre': forms.TextInput(attrs={'class': 'form-control'}),
            'entidad': forms.TextInput(attrs={'class': 'form-control'}),
            'activa': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

class SaldoMensualTarjetaForm(forms.ModelForm):
    mes = forms.ChoiceField(choices=[(i, i) for i in range(1, 13)], label="Mes")
    anio = forms.ChoiceField(label="Año")

    class Meta:
        model = SaldoMensualTarjeta
        fields = ['saldo']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        hoy = datetime.date.today()
        self.fields['anio'].choices = [(a, a) for a in range(hoy.year, hoy.year - 6, -1)]
        self.initial.setdefault('mes', hoy.month)
        self.initial.setdefault('anio', hoy.year)
