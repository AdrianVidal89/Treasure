"""Framework de escritura: propuesta → confirmación → aplicación.

La IA NUNCA escribe directamente en la base de datos. Cuando el modelo llama
a una tool `propose_<tipo>`, este módulo:

1. Revalida que el usuario tiene permiso de escritura (rol no-viewer).
2. RESUELVE Y VALIDA los datos contra la base de datos real (existencia de la
   categoría, tipos numéricos, elecciones válidas…) — nunca se confía en el
   payload crudo del modelo.
3. Genera una vista previa legible y la guarda como "propuesta pendiente" en
   la SESIÓN del usuario (no en base de datos: es contexto de trabajo
   efímero), con un id.

Solo cuando el usuario confirma explícitamente desde el chat se REVALIDA el
permiso otra vez (pudo cambiar entre la propuesta y la confirmación) y se
aplica el cambio real, dentro de una transacción. Cada propuesta cierra el
turno del agente: nada más se construye sobre un cambio todavía no aplicado.
"""
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from . import registro
from .models import AccionPropuestaIA, ConversacionIA

SESSION_KEY = 'ia_propuestas'


def puede_escribir(usuario):
    return registro.puede_escribir(usuario)


@dataclass
class ResultadoResolucion:
    ok: bool
    preview: str = ''
    payload: dict = field(default_factory=dict)
    error: str = ''


@dataclass
class ResultadoPropuesta:
    ok: bool
    id: str = ''
    preview: str = ''
    mensaje: str = ''


@dataclass
class ResultadoAplicacion:
    ok: bool
    mensaje: str = ''


@dataclass
class DefinicionAccion:
    tipo: str
    descripcion: str
    schema: dict
    resolver: callable  # (datos: dict, hogar) -> ResultadoResolucion
    aplicar: callable   # (payload_resuelto: dict, hogar, usuario) -> None (lanza si falla)


# ─── Resolutores: crear_gasto / editar_gasto ──────────────────────────────

def _a_decimal_positivo(valor, nombre_campo):
    try:
        numero = Decimal(str(valor))
    except (InvalidOperation, TypeError):
        raise ValueError(f"'{nombre_campo}' debe ser un número.")
    if numero <= 0:
        raise ValueError(f"'{nombre_campo}' debe ser mayor que 0.")
    return numero


def _resolver_crear_gasto(datos, hogar):
    from finanzas.models import CategoriaGasto, PERIODICIDAD_GASTO_CHOICES

    nombre = (datos.get('nombre') or '').strip()
    if not nombre:
        return ResultadoResolucion(False, error="Falta el nombre del gasto.")
    try:
        importe = _a_decimal_positivo(datos.get('importe'), 'importe')
    except ValueError as e:
        return ResultadoResolucion(False, error=str(e))

    nombre_categoria = (datos.get('categoria') or '').strip()
    categoria = CategoriaGasto.objects.filter(hogar=hogar, nombre__iexact=nombre_categoria).first()
    if not categoria:
        disponibles = ', '.join(CategoriaGasto.objects.filter(hogar=hogar).values_list('nombre', flat=True))
        return ResultadoResolucion(
            False,
            error=f"No existe la categoría '{nombre_categoria}' en este hogar. Disponibles: {disponibles or '(ninguna todavía)'}",
        )

    periodicidad = datos.get('periodicidad') or 'mensual'
    if periodicidad not in dict(PERIODICIDAD_GASTO_CHOICES):
        return ResultadoResolucion(False, error=f"Periodicidad no válida: '{periodicidad}'")

    preview = f"Crear gasto «{nombre}» de {importe}€ ({periodicidad}) en la categoría «{categoria.nombre}»"
    payload = {
        'categoria_id': categoria.id, 'nombre': nombre,
        'importe': float(importe), 'periodicidad': periodicidad,
    }
    return ResultadoResolucion(True, preview=preview, payload=payload)


