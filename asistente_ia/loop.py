"""El bucle del agente: orquesta proveedor + tools + staging de escrituras
para un único turno de conversación.

Contrato:
  - Lectura ejecuta inmediatamente y el loop sigue.
  - Una escritura propuesta CORTA el turno — nada más se construye sobre un
    cambio todavía no aplicado.
  - Tope duro de iteraciones; si se agota sin respuesta final, se fuerza una
    última llamada SIN tools para garantizar una respuesta en prosa (nunca
    silencio).
  - El working-context (bloques tool_use/tool_result del turno) vive en
    `request.session`, agrupado por "turnos" completos para poder truncar
    sin romper jamás el pairing tool_use↔tool_result. El historial legible
    (texto de usuario/asistente) se persiste en `MensajeIA` para poder
    recuperar la conversación entre sesiones.
"""
import time
from dataclasses import dataclass, field

from . import acciones, herramientas
from .models import ConfiguracionIA, MensajeIA
from .prompts import construir_system_blocks
from .proveedores import ErrorProveedorIA, obtener_proveedor
from .proveedores import bloques

MAX_ITERACIONES = 7
HIST_SESSION_KEY = 'ia_historial'

# Presupuesto de reloj para un turno completo del agente. Debe quedar
# holgadamente por debajo del --timeout de gunicorn (300 s) para que el turno
# termine SIEMPRE con una respuesta en prosa antes de que el worker sea
# reiniciado (lo que dejaba al chat con "No se pudo contactar con el asistente").
PRESUPUESTO_TURNO_SEG = 230
# Techo por llamada individual al proveedor (Opus/Sonnet pueden tardar).
TIMEOUT_LLAMADA_MAX = 120
# Margen mínimo que reservamos para la llamada final sin tools.
TIMEOUT_FINAL_MIN = 25


def _timeout_restante(inicio, tope=TIMEOUT_LLAMADA_MAX):
    """Segundos que quedan del presupuesto del turno, acotados a [0, tope]."""
    restante = PRESUPUESTO_TURNO_SEG - (time.monotonic() - inicio)
    return max(0, min(int(restante), tope))


@dataclass
class ResultadoTurno:
    respuesta: str = ''
    pasos: list = field(default_factory=list)
    accion_propuesta: dict = None
    error: str = ''


def _cargar_turnos(session, conversacion_id):
    historial = session.get(HIST_SESSION_KEY, {})
    return list(historial.get(str(conversacion_id), []))


def _guardar_turnos(session, conversacion_id, turnos):
    historial = session.get(HIST_SESSION_KEY, {})
    historial[str(conversacion_id)] = bloques.truncar_turnos(turnos)
    session[HIST_SESSION_KEY] = historial
    session.modified = True


def _resolver_proveedor_y_modelo(config, agente):
    proveedor_id = config.proveedor_activo
    if agente and agente.proveedor_preferido and config.tiene_proveedor_configurado(agente.proveedor_preferido):
        proveedor_id = agente.proveedor_preferido
    if not proveedor_id or not config.tiene_proveedor_configurado(proveedor_id):
        return None, None, None
    return proveedor_id, config.get_modelo(proveedor_id), config.get_api_key(proveedor_id)


