"""Construcción del system prompt: instrucciones del agente + reglas fijas +
el briefing del hogar, devuelto como bloques `system` neutros (ver
`proveedores.bloques`) para que el proveedor pueda marcar el briefing como
cacheable.
"""

INSTRUCCIONES_SIN_AGENTE = (
    "Eres el asistente de IA de Treasure, la app de finanzas de este hogar. "
    "Ayudas con finanzas familiares, gestión patrimonial y análisis de activos. "
    "Responde siempre en español."
)

# Eficiencia: el resumen ya trae cuentas, ingresos, gastos (con importes) y
# fondos. Responder desde ahí cuando se pueda evita encadenar llamadas al
# proveedor (cada una es un ida y vuelta lento), así que el chat va más ágil.
REGLA_EFICIENCIA = (
    "\n\nEFICIENCIA: el RESUMEN INICIAL DEL HOGAR ya incluye tus cuentas, ingresos y "
    "gastos con sus importes, y tus fondos. Si la pregunta se puede responder con esa "
    "información, respóndela DIRECTAMENTE, sin usar herramientas. Recurre a las "
    "herramientas solo para datos que no estén en el resumen, para la cifra exacta de un "
    "registro concreto, o para confirmar antes de negar que algo existe. No hagas más "
    "consultas de las estrictamente necesarias."
)

# Regla no negociable: el modelo nunca debe poder decir "esto no está en el
# briefing" como si fuera lo mismo que "esto no existe" — el resumen inicial
# es solo orientativo y puede estar truncado.
REGLA_CONSULTAR_ANTES_DE_NEGAR = (
    "\n\nREGLA IMPORTANTE: el RESUMEN INICIAL DEL HOGAR puede estar incompleto o "
    "truncado. Nunca digas que un dato \"no existe\" o \"no está disponible\" basándote "
    "solo en que no aparezca en el resumen — antes de negar algo, comprueba con search, "
    "get_item o query. Si tras consultar sigue sin aparecer, entonces sí puedes decir "
    "que no existe."
)

INSTRUCCIONES_ACCIONES = (
    "\n\nSi el usuario te pide crear o modificar algo en la app y tienes datos "
    "suficientes, usa la tool 'propose_<tipo>' correspondiente si está disponible. "
    "El resultado de esa tool es una PROPUESTA pendiente de confirmación: nunca digas "
    "que ya se ha aplicado el cambio, solo que has preparado la propuesta para que el "
    "usuario la confirme desde la interfaz. No propongas cambios a ficheros o código "
    "de la aplicación, ni intentes forzar una tool que no esté entre las disponibles."
)


def construir_system_blocks(agente, briefing_texto):
    """Devuelve `system_blocks` — lista de {'texto','cacheable'} — para pasar
    directamente a `ProveedorLLM.enviar`. El briefing se marca cacheable
    porque es idéntico turno a turno dentro de una misma conversación."""
    instrucciones = agente.instrucciones if agente else INSTRUCCIONES_SIN_AGENTE
    instrucciones_completas = (
        instrucciones + INSTRUCCIONES_ACCIONES + REGLA_EFICIENCIA + REGLA_CONSULTAR_ANTES_DE_NEGAR
    )
    return [
        {'texto': instrucciones_completas, 'cacheable': False},
        {
            'texto': (
                'RESUMEN INICIAL DEL HOGAR (orientativo, puede estar truncado — '
                'amplíalo con search/get_item/query):\n' + (briefing_texto or '')
            ),
            'cacheable': True,
        },
    ]
