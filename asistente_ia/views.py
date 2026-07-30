import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from core.mixins import hogar_required

from . import acciones
from .contexto import construir_contexto_financiero
from .forms import AgenteIAForm
from .loop import procesar_mensaje
from .models import AgenteIA, ConfiguracionIA, ConversacionIA, MensajeIA
from .proveedores import ErrorProveedorIA, listar_modelos

PROVEEDORES_VALIDOS = {p for p, _ in ConfiguracionIA.PROVEEDOR_CHOICES}


def _es_admin(request):
    profile = getattr(request.user, 'userprofile', None)
    return request.user.is_superuser or (profile and profile.es_admin)


def _config_admin(request, proveedor=None):
    """Valida acceso de admin y proveedor para los endpoints de configuración.

    Devuelve (config, None) si todo es correcto, o (None, JsonResponse) con el
    error a devolver.
    """
    if not _es_admin(request):
        return None, JsonResponse(
            {'error': 'Solo el administrador del hogar puede configurar la IA.'}, status=403
        )
    if proveedor is not None and proveedor not in PROVEEDORES_VALIDOS:
        return None, JsonResponse({'error': 'Proveedor desconocido.'}, status=404)
    config, _ = ConfiguracionIA.objects.get_or_create(hogar=request.user.userprofile.hogar)
    return config, None


def _estado_proveedor(config, proveedor):
    return {
        'proveedor': proveedor,
        'conectado': config.tiene_proveedor_configurado(proveedor),
        'clave_enmascarada': config.get_api_key_enmascarada(proveedor),
        'modelo': config.get_modelo(proveedor),
        'activo': config.proveedor_activo == proveedor,
    }


# ─── Páginas ──────────────────────────────────────────────────────────────

@login_required
@hogar_required
def landing(request):
    profile = request.user.userprofile
    config, _ = ConfiguracionIA.objects.get_or_create(hogar=profile.hogar)
    agentes = AgenteIA.objects.filter(hogar=profile.hogar, activo=True)
    return render(request, 'asistente_ia/landing.html', {
        'config': config,
        'agentes': agentes,
        'es_admin': _es_admin(request),
    })


@login_required
@hogar_required
def configuracion(request):
    profile = request.user.userprofile
    if not _es_admin(request):
        messages.error(request, "Solo el administrador del hogar puede configurar la IA.")
        return redirect('asistente_ia:landing')

    config, _ = ConfiguracionIA.objects.get_or_create(hogar=profile.hogar)

    proveedores = [
        {
            'id': identificador,
            'etiqueta': etiqueta,
            'ayuda': AYUDA_PROVEEDOR[identificador],
            **_estado_proveedor(config, identificador),
        }
        for identificador, etiqueta in ConfiguracionIA.PROVEEDOR_CHOICES
    ]
    return render(request, 'asistente_ia/configuracion.html', {
        'config': config,
        'proveedores': proveedores,
    })


AYUDA_PROVEEDOR = {
    'anthropic': 'Consigue una clave en console.anthropic.com',
    'openai': 'Consigue una clave en platform.openai.com/api-keys',
    'gemini': 'Consigue una clave en aistudio.google.com/apikey',
}


# ─── API de configuración de proveedores ─────────────────────────────────

