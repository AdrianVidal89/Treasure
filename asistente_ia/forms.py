from django import forms

from .models import AgenteIA


class AgenteIAForm(forms.ModelForm):
    class Meta:
        model = AgenteIA
        fields = ['nombre', 'descripcion', 'instrucciones', 'proveedor_preferido', 'activo']
        widgets = {
            'instrucciones': forms.Textarea(attrs={'rows': 6}),
            'descripcion': forms.TextInput(),
        }
