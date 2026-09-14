from django.urls import path

from . import views

app_name = 'extractos'

urlpatterns = [
    path('', views.listar, name='listar'),
    path('subir/', views.subir, name='subir'),
    path('subir/revisar/', views.revisar, name='revisar'),
    path('conciliacion/', views.conciliacion, name='conciliacion'),
    path('analisis/', views.analisis, name='analisis'),
    path('etiquetas/', views.etiquetas, name='etiquetas'),
    path('etiquetas/comercio/', views.etiquetar_comercio, name='etiquetar_comercio'),
    path('sin-categorizar/', views.sin_categorizar, name='sin_categorizar'),
    path('reglas/', views.reglas, name='reglas'),
    path('reglas/aprender/', views.aprender_regla, name='aprender_regla'),
    path('<int:pk>/', views.detalle, name='detalle'),
    path('<int:pk>/eliminar/', views.eliminar, name='eliminar'),
    path('movimiento/<int:pk>/actualizar/', views.actualizar_movimiento, name='actualizar_movimiento'),
    path('movimiento/<int:pk>/eliminar/', views.eliminar_movimiento, name='eliminar_movimiento'),
    path('movimiento/<int:pk>/etiquetar/', views.etiquetar_movimiento, name='etiquetar_movimiento'),
    path('movimiento/<int:pk>/imputar/', views.imputar_movimiento, name='imputar_movimiento'),
    path('imputar/comercio/', views.imputar_comercio, name='imputar_comercio'),
    path('movimiento/<int:pk>/provision/', views.marcar_provision, name='marcar_provision'),
]
