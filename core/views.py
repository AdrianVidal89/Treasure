import os
import subprocess
import tempfile

from django.db import connections
from django.http import HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.utils import timezone
from .forms import UserRegisterForm, UserProfileForm
from .models import UserProfile, Hogar
from .mixins import hogar_required

# Palabra exacta que el admin debe escribir para confirmar una restauración.
CONFIRMACION_RESTAURAR = 'RESTAURAR'


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

from .forms import CrearUsuarioDesdeAdminForm

@login_required
def crear_usuario(request):
    profile = getattr(request.user, 'userprofile', None)
    if not request.user.is_superuser and (not profile or not profile.es_admin):
        messages.error(request, "No tienes permisos para crear usuarios.")
        return redirect('dashboard')

    if request.method == 'POST':
        form = CrearUsuarioDesdeAdminForm(request.POST)
        if form.is_valid():
            user = User.objects.create_user(
                username=form.cleaned_data['username'],
                first_name=form.cleaned_data['first_name'],
                email=form.cleaned_data['email'],
                password=form.cleaned_data['password'],
            )
            # El signal ya crea el UserProfile, lo actualizamos
            profile = user.userprofile
            profile.hogar = form.cleaned_data['hogar']
            profile.rol = form.cleaned_data['rol']
            profile.save()

            messages.success(request, f"Usuario '{user.username}' creado correctamente.")
            return redirect('panel_admin')
    else:
        form = CrearUsuarioDesdeAdminForm()

    return render(request, 'core/crear_usuario.html', {'form': form})

@login_required
def eliminar_hogar(request, hogar_id):
    profile = getattr(request.user, 'userprofile', None)
    if not request.user.is_superuser and (not profile or not profile.es_admin):
        messages.error(request, "No tienes permisos para eliminar hogares.")
        return redirect('panel_admin')

    hogar = get_object_or_404(Hogar, id=hogar_id)

    if request.method == 'POST':
        nombre = hogar.nombre
        # Desasignar miembros antes de borrar
        hogar.miembros.update(hogar=None, rol='miembro')
        hogar.delete()
        messages.success(request, f"Hogar '{nombre}' eliminado correctamente.")
        return redirect('panel_admin')

    return render(request, 'core/confirmar_eliminar_hogar.html', {'hogar': hogar})


@login_required
def backup_bd(request):
    """Descarga un volcado (.dump, formato custom de pg_dump) de la base de datos."""
    profile = getattr(request.user, 'userprofile', None)
    if not request.user.is_superuser and (not profile or not profile.es_admin):
        messages.error(request, "No tienes permisos para descargar backups de la base de datos.")
        return redirect('dashboard')

    database_url = os.environ.get('DATABASE_URL', '')

    try:
        resultado = subprocess.run(
            ['pg_dump', '-Fc', database_url],
            capture_output=True, check=True, timeout=120,
        )
    except FileNotFoundError:
        messages.error(request, "pg_dump no está instalado en este servidor.")
        return redirect('panel_admin')
    except subprocess.CalledProcessError as e:
        detalle = e.stderr.decode('utf-8', 'ignore')
        messages.error(request, f"Error al generar el backup: {detalle}")
        return redirect('panel_admin')
    except subprocess.TimeoutExpired:
        messages.error(request, "El backup ha tardado demasiado y se ha cancelado.")
        return redirect('panel_admin')

    nombre_archivo = f"treasure_backup_{timezone.now().strftime('%Y%m%d_%H%M%S')}.dump"
    response = HttpResponse(resultado.stdout, content_type='application/octet-stream')
    response['Content-Disposition'] = f'attachment; filename="{nombre_archivo}"'
    return response


@login_required
def restaurar_bd(request):
    """Restaura la base de datos completa desde un .dump subido por el admin.

    Acción destructiva e irreversible: sobreescribe TODA la base de datos,
    incluida la tabla de sesiones (el admin quedará desconectado). Por eso
    exige escribir una palabra de confirmación exacta antes de ejecutar.
    """
    profile = getattr(request.user, 'userprofile', None)
    if not request.user.is_superuser and (not profile or not profile.es_admin):
        messages.error(request, "No tienes permisos para restaurar la base de datos.")
        return redirect('dashboard')

    if request.method != 'POST':
        return render(request, 'core/restaurar_bd.html', {'palabra_confirmacion': CONFIRMACION_RESTAURAR})

    confirmacion = request.POST.get('confirmacion', '').strip()
    archivo = request.FILES.get('archivo')

    if confirmacion != CONFIRMACION_RESTAURAR:
        messages.error(request, f'Debes escribir exactamente "{CONFIRMACION_RESTAURAR}" para confirmar.')
        return redirect('restaurar_bd')

    if not archivo:
        messages.error(request, "No se subió ningún archivo.")
        return redirect('restaurar_bd')

    cabecera = archivo.read(5)
    archivo.seek(0)
    if cabecera != b'PGDMP':
        messages.error(request, "El archivo no parece un backup válido (debe ser un .dump generado por esta misma función).")
        return redirect('restaurar_bd')

    tmp = tempfile.NamedTemporaryFile(suffix='.dump', delete=False)
    try:
        for chunk in archivo.chunks():
            tmp.write(chunk)
        tmp.close()

        database_url = os.environ.get('DATABASE_URL', '')
        # Cerramos las conexiones de Django antes de que pg_restore recree las tablas.
        connections.close_all()

        try:
            resultado = subprocess.run(
                ['pg_restore', '--clean', '--if-exists', '--no-owner', '--no-privileges',
                 '--single-transaction', '-d', database_url, tmp.name],
                capture_output=True, timeout=300,
            )
            exito = resultado.returncode == 0
            detalle = '' if exito else resultado.stderr.decode('utf-8', 'ignore')
        except FileNotFoundError:
            exito = False
            detalle = "pg_restore no está instalado en este servidor."
        except subprocess.TimeoutExpired:
            exito = False
            detalle = "La restauración ha tardado demasiado y se ha cancelado (no se aplicó ningún cambio)."
    finally:
        os.unlink(tmp.name)

    # Página mínima, sin base.html: tras un restore la sesión/BD pueden estar
    # en un estado distinto al que había al iniciar la petición.
    return render(request, 'core/restaurar_bd_resultado.html', {'exito': exito, 'detalle': detalle})