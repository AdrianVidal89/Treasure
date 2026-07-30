from django import forms

from .acciones import ACCIONES
from .models import AgenteIA


class AgenteIAForm(forms.ModelForm):
    allowed_tools = forms.MultipleChoiceField(
        choices=[(tipo, definicion.descripcion) for tipo, definicion in ACCIONES.items()],
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label='Acciones de escritura permitidas',
        help_text=(
            'Si no marcas ninguna, el agente será de solo lectura — las consultas '
            '(search, get_item, query, count, búsqueda web…) siempre están disponibles '
            'para cualquier agente; esto solo controla qué puede proponer.'
        ),
    )

    class Meta:
        model = AgenteIA
        fields = ['nombre', 'descripcion', 'instrucciones', 'proveedor_preferido', 'activo', 'allowed_tools']
        widgets = {
            'instrucciones': forms.Textarea(attrs={'rows': 6}),
            'descripcion': forms.TextInput(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields['allowed_tools'].initial = self.instance.allowed_tools

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.allowed_tools = self.cleaned_data.get('allowed_tools', [])
        if commit:
            instance.save()
        return instance
