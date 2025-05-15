from django.urls import path
from . import views
from .views import register_view
from .views import editar_perfil


urlpatterns = [
    path('', views.index, name='core-home'),
        path('register/', register_view, name='register'),
        path('perfil/editar/', editar_perfil, name='editar_perfil'),
]