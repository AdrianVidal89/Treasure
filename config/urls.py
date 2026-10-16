from django.contrib import admin
from django.urls import path, include
from django.shortcuts import redirect
from django.templatetags.static import static

urlpatterns = [
    path('admin/', admin.site.urls),
    # Navegadores y marcadores piden /favicon.ico en la raíz aunque la página
    # declare otro.
    path('favicon.ico', lambda request: redirect(static('img/icons/favicon.ico'), permanent=True)),
    path('', lambda request: redirect('login', permanent=False)),
    path('core/', include('core.urls')),
    path('finanzas/', include('finanzas.urls')),
    path('ui/', include('ui.urls')),
    path('asistente-ia/', include('asistente_ia.urls')),
    path('exportador/', include('exportador.urls')),
    path('integraciones/', include('integraciones.urls')),
    path('extractos/', include('extractos.urls')),
]