def _ejecutar_turno(request, conversacion, texto_usuario):
    """Loop puro del agente (sin persistir el mensaje de usuario en BD — eso
    lo hace el llamante para poder guardarlo aunque falle el proveedor)."""
    hogar = conversacion.hogar
    usuario = request.user
    agente = conversacion.agente
    session = request.session

    config = ConfiguracionIA.objects.filter(hogar=hogar).first()
    if not config or not config.esta_configurada:
        return ResultadoTurno(
            error='No hay ningún proveedor de IA configurado para tu hogar. '
                  'Pídele a un administrador que lo configure.'
        )

    proveedor_id, modelo, api_key = _resolver_proveedor_y_modelo(config, agente)
    if not proveedor_id:
        return ResultadoTurno(error='No hay ningún proveedor de IA configurado para tu hogar.')
    try:
        proveedor = obtener_proveedor(proveedor_id)
    except ErrorProveedorIA as e:
        return ResultadoTurno(error=str(e))

    system_blocks = construir_system_blocks(agente, conversacion.contexto_financiero_cache)
    tools = herramientas.SCHEMA_LECTURA + acciones.schema_para_agente(agente)

    turnos = _cargar_turnos(session, conversacion.id)
    turno_actual = [bloques.mensaje('user', [bloques.bloque_texto(texto_usuario)])]

    pasos = []
    accion_propuesta = None
    texto_final = ''
    turno_cortado = False
    inicio = time.monotonic()
    sin_tiempo = False

    for _ in range(MAX_ITERACIONES):
        timeout_llamada = _timeout_restante(inicio)
        # Si apenas queda presupuesto, no arrancamos otra ronda con tools:
        # salimos del bucle y forzamos la respuesta final en prosa.
        if timeout_llamada < TIMEOUT_FINAL_MIN:
            sin_tiempo = True
            break
        historial_mensajes = bloques.aplanar_turnos(turnos) + turno_actual
        respuesta = proveedor.enviar(historial_mensajes, system_blocks, tools, api_key, modelo, timeout=timeout_llamada)
        if respuesta.error:
            # Si ya teníamos pasos hechos, intentamos rematar con una respuesta
            # en prosa en vez de devolver solo el error.
            if pasos:
                sin_tiempo = True
                break
            return ResultadoTurno(error=respuesta.error, pasos=pasos)

        tool_uses = respuesta.tool_uses
        if not tool_uses:
            texto_final = respuesta.texto
            break

        turno_actual.append(bloques.mensaje('assistant', respuesta.blocks))

        resultado_bloques = []
        for tu in tool_uses:
            nombre, argumentos, tool_use_id = tu['nombre'], tu['argumentos'], tu['id']
            if nombre.startswith('propose_'):
                tipo = nombre[len('propose_'):]
                resultado_prop = acciones.proponer_accion(
                    tipo, argumentos, hogar, usuario, conversacion, session, agente=agente,
                )
                if resultado_prop.ok:
                    accion_propuesta = {
                        'id': resultado_prop.id, 'tipo': tipo, 'resumen': resultado_prop.preview,
                    }
                    pasos.append(f"Propuesta preparada: {resultado_prop.preview}")
                    resultado_bloques.append(bloques.bloque_tool_result(
                        tool_use_id,
                        f"Propuesta registrada. PENDIENTE de confirmación explícita del usuario en la "
                        f"interfaz (todavía no se ha aplicado ningún cambio): {resultado_prop.preview}",
                    ))
                    turno_cortado = True
                else:
                    pasos.append(f"Propuesta no válida: {resultado_prop.mensaje}")
                    resultado_bloques.append(bloques.bloque_tool_result(
                        tool_use_id, f"No se pudo proponer la acción: {resultado_prop.mensaje}", es_error=True,
                    ))
            else:
                resultado_texto = herramientas.ejecutar_herramienta(nombre, argumentos, hogar)
                pasos.append(herramientas.resumen_paso(nombre, argumentos))
                resultado_bloques.append(bloques.bloque_tool_result(tool_use_id, resultado_texto))

        turno_actual.append(bloques.mensaje('user', resultado_bloques))

        if turno_cortado:
            texto_final = respuesta.texto or 'He preparado una propuesta; confírmala para aplicarla.'
            break

    if not texto_final and not turno_cortado:
        # Tope de iteraciones o de tiempo agotado sin respuesta final ni
        # escritura propuesta: forzamos una última llamada SIN tools para
        # garantizar una respuesta en prosa. Nunca dejamos al usuario en
        # silencio ni con el "No se pudo contactar" del front.
        timeout_final = max(_timeout_restante(inicio), TIMEOUT_FINAL_MIN)
        historial_mensajes = bloques.aplanar_turnos(turnos) + turno_actual
        respuesta_final = proveedor.enviar(
            historial_mensajes, system_blocks, None, api_key, modelo, timeout=timeout_final,
        )
        if not respuesta_final.error and respuesta_final.texto:
            texto_final = respuesta_final.texto
        elif sin_tiempo:
            texto_final = (
                'He estado investigando pero la consulta está tardando más de lo '
                'que puedo procesar de una vez. Cuéntame algo más concreto o pídeme '
                'el análisis por partes y lo resuelvo.'
            )
        else:
            texto_final = (
                'He hecho varias consultas pero no he podido completar la respuesta. '
                'Prueba a preguntarme algo más concreto.'
            )

    turno_actual.append(bloques.mensaje('assistant', [bloques.bloque_texto(texto_final)]))

    # Defensivo: si por lo que sea el turno quedara mal pareado, preferimos
    # perder el detalle de herramientas de ESTE turno antes que persistir un
    # historial que rompería la próxima llamada al proveedor.
    if not bloques.verificar_pairing(turno_actual):
        turno_actual = [
            bloques.mensaje('user', [bloques.bloque_texto(texto_usuario)]),
            bloques.mensaje('assistant', [bloques.bloque_texto(texto_final)]),
        ]

    turnos.append(turno_actual)
    _guardar_turnos(session, conversacion.id, turnos)

    return ResultadoTurno(respuesta=texto_final, pasos=pasos, accion_propuesta=accion_propuesta)


def procesar_mensaje(request, conversacion, texto_usuario):
    """Punto de entrada desde la vista: persiste el mensaje de usuario,
    ejecuta el loop y persiste la respuesta final. Devuelve un dict listo
    para `JsonResponse`."""
    MensajeIA.objects.create(conversacion=conversacion, rol='user', contenido=texto_usuario)

    resultado = _ejecutar_turno(request, conversacion, texto_usuario)
    if resultado.error:
        return {'error': resultado.error, 'pasos': resultado.pasos}

    config = ConfiguracionIA.objects.filter(hogar=conversacion.hogar).first()
    proveedor_id, modelo, _ = _resolver_proveedor_y_modelo(config, conversacion.agente) if config else (None, None, None)

    MensajeIA.objects.create(
        conversacion=conversacion, rol='assistant', contenido=resultado.respuesta,
        proveedor=proveedor_id or '', modelo=modelo or '',
    )
    conversacion.save()  # actualiza actualizado_en

    payload = {'respuesta': resultado.respuesta, 'pasos': resultado.pasos}
    if resultado.accion_propuesta:
        payload['accion_propuesta'] = resultado.accion_propuesta
    return payload