def _aplicar_crear_gasto(payload, hogar, usuario):
    from finanzas.models import CategoriaGasto, PartidaGasto
    categoria = CategoriaGasto.objects.get(hogar=hogar, id=payload['categoria_id'])
    PartidaGasto.objects.create(
        hogar=hogar, categoria=categoria, nombre=payload['nombre'],
        importe=payload['importe'], periodicidad=payload['periodicidad'],
    )


def _resolver_editar_gasto(datos, hogar):
    from finanzas.models import CategoriaGasto, PartidaGasto, PERIODICIDAD_GASTO_CHOICES

    id_gasto = datos.get('id')
    partida = PartidaGasto.objects.filter(hogar=hogar, id=id_gasto).first()
    if not partida:
        return ResultadoResolucion(False, error=f"No existe ninguna partida de gasto con id {id_gasto} en este hogar.")

    cambios = {}
    descripcion_cambios = []

    if 'importe' in datos and datos.get('importe') is not None:
        try:
            importe = _a_decimal_positivo(datos.get('importe'), 'importe')
        except ValueError as e:
            return ResultadoResolucion(False, error=str(e))
        if importe != partida.importe:
            cambios['importe'] = float(importe)
            descripcion_cambios.append(f"importe {partida.importe}€ → {importe}€")

    if 'periodicidad' in datos and datos.get('periodicidad'):
        periodicidad = datos['periodicidad']
        if periodicidad not in dict(PERIODICIDAD_GASTO_CHOICES):
            return ResultadoResolucion(False, error=f"Periodicidad no válida: '{periodicidad}'")
        if periodicidad != partida.periodicidad:
            cambios['periodicidad'] = periodicidad
            descripcion_cambios.append(f"periodicidad {partida.periodicidad} → {periodicidad}")

    if 'categoria' in datos and datos.get('categoria'):
        nombre_categoria = datos['categoria'].strip()
        categoria = CategoriaGasto.objects.filter(hogar=hogar, nombre__iexact=nombre_categoria).first()
        if not categoria:
            return ResultadoResolucion(False, error=f"No existe la categoría '{nombre_categoria}' en este hogar.")
        if categoria.id != partida.categoria_id:
            cambios['categoria_id'] = categoria.id
            descripcion_cambios.append(f"categoría {partida.categoria.nombre} → {categoria.nombre}")

    if 'nombre' in datos and datos.get('nombre'):
        nuevo_nombre = datos['nombre'].strip()
        if nuevo_nombre and nuevo_nombre != partida.nombre:
            cambios['nombre'] = nuevo_nombre
            descripcion_cambios.append(f"nombre «{partida.nombre}» → «{nuevo_nombre}»")

    if not cambios:
        return ResultadoResolucion(False, error="No hay ningún cambio que aplicar (los valores coinciden con los actuales).")

    preview = f"Editar gasto «{partida.nombre}»: " + ', '.join(descripcion_cambios)
    return ResultadoResolucion(True, preview=preview, payload={'id': partida.id, 'cambios': cambios})


def _aplicar_editar_gasto(payload, hogar, usuario):
    from finanzas.models import PartidaGasto
    partida = PartidaGasto.objects.get(hogar=hogar, id=payload['id'])
    for campo, valor in payload['cambios'].items():
        setattr(partida, campo, valor)
    partida.save(update_fields=list(payload['cambios']))


# ─── Resolutores: crear_categoria_gasto ───────────────────────────────────

def _resolver_crear_categoria_gasto(datos, hogar):
    from finanzas.models import CategoriaGasto, TIPO_GASTO_CHOICES

    nombre = (datos.get('nombre') or '').strip()
    if not nombre:
        return ResultadoResolucion(False, error="Falta el nombre de la categoría.")
    if CategoriaGasto.objects.filter(hogar=hogar, nombre__iexact=nombre).exists():
        return ResultadoResolucion(False, error=f"Ya existe una categoría llamada «{nombre}» en este hogar.")

    tipo = datos.get('tipo') or 'variable'
    if tipo not in dict(TIPO_GASTO_CHOICES):
        return ResultadoResolucion(False, error=f"Tipo de categoría no válido: '{tipo}'")

    preview = f"Crear categoría de gasto «{nombre}» ({dict(TIPO_GASTO_CHOICES)[tipo]})"
    return ResultadoResolucion(True, preview=preview, payload={'nombre': nombre, 'tipo': tipo})


