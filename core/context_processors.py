"""Contexto global de UI (tema de la interfaz)."""

from .models import UserProfile

TEMA_POR_DEFECTO = 'claro'
TEMAS_VALIDOS = {clave for clave, _ in UserProfile.TEMA_CHOICES}


def tema_context(request):
    """Expone el tema elegido por el usuario a todas las plantillas.

    El valor se pinta en <html data-theme="…"> para que la hoja de estilos
    aplique la paleta correcta ya en el primer render, sin parpadeo.
    """
    tema = TEMA_POR_DEFECTO
    usuario = getattr(request, 'user', None)
    if usuario is not None and usuario.is_authenticated:
        profile = getattr(usuario, 'userprofile', None)
        if profile and profile.tema in TEMAS_VALIDOS:
            tema = profile.tema
    return {'tema_actual': tema, 'temas_disponibles': UserProfile.TEMA_CHOICES}
