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

    path('api/proveedores/<str:proveedor>/clave/', views.api_guardar_clave, name='api_guardar_clave'),
    path('api/proveedores/<str:proveedor>/modelos/', views.api_listar_modelos, name='api_listar_modelos'),
    path('api/proveedores/<str:proveedor>/modelo/', views.api_guardar_modelo, name='api_guardar_modelo'),
    path('api/proveedores/<str:proveedor>/activar/', views.api_activar_proveedor, name='api_activar_proveedor'),
    path('api/proveedores/<str:proveedor>/desconectar/', views.api_desconectar_proveedor, name='api_desconectar_proveedor'),

    path('api/agentes/', views.api_agentes, name='api_agentes'),
    path('api/conversaciones/<int:conv_id>/agente/', views.api_cambiar_agente, name='api_cambiar_agente'),

    path('api/conversaciones/', views.api_listar_conversaciones, name='api_listar_conversaciones'),
    path('api/conversaciones/nueva/', views.api_crear_conversacion, name='api_crear_conversacion'),
    path('api/conversaciones/<int:conv_id>/mensajes/', views.api_mensajes, name='api_mensajes'),
    path('api/acciones/<str:accion_id>/confirmar/', views.api_confirmar_accion, name='api_confirmar_accion'),
    path('api/acciones/<str:accion_id>/rechazar/', views.api_rechazar_accion, name='api_rechazar_accion'),
]
