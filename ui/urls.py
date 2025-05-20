from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='ui-home'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('dashboard/', views.dashboard_view, name='dashboard'),
    path('dashboard/datos-evolucion/', views.datos_evolucion_financiera, name='datos_evolucion'),
]
