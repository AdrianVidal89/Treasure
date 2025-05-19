from django.urls import path
from . import views
from .views import nueva_cuenta_bancaria

urlpatterns = [
    path('', views.index, name='finanzas-home'),
    path('resumen/<int:anio>/<int:mes>/', views.resumen_mensual, name='resumen_mensual'),
    path('gestionar/', views.gestionar_cuentas, name='gestionar_cuentas'),
    path('cuenta/nueva/', nueva_cuenta_bancaria, name='nueva_cuenta'),
    path('cuenta/saldo/', views.nuevo_saldo, name='nuevo_saldo'),
    path('cuenta/<int:cuenta_id>/', views.detalle_cuenta, name='detalle_cuenta'),

]
