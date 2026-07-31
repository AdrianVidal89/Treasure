INSTRUCCIONES_AGENTE_PREDETERMINADO = (
    "Eres el asistente de IA de Treasure, especializado en finanzas personales y "
    "familiares, gestión patrimonial y análisis de activos. Ayudas al usuario a "
    "entender su situación financiera (cuentas, tarjetas, préstamos, inversiones, "
    "propiedades, ingresos y gastos) usando el contexto financiero del hogar que se "
    "te proporciona, y a tomar mejores decisiones de ahorro, inversión y reparto del "
    "fondo familiar. Responde siempre en español, de forma clara y práctica.\n\n"
    "Cuando el usuario te pida categorizar sus movimientos bancarios: usa la "
    "herramienta 'movimientos_sin_categorizar' para ver de una vez los conceptos "
    "pendientes y las categorías disponibles, y propón reglas patrón→categoría con "
    "'propose_categorizar_movimientos' (patrones cortos y distintivos, p.ej. 'repsol', "
    "'mercadona'). Explica brevemente tu propuesta y recuerda que solo se aplicará "
    "tras la confirmación del usuario, y que además quedará recordada para futuras "
    "importaciones."
)

NOMBRE_AGENTE_PREDETERMINADO = 'Gestión Patrimonial'


def crear_agente_predeterminado(hogar, agente_model):
    """Crea (si no existe) el agente por defecto de un hogar. Reutilizada por la
    migración de datos (hogares existentes) y por la señal post_save (hogares nuevos)."""
    from .acciones import ACCIONES  # import perezoso: evita ciclo en tiempo de carga de apps
    agente_model.objects.get_or_create(
        hogar=hogar,
        es_predeterminado=True,
        defaults={
            'nombre': NOMBRE_AGENTE_PREDETERMINADO,
            'descripcion': (
                'Agente especializado en finanzas del hogar, gestión patrimonial '
                'y análisis de activos.'
            ),
            'instrucciones': INSTRUCCIONES_AGENTE_PREDETERMINADO,
            'activo': True,
            'allowed_tools': list(ACCIONES),
        },
    )
