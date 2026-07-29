import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from core.mixins import hogar_required

from .acciones import aplicar_accion, extraer_accion_propuesta
from .contexto import construir_contexto_financiero
from .forms import AgenteIAForm
from .models import AccionPropuestaIA, AgenteIA, ConfiguracionIA, ConversacionIA, MensajeIA
from .providers import ErrorProveedorIA, enviar_mensaje, listar_modelos

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
    agente_id = data.get('agente_id')
    if agente_id:
        agente = AgenteIA.objects.filter(id=agente_id, hogar=profile.hogar, activo=True).first()
    if not agente:
        agente = AgenteIA.objects.filter(hogar=profile.hogar, es_predeterminado=True, activo=True).first()

    contexto = construir_contexto_financiero(profile.hogar)

    conversacion = ConversacionIA.objects.create(
        usuario=request.user,
        hogar=profile.hogar,
        agente=agente,
        titulo=agente.nombre if agente else 'Nueva conversación',
        contexto_financiero_cache=contexto,
    )
    return JsonResponse({'id': conversacion.id, 'titulo': conversacion.titulo})


@login_required
@hogar_required
def api_mensajes(request, conv_id):
    profile = request.user.userprofile
    conversacion = get_object_or_404(
        ConversacionIA, id=conv_id, usuario=request.user, hogar=profile.hogar
    )

    if request.method == 'GET':
        mensajes = list(conversacion.mensajes.values('rol', 'contenido', 'creado_en'))
        payload = {'mensajes': mensajes}
        pendiente = conversacion.acciones_propuestas.filter(estado='pendiente').first()
        if pendiente:
            payload['accion_propuesta'] = {
                'id': pendiente.id,
                'tipo': pendiente.tipo_accion,
                'resumen': pendiente.resumen_legible,
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

    mensaje_usuario = MensajeIA.objects.create(
        conversacion=conversacion, rol='user', contenido=texto_usuario,
    )

    config = ConfiguracionIA.objects.filter(hogar=profile.hogar).first()
    if not config or not config.esta_configurada:
        return JsonResponse({
            'error': 'No hay ningún proveedor de IA configurado para tu hogar. '
                     'Pídele a un administrador que lo configure.'
        })

    proveedor = config.proveedor_activo
    agente = conversacion.agente
    instrucciones = agente.instrucciones if agente else (
        "Eres el asistente de IA de Treasure. Ayuda al usuario con sus finanzas."
    )
    instrucciones_acciones = (
        "\n\nSi el usuario te pide crear algo en la app (un gasto, una categoría de "
        "gasto, un agente o una fuente de ingreso), y tienes datos suficientes, "
        "incluye al final de tu respuesta, en un bloque separado, exactamente esto "
        "(sustituyendo los valores): ```accion_ia\n"
        '{"tipo": "crear_gasto|crear_categoria_gasto|crear_agente|crear_ingreso", '
        '"datos": {...}}\n```\n'
        "El usuario deberá confirmar la acción antes de que se aplique. Nunca "
        "propongas cambios a ficheros o código de la aplicación."
    )
    system_prompt = instrucciones + instrucciones_acciones + "\n\n" + conversacion.contexto_financiero_cache

    historial = [
        {'rol': m['rol'], 'contenido': m['contenido']}
        for m in conversacion.mensajes.exclude(rol='system').values('rol', 'contenido')
    ]

    try:
        respuesta = enviar_mensaje(
            proveedor,
            config.get_api_key(proveedor),
            config.get_modelo(proveedor),
            historial,
            system_prompt,
        )
    except ErrorProveedorIA as e:
        return JsonResponse({'error': str(e)})

    respuesta_limpia, accion_propuesta = extraer_accion_propuesta(
        respuesta, conversacion, mensaje_origen=mensaje_usuario
    )

    mensaje_asistente = MensajeIA.objects.create(
        conversacion=conversacion, rol='assistant', contenido=respuesta_limpia,
        proveedor=proveedor, modelo=config.get_modelo(proveedor),
    )
    if accion_propuesta:
        accion_propuesta.mensaje_origen = mensaje_asistente
        accion_propuesta.save(update_fields=['mensaje_origen'])

    conversacion.save()  # actualiza actualizado_en

    payload = {'respuesta': respuesta_limpia}
    if accion_propuesta:
        payload['accion_propuesta'] = {
            'id': accion_propuesta.id,
            'tipo': accion_propuesta.tipo_accion,
            'resumen': accion_propuesta.resumen_legible,
        }
    return JsonResponse(payload)


@login_required
@hogar_required
def api_confirmar_accion(request, accion_id):
    if request.method != 'POST':
        return JsonResponse({'error': 'Método no permitido.'}, status=405)
    profile = request.user.userprofile
    accion = get_object_or_404(
        AccionPropuestaIA, id=accion_id,
        conversacion__usuario=request.user, conversacion__hogar=profile.hogar,
    )
    if accion.estado != 'pendiente':
        return JsonResponse({'mensaje': 'Esta acción ya había sido resuelta.'})

    accion.estado = 'confirmada'
    accion.save(update_fields=['estado'])
    aplicar_accion(accion, profile.hogar, request.user)

    if accion.estado == 'aplicada':
        return JsonResponse({'mensaje': f'✅ {accion.resumen_legible} — aplicado.'})
    return JsonResponse({'mensaje': f'⚠️ No se pudo aplicar la acción: {accion.detalle_error}'})


@login_required
@hogar_required
def api_rechazar_accion(request, accion_id):
    if request.method != 'POST':
        return JsonResponse({'error': 'Método no permitido.'}, status=405)
    profile = request.user.userprofile
    accion = get_object_or_404(
        AccionPropuestaIA, id=accion_id,
        conversacion__usuario=request.user, conversacion__hogar=profile.hogar,
    )
    if accion.estado == 'pendiente':
        accion.marcar_resuelta('rechazada')
    return JsonResponse({'mensaje': 'Acción descartada.'})
