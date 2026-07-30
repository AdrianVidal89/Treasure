"""
Categorización "por código": heurística de palabras clave sobre el concepto.

Asocia comercios/conceptos habituales a las categorías de gasto predefinidas
(ver finanzas.views_gastos.CATEGORIAS_PREDEFINIDAS). Lo que no encaje aquí
quedará 'sin_categorizar' a la espera de la categorización con IA (fase posterior).
"""

# nombre de categoría (tal cual en CATEGORIAS_PREDEFINIDAS) → palabras clave
REGLAS = {
    'Alimentacion': ('mercadona', 'carrefour', 'lidl', 'aldi', 'dia ', 'consum',
                     'alcampo', 'eroski', 'supermercado', 'super ', 'hipercor'),
    'Gasolina': ('repsol', 'cepsa', 'galp', 'bp ', 'shell', 'gasolin', 'carburante', 'petronor'),
    'Luz': ('iberdrola', 'endesa', 'naturgy', 'holaluz', 'electricidad', ' luz '),
    'Agua': ('canal isabel', 'aguas de', 'emasesa', 'aqualia', 'factura agua'),
    'Gas': ('gas natural', 'redexis', 'nortegas', 'butano'),
    'Internet / Telefono': ('movistar', 'vodafone', 'orange', 'yoigo', 'jazztel',
                            'masmovil', 'pepephone', 'digi ', 'telefono', 'internet', 'fibra'),
    'Suscripciones': ('netflix', 'spotify', 'hbo', 'disney', 'amazon prime', 'youtube premium',
                      'apple.com', 'icloud', 'dropbox', 'suscripcion'),
    'Seguros': ('seguro', 'mapfre', 'axa', 'allianz', 'mutua', 'linea directa', 'zurich', 'generali'),
    'Hipoteca / Alquiler': ('hipoteca', 'prestamo hipotec', 'alquiler', 'arrendamiento'),
    'Comunidad': ('comunidad de propietarios', 'cuota comunidad', 'administrador de fincas'),
    'Gimnasio': ('gimnasio', 'basic fit', 'mcfit', 'anytime fitness', 'gym'),
    'Restaurantes': ('restaurante', 'cafeteria', 'bar ', 'mcdonald', 'burger', 'telepizza',
                     'glovo', 'just eat', 'uber eats'),
    'Transporte': ('renfe', 'metro', 'emt', 'uber', 'cabify', 'taxi', 'peaje', 'parking', 'blablacar'),
    'Ropa': ('zara', 'primark', 'h&m', 'decathlon', 'el corte ingles', 'mango', 'pull&bear'),
    'Ocio': ('cine', 'spotify', 'teatro', 'entradas', 'steam', 'playstation', 'nintendo'),
    'IBI': ('ibi', 'impuesto bienes inmuebles'),
    'Basura': ('basura', 'residuos', 'tasa residuos'),
}


def categorizar_por_codigo(concepto):
    """Devuelve el nombre de categoría sugerido o None si no hay coincidencia."""
    if not concepto:
        return None
    texto = f" {concepto.lower()} "
    for categoria, claves in REGLAS.items():
        if any(clave in texto for clave in claves):
            return categoria
    return None
