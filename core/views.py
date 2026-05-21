from django.http import HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from .forms import UserRegisterForm, UserProfileForm
from .models import UserProfile, Hogar
from .mixins import hogar_required


def register_view(request):
    if request.method == 'POST':
        form = UserRegisterForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            return redirect('dashboard')
    else:
        form = UserRegisterForm()
    return render(request, 'core/register.html', {'form': form, 'hide_navbar': True})


@login_required
def editar_perfil(request):
    profile, created = UserProfile.objects.get_or_create(user=request.user)
    if request.method == 'POST':
        form = UserProfileForm(request.POST, request.FILES, instance=profile)
        if form.is_valid():
            form.save()
            return redirect('dashboard')
    else:
        form = UserProfileForm(instance=profile)
    return render(request, 'core/editar_perfil.html', {'form': form})


@login_required
def panel_admin(request):
    """Landing page de configuración para administradores."""
    profile = getattr(request.user, 'userprofile', None)

    # Superusuario de Django puede acceder siempre
    # Admin de hogar puede gestionar su hogar
    if not request.user.is_superuser and (not profile or not profile.es_admin):
        messages.error(request, "No tienes permisos para acceder al panel de administración.")
        return redirect('dashboard')

    hogares = Hogar.objects.all().prefetch_related('miembros__user')
    usuarios_sin_hogar = UserProfile.objects.filter(hogar__isnull=True).select_related('user')

    return render(request, 'core/panel_admin.html', {
        'hogares': hogares,
        'usuarios_sin_hogar': usuarios_sin_hogar,
    })


@login_required
def crear_hogar(request):
    profile = getattr(request.user, 'userprofile', None)
    if not request.user.is_superuser and (not profile or not profile.es_admin):
        messages.error(request, "No tienes permisos para crear hogares.")
        return redirect('dashboard')

    if request.method == 'POST':
        nombre = request.POST.get('nombre', '').strip()
        moneda = request.POST.get('moneda', 'EUR')
        if nombre:
            hogar = Hogar.objects.create(
                nombre=nombre,
                moneda_principal=moneda,
                creado_por=request.user
            )
            messages.success(request, f"Hogar '{hogar.nombre}' creado correctamente.")
            return redirect('panel_admin')
        else:
            messages.error(request, "El nombre del hogar no puede estar vacío.")

    return render(request, 'core/crear_hogar.html')


@login_required
def asignar_usuario(request):
    """Asigna un usuario a un hogar con un rol."""
    profile = getattr(request.user, 'userprofile', None)
    if not request.user.is_superuser and (not profile or not profile.es_admin):
        messages.error(request, "No tienes permisos para asignar usuarios.")
        return redirect('dashboard')

    if request.method == 'POST':
        usuario_id = request.POST.get('usuario_id')
        hogar_id = request.POST.get('hogar_id')
        rol = request.POST.get('rol', 'miembro')

        usuario_profile = get_object_or_404(UserProfile, user__id=usuario_id)
        hogar = get_object_or_404(Hogar, id=hogar_id)

        usuario_profile.hogar = hogar
        usuario_profile.rol = rol
        usuario_profile.save()

        messages.success(request, f"{usuario_profile.user.username} asignado a {hogar.nombre} como {rol}.")
        return redirect('panel_admin')

    hogares = Hogar.objects.all()
    usuarios = UserProfile.objects.select_related('user', 'hogar').all()
    return render(request, 'core/asignar_usuario.html', {
        'hogares': hogares,
        'usuarios': usuarios,
    })


@login_required
def mi_hogar(request):
    """Vista del hogar para usuarios normales."""
    profile = getattr(request.user, 'userprofile', None)

    if not profile or not profile.hogar:
        return render(request, 'core/sin_hogar.html')

    miembros = UserProfile.objects.filter(
        hogar=profile.hogar
    ).select_related('user')

    return render(request, 'core/mi_hogar.html', {
        'hogar': profile.hogar,
        'miembros': miembros,
        'profile': profile,
    })

@login_required
def editar_hogar(request, hogar_id):
    profile = getattr(request.user, 'userprofile', None)
    if not request.user.is_superuser and (not profile or not profile.es_admin):
        messages.error(request, "No tienes permisos.")
        return redirect('dashboard')

    hogar = get_object_or_404(Hogar, id=hogar_id)

    if request.method == 'POST':
        nombre = request.POST.get('nombre', '').strip()
        moneda = request.POST.get('moneda', 'EUR')
        if nombre:
            hogar.nombre = nombre
            hogar.moneda_principal = moneda
            hogar.save()
            messages.success(request, f"Hogar '{hogar.nombre}' actualizado.")
            return redirect('panel_admin')
        else:
            messages.error(request, "El nombre no puede estar vacío.")

    return render(request, 'core/editar_hogar.html', {'hogar': hogar})


@login_required
def eliminar_miembro(request, profile_id):
    profile = getattr(request.user, 'userprofile', None)
    if not request.user.is_superuser and (not profile or not profile.es_admin):
        messages.error(request, "No tienes permisos.")
        return redirect('dashboard')

    miembro_profile = get_object_or_404(UserProfile, id=profile_id)
    miembro_profile.hogar = None
    miembro_profile.rol = 'miembro'
    miembro_profile.save()
    messages.success(request, f"{miembro_profile.user.username} eliminado del hogar.")
    return redirect('panel_admin')