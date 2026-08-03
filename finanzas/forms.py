from .models import SaldoMensualCuenta, CuentaBancaria
import datetime
from .models import TarjetaCredito
from .models import SaldoMensualTarjeta
from django import forms
from .models import Inversion, MovimientoInversion, FondoFamiliar, GrupoInversion


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
class InversionForm(forms.ModelForm):
    valor_unitario_manual = forms.DecimalField(
        required=False,
        label="Valor unitario (manual)",
        help_text="Solo si no está marcada la casilla de actualización automática"
    )

    class Meta:
        model = Inversion
        # ANTES: exclude = ['usuario', 'fecha_creacion']
        # AHORA: también excluir cantidad_actual
        exclude = ['usuario', 'fecha_creacion', 'cantidad_actual']

    def __init__(self, *args, hogar=None, usuario=None, **kwargs):
        super().__init__(*args, **kwargs)
        instance = kwargs.get('instance')

        if hogar:
            fondo_qs = FondoFamiliar.objects.filter(hogar=hogar, tipo_fondo='inversion', activo=True)
        else:
            fondo_qs = FondoFamiliar.objects.none()
        # Asegura que el fondo actualmente asignado siempre aparezca (aunque hoy
        # esté inactivo o filtrado), para que la edición recupere el valor.
        if instance and instance.fondo_id:
            fondo_qs = (fondo_qs | FondoFamiliar.objects.filter(id=instance.fondo_id)).distinct()
        self.fields['fondo'].queryset = fondo_qs
        self.fields['fondo'].required = False
        self.fields['fondo'].empty_label = '— Sin fondo asignado —'

        if usuario is not None:
            grupo_qs = GrupoInversion.objects.filter(usuario=usuario)
        else:
            grupo_qs = GrupoInversion.objects.none()
        if instance and instance.grupo_id:
            grupo_qs = (grupo_qs | GrupoInversion.objects.filter(id=instance.grupo_id)).distinct()
        self.fields['grupo'].queryset = grupo_qs
        self.fields['grupo'].required = False
        self.fields['grupo'].empty_label = '— Sin cartera asignada —'

        if instance and not instance.actualizable:
            try:
                self.fields['valor_unitario_manual'].initial = instance.valor_actual.valor_unitario
            except AttributeError:
                pass



class MovimientoInversionForm(forms.ModelForm):
    class Meta:
        model = MovimientoInversion
        fields = ['fecha', 'tipo', 'cantidad', 'precio_unitario', 'comision', 'grupo']
        widgets = {
            'fecha': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
            'tipo': forms.Select(attrs={'class': 'form-control'}),
            'cantidad': forms.NumberInput(attrs={'step': '0.0001', 'class': 'form-control'}),
            'precio_unitario': forms.NumberInput(attrs={'step': '0.0001', 'class': 'form-control'}),
            'comision': forms.NumberInput(attrs={'step': '0.01', 'class': 'form-control'}),
        }

    def __init__(self, *args, usuario=None, **kwargs):
        super().__init__(*args, **kwargs)
        if usuario is not None:
            self.fields['grupo'].queryset = GrupoInversion.objects.filter(usuario=usuario)
        else:
            self.fields['grupo'].queryset = GrupoInversion.objects.none()
        self.fields['grupo'].required = False
        self.fields['grupo'].empty_label = '— Sin cartera asignada —'
