from django.urls import path

from . import views

app_name = 'extractos'

urlpatterns = [
    path('', views.listar, name='listar'),
    path('subir/', views.subir, name='subir'),
    path('subir/revisar/', views.revisar, name='revisar'),
    # Conciliación y Análisis se fundieron con Movimientos: la comparación con
    # el presupuesto, la media de los meses anteriores, el desglose por
    # comercio y lo que se sale del límite viven ahora en la vista principal,
    # al lado de los apuntes que lo explican. Las rutas se mantienen
    # redirigiendo, para que los enlaces guardados sigan llevando a algún sitio.
    path('conciliacion/', views.conciliacion, name='conciliacion'),
    path('analisis/', views.analisis, name='analisis'),
    path('categoria/desglose/', views.movimientos_de_categoria, name='desglose_categoria'),
    path('mes/filas/', views.filas_del_mes, name='filas_mes'),
    path('movimiento/<int:pk>/cubrir/', views.cubrir_con_reserva, name='cubrir_con_reserva'),
    path('movimiento/<int:pk>/sin-reserva/', views.marcar_sin_reserva, name='marcar_sin_reserva'),
    path('movimiento/reserva/candidatos/', views.candidatos_reserva, name='candidatos_reserva'),
    path('movimiento/<int:pk>/dividir/', views.dividir_movimiento, name='dividir_movimiento'),
    path('movimiento/<int:pk>/dividir/deshacer/', views.deshacer_division, name='deshacer_division'),
    path('movimiento/dividir/aprender/', views.aprender_division, name='aprender_division'),
    path('movimientos/lote/', views.accion_lote, name='accion_lote'),
    path('etiquetas/', views.etiquetas, name='etiquetas'),
    path('etiquetas/comercio/', views.etiquetar_comercio, name='etiquetar_comercio'),
    path('sin-categorizar/', views.sin_categorizar, name='sin_categorizar'),
    path('reglas/', views.reglas, name='reglas'),
    path('reglas/aprender/', views.aprender_regla, name='aprender_regla'),
    path('<int:pk>/', views.detalle, name='detalle'),
    path('<int:pk>/eliminar/', views.eliminar, name='eliminar'),
    path('movimiento/<int:pk>/actualizar/', views.actualizar_movimiento, name='actualizar_movimiento'),
    path('movimiento/nuevo/', views.crear_movimiento, name='crear_movimiento'),
    path('movimiento/<int:pk>/eliminar/', views.eliminar_movimiento, name='eliminar_movimiento'),
    path('movimiento/<int:pk>/etiquetar/', views.etiquetar_movimiento, name='etiquetar_movimiento'),
    path('movimiento/<int:pk>/imputar/', views.imputar_movimiento, name='imputar_movimiento'),
    path('imputar/comercio/', views.imputar_comercio, name='imputar_comercio'),
    path('movimiento/<int:pk>/provision/', views.marcar_provision, name='marcar_provision'),
    path('movimiento/<int:pk>/traspaso/', views.marcar_traspaso, name='marcar_traspaso'),
]
