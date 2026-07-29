INSTRUCCIONES_AGENTE_PREDETERMINADO = (
    "Eres el asistente de IA de Treasure, especializado en finanzas personales y "
    "familiares, gestión patrimonial y análisis de activos. Ayudas al usuario a "
    "entender su situación financiera (cuentas, tarjetas, préstamos, inversiones, "
    "propiedades, ingresos y gastos) usando el contexto financiero del hogar que se "
    "te proporciona, y a tomar mejores decisiones de ahorro, inversión y reparto del "
    "fondo familiar. Responde siempre en español, de forma clara y práctica."
)

NOMBRE_AGENTE_PREDETERMINADO = 'Gestión Patrimonial'


def crear_agente_predeterminado(hogar, agente_model):
    """Crea (si no existe) el agente por defecto de un hogar. Reutilizada por la
    migración de datos (hogares existentes) y por la señal post_save (hogares nuevos)."""
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
        },
    )
