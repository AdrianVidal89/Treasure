from django.http import HttpResponse
from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.decorators import login_required
from django.contrib.auth import logout
from datetime import datetime
from finanzas.models import RegistroMensual
from django.http import JsonResponse



def index(request):
    return HttpResponse("<h1>Bienvenido a la interfaz UI</h1>")

def login_view(request):
    if request.method == 'POST':
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            login(request, form.get_user())
            return redirect('dashboard')  # 👈 Redirige aquí
    else:
        form = AuthenticationForm()
    return render(request, 'ui/login.html', {'form': form, 'hide_navbar': True})

def logout_view(request):
    logout(request)
    return redirect('login')

@login_required
def dashboard_view(request):
    registros = RegistroMensual.objects.filter(usuario=request.user).order_by('anio', 'mes')

    labels = []
    patrimonio = []
    liquido = []
    inversiones = []

    for r in registros:
        fecha = datetime(r.anio, r.mes, 1).strftime('%b %Y')  # Ej. "Ene 2024"
        labels.append(fecha)
        patrimonio.append(float(r.patrimonio_total))
        liquido.append(float(r.total_liquido))
        inversiones.append(float(r.total_inversiones))
    
    print("LABELS:", labels)
    print("PATRIMONIO:", patrimonio)
    print("LIQUIDO:", liquido)
    print("INVERSIONES:", inversiones)


    return render(request, 'ui/dashboard.html', {
        'labels': labels,
        'patrimonio': patrimonio,
        'liquido': liquido,
        'inversiones': inversiones
    })

@login_required
def datos_evolucion_financiera(request):
    categorias = request.GET.getlist('categorias[]')
    periodo = request.GET.get('periodo', '12')

    registros = RegistroMensual.objects.filter(usuario=request.user).order_by('anio', 'mes')

    if periodo != 'todos':
        try:
            periodo_int = int(periodo)
            registros = registros[::-1][:periodo_int][::-1]
        except ValueError:
            pass

    labels = [f"{r.mes:02d}/{r.anio}" for r in registros]
    data = {}

    if 'total' in categorias:
        data['total'] = [float(r.patrimonio_total) for r in registros]
    if 'liquido' in categorias:
        data['liquido'] = [float(r.total_liquido) for r in registros]
    if 'inversiones' in categorias:
        data['inversiones'] = [float(r.total_inversiones) for r in registros]

    # ⬇️ Datos para widgets mini-dashboard
    actual = registros[-1] if registros else None
    anterior = registros[-2] if len(registros) >= 2 else None

    def valor(obj, attr):
        return float(getattr(obj, attr, 0)) if obj else 0

    resumen_actual = {
        'liquido': {
            'actual': float(actual.total_liquido) if actual else 0,
            'anterior': float(anterior.total_liquido) if anterior else 0,
        },
        'creditos': {
            'actual': valor(actual, 'total_creditos'),
            'anterior': valor(anterior, 'total_creditos'),
        },
        'activos': {
            'actual': valor(actual, 'total_vehiculos'),
            'anterior': valor(anterior, 'total_vehiculos'),
        },
        'inversiones': {
            'actual': valor(actual, 'total_inversiones'),
            'anterior': valor(anterior, 'total_inversiones'),
        }
    }

    return JsonResponse({
        'labels': labels,
        'series': data,
        'resumen_actual': resumen_actual
    })
