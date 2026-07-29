"""Framework de acciones whitelisted: propuesta → confirmación → ejecución.

La IA nunca escribe directamente en la base de datos. Cuando quiere proponer
una acción, la instrucción del sistema le pide que incluya al final de su
respuesta un bloque:

    ```accion_ia
    {"tipo": "crear_gasto", "datos": {...}}
    ```

Este módulo extrae ese bloque, valida que el tipo esté en la whitelist, crea
un registro `AccionPropuestaIA` pendiente, y expone `aplicar_accion()` para
ejecutarla solo cuando el usuario confirma explícitamente desde el chat.

Alcance intencionalmente limitado a datos de la app (nunca ficheros/código).
"""
import json
import re

from .models import AccionPropuestaIA, AgenteIA

ACCION_RE = re.compile(r"```accion_ia\s*(\{.*?\})\s*```", re.DOTALL)
HERRAMIENTA_RE = re.compile(r"```herramienta_ia\s*(\{.*?\})\s*```", re.DOTALL)

ACCIONES_PERMITIDAS = {
    'crear_gasto', 'crear_categoria_gasto', 'crear_agente', 'crear_ingreso',
}


def extraer_llamada_herramienta(texto_respuesta):
    """Detecta si el modelo ha pedido usar una herramienta de solo lectura.

    Devuelve (nombre, argumentos) o None si la respuesta es texto final.
    """
    match = HERRAMIENTA_RE.search(texto_respuesta or '')
    if not match:
        return None
    try:
        bloque = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    nombre = bloque.get('herramienta')
    if not nombre:
        return None
    return nombre, bloque.get('argumentos') or {}


def extraer_accion_propuesta(texto_respuesta, conversacion, mensaje_origen=None):
    """Si la respuesta contiene un bloque de acción válido, lo extrae, crea el
    registro pendiente y devuelve (texto_sin_el_bloque, accion_o_None)."""
    match = ACCION_RE.search(texto_respuesta)
    if not match:
        return texto_respuesta, None

    texto_limpio = ACCION_RE.sub('', texto_respuesta).strip()

    try:
        bloque = json.loads(match.group(1))
    except json.JSONDecodeError:
        return texto_limpio, None

    tipo = bloque.get('tipo')
    if tipo not in ACCIONES_PERMITIDAS:
        return texto_limpio, None

    datos = bloque.get('datos', {}) or {}
    resumen = _resumen_legible(tipo, datos)

    accion = AccionPropuestaIA.objects.create(
        conversacion=conversacion,
        mensaje_origen=mensaje_origen,
        tipo_accion=tipo,
        payload=datos,
        resumen_legible=resumen,
    )
    return texto_limpio, accion


def _resumen_legible(tipo, datos):
    if tipo == 'crear_gasto':
        return f"Crear gasto '{datos.get('nombre', '?')}' de {datos.get('importe', '?')}€ ({datos.get('periodicidad', 'mensual')})"
    if tipo == 'crear_categoria_gasto':
        return f"Crear categoría de gasto '{datos.get('nombre', '?')}'"
    if tipo == 'crear_agente':
        return f"Crear agente IA '{datos.get('nombre', '?')}'"
    if tipo == 'crear_ingreso':
        return f"Crear fuente de ingreso '{datos.get('nombre', '?')}' de {datos.get('importe', '?')}€"
    return "Acción propuesta por la IA"


def aplicar_accion(accion, hogar, usuario):
    """Ejecuta la acción confirmada. Marca el estado resultante en el propio
    registro. Nunca lanza excepción: cualquier fallo se registra en el campo
    `detalle_error` con estado 'error'."""
    from finanzas.models import PartidaGasto, CategoriaGasto, FuenteIngreso

    datos = accion.payload or {}
    try:
        if accion.tipo_accion == 'crear_gasto':
            categoria = CategoriaGasto.objects.get(hogar=hogar, nombre=datos['categoria'])
            PartidaGasto.objects.create(
                hogar=hogar,
                categoria=categoria,
                nombre=datos['nombre'],
                importe=datos['importe'],
                periodicidad=datos.get('periodicidad', 'mensual'),
            )
        elif accion.tipo_accion == 'crear_categoria_gasto':
            CategoriaGasto.objects.create(
                hogar=hogar,
                nombre=datos['nombre'],
                tipo=datos.get('tipo', 'variable'),
            )
        elif accion.tipo_accion == 'crear_agente':
            AgenteIA.objects.create(
                hogar=hogar,
                nombre=datos['nombre'],
                descripcion=datos.get('descripcion', ''),
                instrucciones=datos.get('instrucciones', ''),
                creado_por=usuario,
            )
        elif accion.tipo_accion == 'crear_ingreso':
            FuenteIngreso.objects.create(
                usuario=usuario,
                hogar=hogar,
                nombre=datos['nombre'],
                importe_declarado=datos['importe'],
                tipo=datos.get('tipo', 'fijo'),
            )
        else:
            raise ValueError(f"Tipo de acción no soportado: {accion.tipo_accion}")
        accion.marcar_resuelta('aplicada')
    except Exception as e:
        accion.marcar_resuelta('error', detalle_error=str(e))
    return accion
