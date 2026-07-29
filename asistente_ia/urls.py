from django.urls import path

from . import views

app_name = 'asistente_ia'

urlpatterns = [
    path('', views.landing, name='landing'),
    path('configuracion/', views.configuracion, name='configuracion'),

    path('agentes/', views.listar_agentes, name='listar_agentes'),
    path('agentes/nuevo/', views.crear_agente, name='crear_agente'),
    path('agentes/<int:agente_id>/editar/', views.editar_agente, name='editar_agente'),
    path('agentes/<int:agente_id>/eliminar/', views.eliminar_agente, name='eliminar_agente'),

    path('api/conversaciones/', views.api_listar_conversaciones, name='api_listar_conversaciones'),
    path('api/conversaciones/nueva/', views.api_crear_conversacion, name='api_crear_conversacion'),
    path('api/conversaciones/<int:conv_id>/mensajes/', views.api_mensajes, name='api_mensajes'),
    path('api/acciones/<int:accion_id>/confirmar/', views.api_confirmar_accion, name='api_confirmar_accion'),
    path('api/acciones/<int:accion_id>/rechazar/', views.api_rechazar_accion, name='api_rechazar_accion'),
]
