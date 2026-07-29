from django import forms

from .models import AgenteIA, ConfiguracionIA


class ConfiguracionIAForm(forms.ModelForm):
    """Las claves API se editan como campos de solo-escritura: nunca se
    precargan con el valor existente (están cifradas) y solo se sobrescriben
    si el usuario introduce un valor nuevo."""

    anthropic_api_key = forms.CharField(
        label='Clave API de Claude (Anthropic)', required=False,
        widget=forms.PasswordInput(render_value=False, attrs={'autocomplete': 'off'}),
    )
    openai_api_key = forms.CharField(
        label='Clave API de ChatGPT (OpenAI)', required=False,
        widget=forms.PasswordInput(render_value=False, attrs={'autocomplete': 'off'}),
    )
    gemini_api_key = forms.CharField(
        label='Clave API de Gemini (Google)', required=False,
        widget=forms.PasswordInput(render_value=False, attrs={'autocomplete': 'off'}),
    )

    class Meta:
        model = ConfiguracionIA
        fields = [
            'proveedor_activo',
            'anthropic_modelo', 'openai_modelo', 'gemini_modelo',
        ]

    def save(self, commit=True):
        config = super().save(commit=False)
        for proveedor in ('anthropic', 'openai', 'gemini'):
            nueva_clave = self.cleaned_data.get(f'{proveedor}_api_key')
            if nueva_clave:
                config.set_api_key(proveedor, nueva_clave)
        if commit:
            config.save()
        return config


class AgenteIAForm(forms.ModelForm):
    class Meta:
        model = AgenteIA
        fields = ['nombre', 'descripcion', 'instrucciones', 'proveedor_preferido', 'activo']
        widgets = {
            'instrucciones': forms.Textarea(attrs={'rows': 6}),
            'descripcion': forms.TextInput(),
        }
