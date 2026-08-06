from django.urls import path

from . import views

app_name = 'extractos'

urlpatterns = [
    path('', views.listar, name='listar'),
    path('subir/', views.subir, name='subir'),
    path('subir/revisar/', views.revisar, name='revisar'),
    path('conciliacion/', views.conciliacion, name='conciliacion'),
    path('sin-categorizar/', views.sin_categorizar, name='sin_categorizar'),
    path('categorias/', views.categorias, name='categorias'),
    path('reglas/', views.reglas, name='reglas'),
    path('reglas/aprender/', views.aprender_regla, name='aprender_regla'),
    path('<int:pk>/', views.detalle, name='detalle'),
    path('<int:pk>/eliminar/', views.eliminar, name='eliminar'),
    path('movimiento/<int:pk>/actualizar/', views.actualizar_movimiento, name='actualizar_movimiento'),
    path('movimiento/<int:pk>/eliminar/', views.eliminar_movimiento, name='eliminar_movimiento'),
]