def _aplicar_crear_categoria_gasto(payload, hogar, usuario):
    from finanzas.models import CategoriaGasto
    CategoriaGasto.objects.create(hogar=hogar, nombre=payload['nombre'], tipo=payload['tipo'])


# ─── Resolutores: crear_ingreso / editar_ingreso ──────────────────────────

def _resolver_crear_ingreso(datos, hogar, usuario):
    from finanzas.models import FuenteIngreso

    nombre = (datos.get('nombre') or '').strip()
    if not nombre:
        return ResultadoResolucion(False, error="Falta el nombre de la fuente de ingreso.")
    try:
        importe = _a_decimal_positivo(datos.get('importe'), 'importe')
    except ValueError as e:
        return ResultadoResolucion(False, error=str(e))

    tipo = datos.get('tipo') or 'fijo'
    if tipo not in dict(FuenteIngreso.TIPO_CHOICES):
        return ResultadoResolucion(False, error=f"Tipo de ingreso no válido: '{tipo}'")

    modo_entrada = datos.get('modo_entrada') or 'anual'
    if modo_entrada not in dict(FuenteIngreso.MODO_ENTRADA_CHOICES):
        return ResultadoResolucion(False, error=f"Modo de entrada no válido: '{modo_entrada}'")

    etiqueta_modo = 'anual' if modo_entrada == 'anual' else 'por periodo'
    preview = f"Crear ingreso «{nombre}» de {importe}€ ({etiqueta_modo}, {tipo})"
    payload = {
        'nombre': nombre, 'importe': float(importe), 'tipo': tipo, 'modo_entrada': modo_entrada,
    }
    return ResultadoResolucion(True, preview=preview, payload=payload)


def _aplicar_crear_ingreso(payload, hogar, usuario):
    from finanzas.models import FuenteIngreso
    FuenteIngreso.objects.create(
        usuario=usuario, hogar=hogar, nombre=payload['nombre'],
        importe_declarado=payload['importe'], tipo=payload['tipo'],
        modo_entrada=payload['modo_entrada'],
    )


def _resolver_editar_ingreso(datos, hogar, usuario):
    from finanzas.models import FuenteIngreso

    id_fuente = datos.get('id')
    fuente = FuenteIngreso.objects.filter(hogar=hogar, id=id_fuente).first()
    if not fuente:
        return ResultadoResolucion(False, error=f"No existe ninguna fuente de ingreso con id {id_fuente} en este hogar.")

    cambios = {}
    descripcion_cambios = []

    if 'importe' in datos and datos.get('importe') is not None:
        try:
            importe = _a_decimal_positivo(datos.get('importe'), 'importe')
        except ValueError as e:
            return ResultadoResolucion(False, error=str(e))
        if importe != fuente.importe_declarado:
            cambios['importe_declarado'] = float(importe)
            descripcion_cambios.append(f"importe {fuente.importe_declarado}€ → {importe}€")

    if 'activo' in datos and datos.get('activo') is not None:
        nuevo_activo = bool(datos['activo'])
        if nuevo_activo != fuente.activo:
            cambios['activo'] = nuevo_activo
            descripcion_cambios.append('activada' if nuevo_activo else 'desactivada')

    if not cambios:
        return ResultadoResolucion(False, error="No hay ningún cambio que aplicar (los valores coinciden con los actuales).")

    preview = f"Editar ingreso «{fuente.nombre}»: " + ', '.join(descripcion_cambios)
    return ResultadoResolucion(True, preview=preview, payload={'id': fuente.id, 'cambios': cambios})


def _aplicar_editar_ingreso(payload, hogar, usuario):
    from finanzas.models import FuenteIngreso
    fuente = FuenteIngreso.objects.get(hogar=hogar, id=payload['id'])
    for campo, valor in payload['cambios'].items():
        setattr(fuente, campo, valor)
    fuente.save(update_fields=list(payload['cambios']))