@login_required
@hogar_required
def api_guardar_clave(request, proveedor):
    """Verifica la clave contra el proveedor y, si es válida, la guarda cifrada."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Método no permitido.'}, status=405)
    config, error = _config_admin(request, proveedor)
    if error:
        return error

    try:
        data = json.loads(request.body or b'{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Solicitud inválida.'}, status=400)

    api_key = (data.get('api_key') or '').strip()
    if not api_key:
        return JsonResponse({'error': 'Introduce una clave API.'}, status=400)

    # Listar modelos hace de verificación: si la clave no vale, falla aquí y no
    # llegamos a guardarla.
    try:
        modelos = listar_modelos(proveedor, api_key)
    except ErrorProveedorIA as e:
        return JsonResponse({'error': str(e)}, status=400)

    config.set_api_key(proveedor, api_key)
    # Si el modelo guardado ya no existe para esta clave, escogemos el primero
    # disponible para evitar errores 404 al chatear.
    ids_disponibles = [m['id'] for m in modelos]
    if modelos and config.get_modelo(proveedor) not in ids_disponibles:
        setattr(config, f'{proveedor}_modelo', ids_disponibles[0])
    if not config.proveedor_activo:
        config.proveedor_activo = proveedor
    config.actualizado_por = request.user
    config.save()

    return JsonResponse({
        'ok': True,
        'modelos': modelos,
        **_estado_proveedor(config, proveedor),
    })


@login_required
@hogar_required
def api_listar_modelos(request, proveedor):
    """Devuelve los modelos disponibles usando la clave ya guardada."""
    config, error = _config_admin(request, proveedor)
    if error:
        return error
    if not config.tiene_proveedor_configurado(proveedor):
        return JsonResponse({'error': 'Este proveedor no tiene ninguna clave guardada.'}, status=400)
    try:
        modelos = listar_modelos(proveedor, config.get_api_key(proveedor))
    except ErrorProveedorIA as e:
        return JsonResponse({'error': str(e)}, status=400)
    return JsonResponse({'modelos': modelos, 'modelo_actual': config.get_modelo(proveedor)})


@login_required
@hogar_required
def api_guardar_modelo(request, proveedor):
    if request.method != 'POST':
        return JsonResponse({'error': 'Método no permitido.'}, status=405)
    config, error = _config_admin(request, proveedor)
    if error:
        return error
    try:
        data = json.loads(request.body or b'{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Solicitud inválida.'}, status=400)

    modelo = (data.get('modelo') or '').strip()
    if not modelo:
        return JsonResponse({'error': 'Selecciona un modelo.'}, status=400)

    setattr(config, f'{proveedor}_modelo', modelo)
    config.actualizado_por = request.user
    config.save()
    return JsonResponse({'ok': True, **_estado_proveedor(config, proveedor)})


@login_required
@hogar_required
def api_activar_proveedor(request, proveedor):
    if request.method != 'POST':
        return JsonResponse({'error': 'Método no permitido.'}, status=405)
    config, error = _config_admin(request, proveedor)
    if error:
        return error
    if not config.tiene_proveedor_configurado(proveedor):
        return JsonResponse({'error': 'Guarda primero una clave para este proveedor.'}, status=400)
    config.proveedor_activo = proveedor
    config.actualizado_por = request.user
    config.save()
    return JsonResponse({'ok': True, **_estado_proveedor(config, proveedor)})


@login_required
@hogar_required
def api_desconectar_proveedor(request, proveedor):
    if request.method != 'POST':
        return JsonResponse({'error': 'Método no permitido.'}, status=405)
    config, error = _config_admin(request, proveedor)
    if error:
        return error
    config.desconectar(proveedor)
    config.actualizado_por = request.user
    config.save()
    return JsonResponse({'ok': True, **_estado_proveedor(config, proveedor)})


@login_required
@hogar_required
def listar_agentes(request):
    profile = request.user.userprofile
    agentes = AgenteIA.objects.filter(hogar=profile.hogar)
    return render(request, 'asistente_ia/agentes_lista.html', {'agentes': agentes})


@login_required
@hogar_required
def crear_agente(request):
    profile = request.user.userprofile
    if request.method == 'POST':
        form = AgenteIAForm(request.POST)
        if form.is_valid():
            agente = form.save(commit=False)
            agente.hogar = profile.hogar
            agente.creado_por = request.user
            agente.save()
            messages.success(request, f"Agente '{agente.nombre}' creado.")
            return redirect('asistente_ia:listar_agentes')
    else:
        form = AgenteIAForm()
    return render(request, 'asistente_ia/agente_form.html', {'form': form, 'modo': 'crear'})


@login_required
@hogar_required
def editar_agente(request, agente_id):
    profile = request.user.userprofile
    agente = get_object_or_404(AgenteIA, id=agente_id, hogar=profile.hogar)
    if request.method == 'POST':
        form = AgenteIAForm(request.POST, instance=agente)
        if form.is_valid():
            form.save()
            messages.success(request, f"Agente '{agente.nombre}' actualizado.")
            return redirect('asistente_ia:listar_agentes')
    else:
        form = AgenteIAForm(instance=agente)
    return render(request, 'asistente_ia/agente_form.html', {'form': form, 'modo': 'editar', 'agente': agente})


@login_required
@hogar_required
def eliminar_agente(request, agente_id):
    profile = request.user.userprofile
    agente = get_object_or_404(AgenteIA, id=agente_id, hogar=profile.hogar)
    if agente.es_predeterminado:
        messages.error(request, "No se puede eliminar el agente predeterminado.")
        return redirect('asistente_ia:listar_agentes')
    if request.method == 'POST':
        nombre = agente.nombre
        agente.delete()
        messages.success(request, f"Agente '{nombre}' eliminado.")
        return redirect('asistente_ia:listar_agentes')
    return render(request, 'asistente_ia/agente_confirmar_eliminar.html', {'agente': agente})


# ─── API JSON para el widget de chat ─────────────────────────────────────

@login_required
@hogar_required
def api_agentes(request):
    """Agentes del hogar, para el selector dentro de la ventana de chat."""
    profile = request.user.userprofile
    agentes = AgenteIA.objects.filter(hogar=profile.hogar, activo=True).values(
        'id', 'nombre', 'descripcion', 'es_predeterminado'
    )
    return JsonResponse({'agentes': list(agentes)})


@login_required
@hogar_required
def api_cambiar_agente(request, conv_id):
    """Cambia (o quita) el agente de una conversación ya abierta."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Método no permitido.'}, status=405)
    profile = request.user.userprofile
    conversacion = get_object_or_404(
        ConversacionIA, id=conv_id, usuario=request.user, hogar=profile.hogar
    )
    try:
        data = json.loads(request.body or b'{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Solicitud inválida.'}, status=400)

    agente_id = data.get('agente_id')
    if agente_id in (None, '', 'ninguno'):
        conversacion.agente = None
        conversacion.titulo = 'Sin agente'
    else:
        agente = get_object_or_404(
            AgenteIA, id=agente_id, hogar=profile.hogar, activo=True
        )
        conversacion.agente = agente
        conversacion.titulo = agente.nombre
    conversacion.save()
    return JsonResponse({
        'ok': True,
        'agente_id': conversacion.agente_id,
        'titulo': conversacion.titulo,
    })


