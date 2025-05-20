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
    categorias = request.GET.getlist('categorias[]')  # total, liquido, inversiones
    periodo = request.GET.get('periodo', '12')  # por defecto 12 meses

    registros = RegistroMensual.objects.filter(usuario=request.user).order_by('anio', 'mes')

    if periodo != 'todos':
        try:
            periodo_int = int(periodo)
            registros = registros[::-1][:periodo_int][::-1]  # últimos N registros
        except ValueError:
            pass  # por si mandan algo inválido

    labels = [f"{r.mes:02d}/{r.anio}" for r in registros]
    data = {}

    if 'total' in categorias:
        data['total'] = [float(r.patrimonio_total) for r in registros]
    if 'liquido' in categorias:
        data['liquido'] = [float(r.total_liquido) for r in registros]
    if 'inversiones' in categorias:
        data['inversiones'] = [float(r.total_inversiones) for r in registros]

    return JsonResponse({'labels': labels, 'series': data})