# ─── Resolutor: crear_agente ───────────────────────────────────────────────

def _resolver_crear_agente(datos, hogar, usuario):
    from .models import AgenteIA

    nombre = (datos.get('nombre') or '').strip()
    if not nombre:
        return ResultadoResolucion(False, error="Falta el nombre del agente.")
    if AgenteIA.objects.filter(hogar=hogar, nombre__iexact=nombre).exists():
        return ResultadoResolucion(False, error=f"Ya existe un agente llamado «{nombre}» en este hogar.")
    instrucciones = (datos.get('instrucciones') or '').strip()
    if not instrucciones:
        return ResultadoResolucion(False, error="Falta el system prompt (instrucciones) del agente.")

    preview = f"Crear agente «{nombre}» (solo lectura; sus permisos de escritura se ajustan luego desde Agentes)"
    payload = {
        'nombre': nombre, 'descripcion': (datos.get('descripcion') or '').strip(),
        'instrucciones': instrucciones,
    }
    return ResultadoResolucion(True, preview=preview, payload=payload)


def _aplicar_crear_agente(payload, hogar, usuario):
    from .models import AgenteIA
    AgenteIA.objects.create(
        hogar=hogar, nombre=payload['nombre'], descripcion=payload['descripcion'],
        instrucciones=payload['instrucciones'], creado_por=usuario, allowed_tools=[],
    )


# ─── Registro de acciones curadas ─────────────────────────────────────────
# NOTA: nunca se registra un "delete" — solo create/update, por seguridad.

ACCIONES = {
    'crear_gasto': DefinicionAccion(
        tipo='crear_gasto',
        descripcion='Propone crear una partida de gasto nueva en una categoría existente.',
        schema={
            'type': 'object',
            'properties': {
                'nombre': {'type': 'string'},
                'importe': {'type': 'number'},
                'categoria': {'type': 'string', 'description': 'Nombre de una categoría de gasto existente'},
                'periodicidad': {'type': 'string', 'enum': ['mensual', 'bimensual', 'trimestral', 'semestral', 'anual']},
            },
            'required': ['nombre', 'importe', 'categoria'],
        },
        resolver=lambda datos, hogar: _resolver_crear_gasto(datos, hogar),
        aplicar=_aplicar_crear_gasto,
    ),
    'editar_gasto': DefinicionAccion(
        tipo='editar_gasto',
        descripcion='Propone modificar una partida de gasto existente (importe, periodicidad, categoría o nombre).',
        schema={
            'type': 'object',
            'properties': {
                'id': {'type': 'integer', 'description': 'id de la partida de gasto a editar'},
                'nombre': {'type': 'string'},
                'importe': {'type': 'number'},
                'categoria': {'type': 'string'},
                'periodicidad': {'type': 'string', 'enum': ['mensual', 'bimensual', 'trimestral', 'semestral', 'anual']},
            },
            'required': ['id'],
        },
        resolver=lambda datos, hogar: _resolver_editar_gasto(datos, hogar),
        aplicar=_aplicar_editar_gasto,
    ),
    'crear_categoria_gasto': DefinicionAccion(
        tipo='crear_categoria_gasto',
        descripcion='Propone crear una categoría de gasto nueva.',
        schema={
            'type': 'object',
            'properties': {
                'nombre': {'type': 'string'},
                'tipo': {'type': 'string', 'enum': ['fijo', 'anual', 'variable']},
            },
            'required': ['nombre'],
        },
        resolver=lambda datos, hogar: _resolver_crear_categoria_gasto(datos, hogar),
        aplicar=_aplicar_crear_categoria_gasto,
    ),
    'crear_ingreso': DefinicionAccion(
        tipo='crear_ingreso',
        descripcion='Propone crear una fuente de ingreso nueva.',
        schema={
            'type': 'object',
            'properties': {
                'nombre': {'type': 'string'},
                'importe': {'type': 'number'},
                'tipo': {'type': 'string', 'enum': ['fijo', 'variable']},
                'modo_entrada': {'type': 'string', 'enum': ['anual', 'periodo'], 'description': 'Si el importe es el total anual o por periodo'},
            },
            'required': ['nombre', 'importe'],
        },
        resolver=None,  # requiere `usuario`; se resuelve vía _resolver_con_usuario en tiempo de registro
        aplicar=_aplicar_crear_ingreso,
    ),
    'editar_ingreso': DefinicionAccion(
        tipo='editar_ingreso',
        descripcion='Propone modificar el importe o el estado activo de una fuente de ingreso existente.',
        schema={
            'type': 'object',
            'properties': {
                'id': {'type': 'integer', 'description': 'id de la fuente de ingreso a editar'},
                'importe': {'type': 'number'},
                'activo': {'type': 'boolean'},
            },
            'required': ['id'],
        },
        resolver=None,
        aplicar=_aplicar_editar_ingreso,
    ),
    'crear_agente': DefinicionAccion(
        tipo='crear_agente',
        descripcion='Propone crear un nuevo agente (preset) de IA para este hogar. Se crea sin permisos de escritura; se ajustan luego desde Agentes.',
        schema={
            'type': 'object',
            'properties': {
                'nombre': {'type': 'string'},
                'descripcion': {'type': 'string'},
                'instrucciones': {'type': 'string', 'description': 'System prompt del agente'},
            },
            'required': ['nombre', 'instrucciones'],
        },
        resolver=None,
        aplicar=_aplicar_crear_agente,
    ),
}

