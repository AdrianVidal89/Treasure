from django.contrib import admin
from django.urls import path, include
from django.shortcuts import redirect

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', lambda request: redirect('login', permanent=False)),
    path('core/', include('core.urls')),
    path('finanzas/', include('finanzas.urls')),
    path('ui/', include('ui.urls')),
    path('exportador/', include('exportador.urls')),
    path('integraciones/', include('integraciones.urls')),
]