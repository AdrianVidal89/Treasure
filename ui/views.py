from django.http import HttpResponse
from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.decorators import login_required
from django.contrib.auth import logout

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
    return render(request, 'ui/dashboard.html', {
        'user': request.user
    })