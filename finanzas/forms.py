from django import forms
from .models import SaldoMensualCuenta, CuentaBancaria
import datetime
from .models import TarjetaCredito
from .models import SaldoMensualTarjeta
from django import forms
from .models import Inversion, MovimientoInversion


### Modulo principal ###

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

### Sub-modulo Inversiones ###
from django import forms
from .models import Inversion

class InversionForm(forms.ModelForm):
    valor_unitario_manual = forms.DecimalField(
        required=False,
        label="Valor unitario (manual)",
        help_text="Solo si no está marcada la casilla de actualización automática"
    )

    class Meta:
        model = Inversion
        exclude = ['usuario', 'fecha_creacion']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Si estamos editando una inversión existente con valor manual
        instance = kwargs.get('instance')
        if instance and not instance.actualizable:
            try:
                self.fields['valor_unitario_manual'].initial = instance.valor_actual.valor_unitario
            except:
                pass

class MovimientoInversionForm(forms.ModelForm):
    class Meta:
        model = MovimientoInversion
        exclude = []