@login_required
@hogar_required
def api_listar_conversaciones(request):
    profile = request.user.userprofile
    conversaciones = ConversacionIA.objects.filter(
        usuario=request.user, hogar=profile.hogar, activa=True
    ).values('id', 'titulo', 'actualizado_en')
    return JsonResponse({'conversaciones': list(conversaciones)})


@login_required
@hogar_required
def api_crear_conversacion(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Método no permitido.'}, status=405)

    profile = request.user.userprofile
    try:
        data = json.loads(request.body or b'{}')
    except json.JSONDecodeError:
        data = {}

    agente = None
    if 'agente_id' not in data:
        # Sin indicación explícita, se arranca con el agente predeterminado.
        agente = AgenteIA.objects.filter(
            hogar=profile.hogar, es_predeterminado=True, activo=True
        ).first()
    else:
        agente_id = data.get('agente_id')
        # 'ninguno' (o vacío) es una elección deliberada del usuario: sin agente.
        if agente_id not in (None, '', 'ninguno'):
            agente = AgenteIA.objects.filter(
                id=agente_id, hogar=profile.hogar, activo=True
            ).first()

    contexto = construir_contexto_financiero(profile.hogar)

    conversacion = ConversacionIA.objects.create(
        usuario=request.user,
        hogar=profile.hogar,
        agente=agente,
        titulo=agente.nombre if agente else 'Sin agente',
        contexto_financiero_cache=contexto,
        contexto_generado_en=timezone.now(),
    )
    return JsonResponse({
        'id': conversacion.id,
        'titulo': conversacion.titulo,
        'agente_id': conversacion.agente_id,
    })


@login_required
@hogar_required
def api_mensajes(request, conv_id):
    profile = request.user.userprofile
    conversacion = get_object_or_404(
        ConversacionIA, id=conv_id, usuario=request.user, hogar=profile.hogar
    )

    if request.method == 'GET':
        mensajes = list(conversacion.mensajes.values('rol', 'contenido', 'creado_en'))
        payload = {
            'mensajes': mensajes,
            'agente_id': conversacion.agente_id,
            'titulo': conversacion.titulo,
        }
        propuesta_id, datos_propuesta = acciones.obtener_propuesta_pendiente_de_conversacion(
            request.session, conversacion.id
        )
        if propuesta_id:
            payload['accion_propuesta'] = {
                'id': propuesta_id,
                'tipo': datos_propuesta['tipo'],
                'resumen': datos_propuesta['preview'],
            }
        return JsonResponse(payload)

    if request.method != 'POST':
        return JsonResponse({'error': 'Método no permitido.'}, status=405)

    try:
        data = json.loads(request.body or b'{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Solicitud inválida.'}, status=400)

    texto_usuario = (data.get('mensaje') or '').strip()
    if not texto_usuario:
        return JsonResponse({'error': 'El mensaje no puede estar vacío.'}, status=400)

    payload = procesar_mensaje(request, conversacion, texto_usuario)
    return JsonResponse(payload)


@login_required
@hogar_required
def api_confirmar_accion(request, accion_id):
    if request.method != 'POST':
        return JsonResponse({'error': 'Método no permitido.'}, status=405)
    profile = request.user.userprofile
    resultado = acciones.confirmar_propuesta(request.session, accion_id, profile.hogar, request.user)
    return JsonResponse({'mensaje': resultado.mensaje})


@login_required
@hogar_required
def api_rechazar_accion(request, accion_id):
    if request.method != 'POST':
        return JsonResponse({'error': 'Método no permitido.'}, status=405)
    mensaje = acciones.rechazar_propuesta(request.session, accion_id)
    return JsonResponse({'mensaje': mensaje})
