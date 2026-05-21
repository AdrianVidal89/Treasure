from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from functools import wraps


class HogarRequiredMixin(LoginRequiredMixin):
    """Verifica que el usuario pertenece a un hogar."""

    def dispatch(self, request, *args, **kwargs):
        response = super().dispatch(request, *args, **kwargs)
        if request.user.is_authenticated:
            profile = getattr(request.user, 'userprofile', None)
            if not profile or not profile.hogar:
                raise PermissionDenied("No perteneces a ningún hogar.")
        return response


class AdminRequiredMixin(HogarRequiredMixin):
    """Verifica que el usuario es admin de su hogar."""

    def dispatch(self, request, *args, **kwargs):
        response = super().dispatch(request, *args, **kwargs)
        if request.user.is_authenticated:
            profile = getattr(request.user, 'userprofile', None)
            if not profile or not profile.es_admin:
                raise PermissionDenied("Necesitas ser administrador para acceder.")
        return response


class ViewerBlockedMixin(HogarRequiredMixin):
    """Bloquea acceso a viewers (solo lectura)."""

    def dispatch(self, request, *args, **kwargs):
        response = super().dispatch(request, *args, **kwargs)
        if request.user.is_authenticated:
            profile = getattr(request.user, 'userprofile', None)
            if profile and profile.es_viewer:
                raise PermissionDenied("No tienes permisos para realizar esta acción.")
        return response


def hogar_required(view_func):
    """Decorador equivalente a HogarRequiredMixin para vistas de función."""
    @login_required
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        profile = getattr(request.user, 'userprofile', None)
        if not profile or not profile.hogar:
            raise PermissionDenied("No perteneces a ningún hogar.")
        return view_func(request, *args, **kwargs)
    return wrapper