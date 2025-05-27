from django.urls import path
from . import views
from .views import nueva_cuenta_bancaria, patrimonio_total_actual
from django.urls import path
from . import views

app_name = 'finanzas'

urlpatterns = [
    path('resumen/<int:anio>/<int:mes>/', views.resumen_mensual, name='resumen_mensual'),
    path('gestionar/', views.gestionar_cuentas, name='gestionar_cuentas'),
    path('cuenta/nueva/', nueva_cuenta_bancaria, name='nueva_cuenta'),
    path('cuenta/saldo/', views.nuevo_saldo, name='nuevo_saldo'),
    path('cuenta/<int:cuenta_id>/', views.detalle_cuenta, name='detalle_cuenta'),
    path('cuentas/<int:cuenta_id>/eliminar/', views.eliminar_cuenta, name='eliminar_cuenta'),
    path('ajax/obtener-saldo/', views.obtener_saldo_ajax, name='obtener_saldo_ajax'),
    path('tarjetas/', views.gestionar_tarjetas, name='gestionar_tarjetas'),
    path('tarjeta/nueva/', views.nueva_tarjeta, name='nueva_tarjeta'),
    path('tarjeta/<int:tarjeta_id>/', views.detalle_tarjeta, name='detalle_tarjeta'),
    path('tarjeta/<int:tarjeta_id>/eliminar/', views.eliminar_tarjeta, name='eliminar_tarjeta'),
    path('ajax/obtener-saldo-tarjeta/', views.obtener_saldo_tarjeta_ajax, name='obtener_saldo_tarjeta_ajax'),
    path("api/patrimonio-total/", patrimonio_total_actual, name="patrimonio_total_actual"),
    path('', views.InversionListView.as_view(), name='listar'),
    path('nueva/', views.InversionCreateView.as_view(), name='crear'),
    path('<int:pk>/', views.InversionDetailView.as_view(), name='detalle'),
    path('<int:pk>/editar/', views.InversionUpdateView.as_view(), name='editar'),
    path('<int:pk>/movimiento/', views.MovimientoCreateView.as_view(), name='nuevo_movimiento'),
    path('resumen/<int:pk>/', views.ResumenInversionesMensualView.as_view(), name='resumen'),
]