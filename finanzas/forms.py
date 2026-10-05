from .models import SaldoMensualCuenta, CuentaBancaria
import datetime
from .models import TarjetaCredito
from .models import SaldoMensualTarjeta
from django import forms
from .models import Inversion, MovimientoInversion, FondoFamiliar, GrupoInversion, AportacionRecurrente


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

        # Todos los fondos activos del hogar: los de inversión sirven para la
        # cartera de mercado y los de ahorro/común para vincular un depósito
        # (así el depósito hereda las reglas/transferencias de ese fondo).
        if hogar:
            fondo_qs = FondoFamiliar.objects.filter(hogar=hogar, activo=True)
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


class AportacionRecurrenteForm(forms.ModelForm):
    class Meta:
        model = AportacionRecurrente
        fields = ['importe', 'dia_mes', 'fecha_inicio', 'fecha_fin', 'activa']
        widgets = {
            'importe': forms.NumberInput(attrs={'step': '0.01', 'min': '0'}),
            'dia_mes': forms.NumberInput(attrs={'min': '1', 'max': '28'}),
            'fecha_inicio': forms.DateInput(attrs={'type': 'date'}),
            'fecha_fin': forms.DateInput(attrs={'type': 'date'}),
        }


class MovimientoDepositoForm(forms.Form):
    """Alta simple de un flujo de un depósito (aportación o retirada), pensada
    en 'importe €' en vez de cantidad×precio. Internamente crea un
    MovimientoInversion (COMPRA/VENTA con precio unitario = 1)."""
    TIPO_CHOICES = [('COMPRA', 'Aportación'), ('VENTA', 'Retirada')]
    fecha = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))
    tipo = forms.ChoiceField(choices=TIPO_CHOICES)
    importe = forms.DecimalField(min_value=0, decimal_places=2,
                                 widget=forms.NumberInput(attrs={'step': '0.01', 'min': '0'}))


### Hipotecas de una propiedad ###

from decimal import Decimal, InvalidOperation  # noqa: E402

from .models import AmortizacionAnticipada, Hipoteca, PartidaGasto, RevisionTipo  # noqa: E402


def _numero(**attrs):
    return forms.NumberInput(attrs={'step': 'any', **attrs})


def _fecha():
    return forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d')