# Las acciones que necesitan `usuario` en el resolutor se envuelven aparte
# (Python no permite closures sobre variables aún no definidas arriba).
ACCIONES['crear_ingreso'].resolver = _resolver_crear_ingreso
ACCIONES['editar_ingreso'].resolver = _resolver_editar_ingreso
ACCIONES['crear_agente'].resolver = _resolver_crear_agente
# Las tres anteriores reciben (datos, hogar, usuario); las demás (datos, hogar).
_ACCIONES_CON_USUARIO = {'crear_ingreso', 'editar_ingreso', 'crear_agente'}


def tipos_permitidos_para(agente):
    """Sin agente seleccionado: comportamiento histórico (todas las curadas
    disponibles). Con agente: exactamente su `allowed_tools` (vacío = solo lectura)."""
    if agente is None:
        return set(ACCIONES)
    return {t for t in (agente.allowed_tools or []) if t in ACCIONES}


def schema_para_agente(agente):
    """Tools `propose_<tipo>` que se ofrecen al proveedor para este turno."""
    tipos = tipos_permitidos_para(agente)
    return [
        {'name': f'propose_{tipo}', 'description': definicion.descripcion, 'parametros': definicion.schema}
        for tipo, definicion in ACCIONES.items() if tipo in tipos
    ]


# ─── Propuesta (en sesión) ─────────────────────────────────────────────────

def _resolver(definicion, datos, hogar, usuario):
    if definicion.tipo in _ACCIONES_CON_USUARIO:
        return definicion.resolver(datos, hogar, usuario)
    return definicion.resolver(datos, hogar)


def proponer_accion(tipo, datos, hogar, usuario, conversacion, session, agente=None):
    """Punto de entrada desde el loop cuando el modelo llama `propose_<tipo>`."""
    if tipo not in ACCIONES:
        return ResultadoPropuesta(False, mensaje=f"Tipo de acción desconocido: '{tipo}'.")
    if tipo not in tipos_permitidos_para(agente):
        return ResultadoPropuesta(False, mensaje="Este agente no tiene permiso para proponer esa acción.")
    if not puede_escribir(usuario):
        return ResultadoPropuesta(False, mensaje="No tienes permiso de escritura en este hogar (tu rol es de solo lectura).")

    definicion = ACCIONES[tipo]
    try:
        resultado = _resolver(definicion, datos or {}, hogar, usuario)
    except Exception as e:  # noqa: BLE001 — nunca debe tumbar el chat
        return ResultadoPropuesta(False, mensaje=f"No se pudo validar la propuesta: {e}")
    if not resultado.ok:
        return ResultadoPropuesta(False, mensaje=resultado.error)

    propuesta_id = uuid.uuid4().hex
    _guardar_propuesta_sesion(session, propuesta_id, {
        'tipo': tipo,
        'payload': resultado.payload,
        'preview': resultado.preview,
        'hogar_id': hogar.id,
        'conversacion_id': conversacion.id,
        'creado_en': timezone.now().isoformat(),
    })
    return ResultadoPropuesta(True, id=propuesta_id, preview=resultado.preview)


