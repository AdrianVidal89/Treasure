from django.urls import path
from . import views

urlpatterns = [
    path('register/', views.register_view, name='register'),
    path('perfil/editar/', views.editar_perfil, name='editar_perfil'),
    path('admin-panel/', views.panel_admin, name='panel_admin'),
    path('admin-panel/crear-hogar/', views.crear_hogar, name='crear_hogar'),
    path('admin-panel/crear-usuario/', views.crear_usuario, name='crear_usuario'),
    path('admin-panel/asignar-usuario/', views.asignar_usuario, name='asignar_usuario'),
    path('admin-panel/hogar/<int:hogar_id>/editar/', views.editar_hogar, name='editar_hogar'),
    path('admin-panel/miembro/<int:profile_id>/eliminar/', views.eliminar_miembro, name='eliminar_miembro'),
    path('mi-hogar/', views.mi_hogar, name='mi_hogar'),
    path('admin-panel/hogar/<int:hogar_id>/eliminar/', views.eliminar_hogar, name='eliminar_hogar'),
]