class HipotecaForm(forms.ModelForm):
    """Las condiciones del préstamo. Las bonificaciones van en un texto, una
    por línea: «Nómina; 0,30; 0» (concepto; rebaja del tipo en puntos; coste
    al año)."""
    bonificaciones_texto = forms.CharField(
        label='Bonificaciones (informativo)', required=False,
        widget=forms.Textarea(attrs={'rows': 3, 'placeholder': 'Nómina; 0,30; 0\nSeguro de hogar; 0,10; 320'}),
        help_text='Una por línea: concepto; rebaja del tipo (puntos); coste al año (€).',
    )

    class Meta:
        model = Hipoteca
        fields = [
            'nombre', 'entidad', 'capital_inicial', 'fecha_firma', 'plazo_meses', 'dia_cobro',
            'modalidad', 'tipo_inicial_pct', 'meses_tramo_fijo', 'indice', 'diferencial_pct',
            'revision_meses', 'saldo_conocido', 'saldo_conocido_fecha', 'cuota_conocida',
            'comision_pct_inicial', 'comision_meses_iniciales', 'comision_pct_despues',
            'partida', 'activa',
        ]
        labels = {
            'nombre': 'Nombre', 'entidad': 'Banco', 'capital_inicial': 'Capital prestado (€)',
            'fecha_firma': 'Fecha de firma', 'plazo_meses': 'Plazo (meses)', 'dia_cobro': 'Día de cobro',
            'modalidad': 'Tipo de interés', 'tipo_inicial_pct': 'Tipo inicial (TIN %)',
            'meses_tramo_fijo': 'Meses del tramo fijo', 'indice': 'Índice',
            'diferencial_pct': 'Diferencial (%)', 'revision_meses': 'Revisión cada (meses)',
            'saldo_conocido': 'Saldo pendiente (€)', 'saldo_conocido_fecha': 'a fecha de',
            'cuota_conocida': 'Cuota mensual en esa fecha (€)',
            'comision_pct_inicial': 'Comisión al principio (%)',
            'comision_meses_iniciales': 'Durante los primeros (meses)',
            'comision_pct_despues': 'Comisión después (%)',
            'partida': 'Partida de gasto de la cuota', 'activa': 'Activa (no cancelada)',
        }
        widgets = {
            'capital_inicial': _numero(min=0, placeholder='Ej: 323400'),
            'fecha_firma': _fecha(),
            'plazo_meses': _numero(min=1, step=1, placeholder='30 años = 360'),
            'dia_cobro': _numero(min=1, max=31, step=1, placeholder='El de la firma'),
            'tipo_inicial_pct': _numero(placeholder='Ej: 2,00'),
            'meses_tramo_fijo': _numero(min=0, step=1, placeholder='Ej: 60'),
            'diferencial_pct': _numero(placeholder='Ej: 0,79'),
            'revision_meses': forms.Select(choices=[('', '—'), (6, 'Cada 6 meses'), (12, 'Cada 12 meses')]),
            'saldo_conocido': _numero(min=0),
            'saldo_conocido_fecha': _fecha(),
            'cuota_conocida': _numero(min=0),
            'comision_pct_inicial': _numero(min=0, placeholder='Por ley'),
            'comision_meses_iniciales': _numero(min=0, step=1, placeholder='Por ley'),
            'comision_pct_despues': _numero(min=0, placeholder='Por ley'),
        }

    def __init__(self, *args, hogar=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['partida'].queryset = (
            PartidaGasto.objects.filter(hogar=hogar, activo=True).order_by('nombre')
            if hogar else PartidaGasto.objects.none()
        )
        self.fields['partida'].required = False
        self.fields['partida'].help_text = (
            'Opcional. Para conciliar: los pagos conciliados con ella cuentan como cuota.'
        )
        if self.instance and self.instance.pk:
            self.fields['bonificaciones_texto'].initial = '\n'.join(
                '; '.join(str(b.get(k, '')).replace('.', ',') for k in ('concepto', 'rebaja_pct', 'coste_anual'))
                for b in self.instance.bonificaciones or []
            )

    def clean_bonificaciones_texto(self):
        bonificaciones = []
        for n, linea in enumerate((self.cleaned_data.get('bonificaciones_texto') or '').splitlines(), 1):
            if not linea.strip():
                continue
            partes = [p.strip() for p in linea.split(';')] + ['', '']
            try:
                rebaja = float(Decimal(partes[1].replace(',', '.') or '0'))
                coste = float(Decimal(partes[2].replace(',', '.') or '0'))
            except InvalidOperation:
                raise forms.ValidationError(f'Línea {n}: «{linea}» no es «concepto; rebaja; coste».')
            bonificaciones.append({'concepto': partes[0], 'rebaja_pct': rebaja, 'coste_anual': coste})
        return bonificaciones

    def save(self, commit=True):
        self.instance.bonificaciones = self.cleaned_data.get('bonificaciones_texto') or []
        return super().save(commit)


class RevisionTipoForm(forms.ModelForm):
    class Meta:
        model = RevisionTipo
        fields = ['fecha_desde', 'tipo_pct', 'valor_indice_pct']
        labels = {'fecha_desde': 'Desde', 'tipo_pct': 'Tipo (TIN %)', 'valor_indice_pct': 'Índice (%)'}
        widgets = {'fecha_desde': _fecha(), 'tipo_pct': _numero(), 'valor_indice_pct': _numero()}


class AmortizacionAnticipadaForm(forms.ModelForm):
    class Meta:
        model = AmortizacionAnticipada
        fields = ['fecha', 'importe', 'modo', 'comision_pagada']
        labels = {'fecha': 'Fecha', 'importe': 'Importe (€)', 'modo': 'Para',
                  'comision_pagada': 'Comisión pagada (€)'}
        widgets = {'fecha': _fecha(), 'importe': _numero(min=0),
                   'comision_pagada': _numero(min=0, placeholder='Se estima')}

    def clean_importe(self):
        importe = self.cleaned_data['importe']
        if importe is None or importe <= 0:
            raise forms.ValidationError('El importe tiene que ser mayor que cero.')
        return importe