def _propuestas_sesion(session):
    return session.get(SESSION_KEY, {})


def _guardar_propuesta_sesion(session, propuesta_id, datos):
    propuestas = _propuestas_sesion(session)
    propuestas[propuesta_id] = datos
    session[SESSION_KEY] = propuestas
    session.modified = True


def obtener_propuesta_sesion(session, propuesta_id):
    return _propuestas_sesion(session).get(propuesta_id)


def obtener_propuesta_pendiente_de_conversacion(session, conversacion_id):
    for propuesta_id, datos in _propuestas_sesion(session).items():
        if datos.get('conversacion_id') == conversacion_id:
            return propuesta_id, datos
    return None, None


def descartar_propuesta_sesion(session, propuesta_id):
    propuestas = _propuestas_sesion(session)
    if propuestas.pop(propuesta_id, None) is not None:
        session[SESSION_KEY] = propuestas
        session.modified = True


# ─── Confirmación / rechazo ─────────────────────────────────────────────────

def _auditar(datos, estado, detalle_error=''):
    """Registro de auditoría best-effort de acciones ya resueltas (aplicadas o
    con error). Nunca debe romper el flujo de confirmación si falla."""
    try:
        conversacion = ConversacionIA.objects.filter(id=datos.get('conversacion_id')).first()
        if not conversacion:
            return
        AccionPropuestaIA.objects.create(
            conversacion=conversacion,
            tipo_accion=datos['tipo'],
            payload=datos['payload'],
            resumen_legible=datos['preview'],
            estado=estado,
            detalle_error=detalle_error,
            resuelto_en=timezone.now(),
        )
    except Exception:
        pass


def confirmar_propuesta(session, propuesta_id, hogar, usuario):
    """Aplica una propuesta pendiente. Revalida permisos de nuevo: el rol del
    usuario pudo cambiar entre la propuesta y esta confirmación."""
    datos = obtener_propuesta_sesion(session, propuesta_id)
    if not datos:
        return ResultadoAplicacion(False, mensaje="Esta propuesta ya no está disponible (pudo expirar o resolverse ya).")
    if datos.get('hogar_id') != hogar.id:
        return ResultadoAplicacion(False, mensaje="Esta propuesta no pertenece a tu hogar.")
    if not puede_escribir(usuario):
        descartar_propuesta_sesion(session, propuesta_id)
        return ResultadoAplicacion(False, mensaje="Ya no tienes permiso de escritura en este hogar.")

    definicion = ACCIONES.get(datos['tipo'])
    if not definicion:
        descartar_propuesta_sesion(session, propuesta_id)
        return ResultadoAplicacion(False, mensaje="Este tipo de acción ya no está soportado.")

    try:
        with transaction.atomic():
            definicion.aplicar(datos['payload'], hogar, usuario)
        _auditar(datos, 'aplicada')
        descartar_propuesta_sesion(session, propuesta_id)
        return ResultadoAplicacion(True, mensaje=f"{datos['preview']} — aplicado.")
    except Exception as e:
        detalle = str(e)
        _auditar(datos, 'error', detalle_error=detalle)
        descartar_propuesta_sesion(session, propuesta_id)
        return ResultadoAplicacion(False, mensaje=f"No se pudo aplicar la acción: {detalle}")


def rechazar_propuesta(session, propuesta_id):
    datos = obtener_propuesta_sesion(session, propuesta_id)
    descartar_propuesta_sesion(session, propuesta_id)
    if not datos:
        return "Esta propuesta ya no estaba disponible."
    return "Propuesta descartada."
