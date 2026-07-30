"""Formato neutro de mensajes, común a los tres proveedores.

Un mensaje es `{'role': 'user'|'assistant', 'blocks': [bloque, ...]}` y cada
bloque es uno de:
  - texto:        {'tipo': 'text', 'texto': str}
  - uso_tool:     {'tipo': 'tool_use', 'id': str, 'nombre': str, 'argumentos': dict}
  - resultado_tool:{'tipo': 'tool_result', 'tool_use_id': str, 'contenido': str, 'es_error': bool}

Son diccionarios planos (no dataclasses) a propósito: así se guardan tal
cual en `request.session` (JSON) sin pasos de serialización adicionales.

La invariante que hay que proteger SIEMPRE es que un `tool_use` nunca quede
sin su `tool_result` correspondiente — rompe a los tres proveedores por
igual. Este módulo agrupa la conversación en "turnos" (todo lo que genera un
único mensaje del usuario) para poder truncar el historial cuando crece sin
arriesgarse a cortar un turno por la mitad.
"""


def bloque_texto(texto):
    return {'tipo': 'text', 'texto': texto or ''}


def bloque_tool_use(id, nombre, argumentos):
    return {'tipo': 'tool_use', 'id': id, 'nombre': nombre, 'argumentos': argumentos or {}}


def bloque_tool_result(tool_use_id, contenido, es_error=False):
    return {'tipo': 'tool_result', 'tool_use_id': tool_use_id, 'contenido': contenido, 'es_error': bool(es_error)}


def mensaje(role, blocks):
    return {'role': role, 'blocks': list(blocks)}


def texto_de_mensaje(msg):
    return ''.join(b['texto'] for b in msg.get('blocks', []) if b.get('tipo') == 'text')


def tool_uses_de_mensaje(msg):
    return [b for b in msg.get('blocks', []) if b.get('tipo') == 'tool_use']


def verificar_pairing(mensajes):
    """Comprueba que cada tool_use de un mensaje 'assistant' tiene su
    tool_result correspondiente en el mensaje inmediatamente siguiente.
    Devuelve True/False; no lanza (se usa como aserción defensiva antes de
    enviar al proveedor, nunca debería fallar si el loop está bien escrito)."""
    for i, msg in enumerate(mensajes):
        ids_tool_use = {b['id'] for b in tool_uses_de_mensaje(msg)}
        if not ids_tool_use:
            continue
        if i + 1 >= len(mensajes):
            return False
        siguiente = mensajes[i + 1]
        ids_resultado = {
            b['tool_use_id'] for b in siguiente.get('blocks', []) if b.get('tipo') == 'tool_result'
        }
        if not ids_tool_use.issubset(ids_resultado):
            return False
    return True


# ─── Turnos (agrupación para truncado seguro) ─────────────────────────────
# Un "turno" es la lista de mensajes generados a partir de UN mensaje de
# usuario: [user(texto), assistant(tool_use...), user(tool_result...), ...,
# assistant(texto final)]. Truncar por turnos completos garantiza que nunca
# se corta un tool_use de su tool_result.

MAX_TURNOS_HISTORIAL = 8


def truncar_turnos(turnos, max_turnos=MAX_TURNOS_HISTORIAL):
    if len(turnos) <= max_turnos:
        return turnos
    return turnos[-max_turnos:]


def aplanar_turnos(turnos):
    mensajes = []
    for turno in turnos:
        mensajes.extend(turno)
    return mensajes
