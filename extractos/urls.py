from django.urls import path

from . import views

app_name = 'extractos'

urlpatterns = [
    path('', views.listar, name='listar'),
    path('subir/', views.subir, name='subir'),
    path('subir/revisar/', views.revisar, name='revisar'),
    path('conciliacion/', views.conciliacion, name='conciliacion'),
    # La pantalla de Análisis se fundió con Movimientos: su contenido —la
    # comparación con la media, el desglose por comercio y lo que se sale del
    # presupuesto— vive ahora en la vista principal. La ruta se mantiene
    # redirigiendo, para que los enlaces guardados y los de la conciliación
    # sigan llevando a algún sitio.
    path('analisis/', views.analisis, name='analisis'),
    path('categoria/desglose/', views.movimientos_de_categoria, name='desglose_categoria'),
    path('mes/filas/', views.filas_del_mes, name='filas_mes'),
    path('movimiento/<int:pk>/cubrir/', views.cubrir_con_reserva, name='cubrir_con_reserva'),
    path('movimiento/pagos-cubribles/', views.pagos_cubribles, name='pagos_cubribles'),
    path('movimiento/<int:pk>/dividir/', views.dividir_movimiento, name='dividir_movimiento'),
    path('movimiento/<int:pk>/dividir/deshacer/', views.deshacer_division, name='deshacer_division'),
    path('movimientos/lote/', views.accion_lote, name='accion_lote'),
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
