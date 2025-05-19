from django import forms
from .models import SaldoMensualCuenta, CuentaBancaria

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
    class Meta:
        model = SaldoMensualCuenta
        fields = ['saldo']

    def __init__(self, *args, **kwargs):
        kwargs.pop('usuario', None)  # Ya no se necesita en esta versión
        super().__init__(*args, **kwargs)
