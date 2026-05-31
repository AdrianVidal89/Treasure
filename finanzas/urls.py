from django.urls import path
from . import views
from . import views_ingresos
from . import views_gastos
from . import views_distribucion
from .views import nueva_cuenta_bancaria, patrimonio_total_actual
from . import views_evolucion

app_name = 'finanzas'

urlpatterns = [
    # ── Resumen y cuentas ──────────────────────────────────────────────────
    path('resumen/<int:anio>/<int:mes>/', views.resumen_mensual, name='resumen_mensual'),
    path('gestionar/', views.gestionar_cuentas, name='gestionar_cuentas'),
    path('cuenta/nueva/', nueva_cuenta_bancaria, name='nueva_cuenta'),
    path('cuenta/saldo/', views.nuevo_saldo, name='nuevo_saldo'),
    path('cuenta/<int:cuenta_id>/', views.detalle_cuenta, name='detalle_cuenta'),
    path('cuentas/<int:cuenta_id>/eliminar/', views.eliminar_cuenta, name='eliminar_cuenta'),
    path('ajax/obtener-saldo/', views.obtener_saldo_ajax, name='obtener_saldo_ajax'),

    # ── Tarjetas ───────────────────────────────────────────────────────────
    path('tarjetas/', views.gestionar_tarjetas, name='gestionar_tarjetas'),
    path('tarjeta/nueva/', views.nueva_tarjeta, name='nueva_tarjeta'),
    path('tarjeta/<int:tarjeta_id>/', views.detalle_tarjeta, name='detalle_tarjeta'),
    path('tarjeta/<int:tarjeta_id>/eliminar/', views.eliminar_tarjeta, name='eliminar_tarjeta'),
    path('ajax/obtener-saldo-tarjeta/', views.obtener_saldo_tarjeta_ajax, name='obtener_saldo_tarjeta_ajax'),

    # ── API patrimonio ─────────────────────────────────────────────────────
    path('api/patrimonio-total/', patrimonio_total_actual, name='patrimonio_total_actual'),

    # ── Inversiones ────────────────────────────────────────────────────────
    path('inversiones/', views.InversionListView.as_view(), name='listar'),
    path('inversiones/nueva/', views.InversionCreateView.as_view(), name='crear'),
    path('inversiones/<int:pk>/', views.InversionDetailView.as_view(), name='detalle'),
    path('inversiones/<int:pk>/editar/', views.InversionUpdateView.as_view(), name='editar'),
    path('inversiones/<int:pk>/movimiento/', views.MovimientoCreateView.as_view(), name='nuevo_movimiento'),
    path('inversiones/resumen/<int:pk>/', views.ResumenInversionesMensualView.as_view(), name='resumen'),

    # ── Ingresos ───────────────────────────────────────────────────────────
    path('ingresos/', views_ingresos.listar_ingresos, name='listar_ingresos'),
    path('ingresos/crear/', views_ingresos.crear_ingreso, name='crear_ingreso'),
    path('ingresos/<int:ingreso_id>/editar/', views_ingresos.editar_ingreso, name='editar_ingreso'),
    path('ingresos/<int:ingreso_id>/eliminar/', views_ingresos.eliminar_ingreso, name='eliminar_ingreso'),
    path('ingresos/destino/crear/', views_ingresos.crear_destino, name='crear_destino'),
    path('ingresos/ajax/simular-neto/', views_ingresos.simular_neto, name='simular_neto'),

    # ── Gastos ─────────────────────────────────────────────────────────────
    path('gastos/', views_gastos.listar_gastos, name='listar_gastos'),
    path('gastos/crear/', views_gastos.crear_partida, name='crear_partida'),
    path('gastos/<int:partida_id>/editar/', views_gastos.editar_partida, name='editar_partida'),
    path('gastos/<int:partida_id>/eliminar/', views_gastos.eliminar_partida, name='eliminar_partida'),
    path('gastos/categoria/crear/', views_gastos.crear_categoria, name='crear_categoria'),

    # ── Distribución ──────────────────────────────────────────────────────
    path('distribucion/', views_distribucion.vista_distribucion, name='vista_distribucion'),
    path('distribucion/resumen-anual/', views_distribucion.vista_resumen_anual, name='resumen_anual'),
    path('distribucion/ajustar-ingreso/', views_distribucion.ajustar_ingreso_mes, name='ajustar_ingreso_mes'),

    # Fondos
    path('distribucion/fondo/crear/', views_distribucion.crear_fondo, name='crear_fondo'),
    path('distribucion/fondo/<int:fondo_id>/editar/', views_distribucion.editar_fondo, name='editar_fondo'),
    path('distribucion/fondo/<int:fondo_id>/eliminar/', views_distribucion.eliminar_fondo, name='eliminar_fondo'),
    path('distribucion/fondo/<int:fondo_id>/gastos/', views_distribucion.asignar_gastos_fondo, name='asignar_gastos_fondo'),
    path('distribucion/gasto/<int:partida_id>/desasignar/', views_distribucion.desasignar_gasto_fondo, name='desasignar_gasto_fondo'),

    # Subsobres
    path('distribucion/fondo/<int:fondo_id>/subsobre/crear/', views_distribucion.crear_subsobres, name='crear_subsobres'),
    path('distribucion/subsobre/<int:subsobres_id>/eliminar/', views_distribucion.eliminar_subsobres, name='eliminar_subsobres'),

    # Reglas
    path('distribucion/regla/crear/', views_distribucion.crear_regla, name='crear_regla'),
    path('distribucion/regla/<int:regla_id>/editar/', views_distribucion.editar_regla, name='editar_regla'),
    path('distribucion/regla/<int:regla_id>/eliminar/', views_distribucion.eliminar_regla, name='eliminar_regla'),

    # Añadir en urlpatterns:
    path('evolucion/', views_evolucion.vista_evolucion, name='vista_evolucion'),
    path('evolucion/saldo/', views_evolucion.registrar_saldo_fondo, name='registrar_saldo_fondo'),
    path('evolucion/ingreso/', views_evolucion.registrar_ingreso_mes, name='registrar_ingreso_mes'),
    path('evolucion/fondo/crear/', views_evolucion.crear_fondo_evolucion, name='crear_fondo_evolucion'),
]


