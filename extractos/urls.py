from django.urls import path

from . import views

app_name = 'extractos'

urlpatterns = [
    path('', views.listar, name='listar'),
    path('subir/', views.subir, name='subir'),
    path('subir/revisar/', views.revisar, name='revisar'),
    path('conciliacion/', views.conciliacion, name='conciliacion'),
    path('<int:pk>/', views.detalle, name='detalle'),
    path('<int:pk>/eliminar/', views.eliminar, name='eliminar'),
]
