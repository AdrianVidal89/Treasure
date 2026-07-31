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
    """Devuelve el nombre de categoría sugerido por las reglas ESTÁTICAS
    (heurística de comercios habituales) o None si no hay coincidencia."""
    if not concepto:
        return None
    texto = f" {concepto.lower()} "
    for categoria, claves in REGLAS.items():
        if any(clave in texto for clave in claves):
            return categoria
    return None


def categorizar(concepto, hogar):
    """Sugiere una CategoriaGasto para un concepto, priorizando las reglas
    APRENDIDAS del hogar (creadas al categorizar movimientos manualmente o con
    la IA) sobre la heurística estática. Devuelve la instancia CategoriaGasto o
    None. Es lo que debe usar la importación para que los criterios del usuario
    se apliquen a futuras importaciones.
    """
    if not concepto:
        return None
    from finanzas.models import CategoriaGasto
    from .models import ReglaCategorizacion

    texto = concepto.lower()
    # 1) Reglas aprendidas del hogar (las más específicas/largas primero, para
    #    que un patrón concreto gane a uno genérico).
    reglas = ReglaCategorizacion.objects.filter(
        hogar=hogar, activo=True,
    ).select_related('categoria')
    mejor = None
    mejor_len = -1
    for r in reglas:
        patron = (r.patron or '').lower().strip()
        if patron and patron in texto and len(patron) > mejor_len:
            mejor = r
            mejor_len = len(patron)
    if mejor:
        ReglaCategorizacion.objects.filter(pk=mejor.pk).update(
            veces_aplicada=mejor.veces_aplicada + 1,
        )
        return mejor.categoria

    # 2) Heurística estática → resolver el nombre a una categoría del hogar.
    nombre_cat = categorizar_por_codigo(concepto)
    if nombre_cat:
        return CategoriaGasto.objects.filter(hogar=hogar, nombre=nombre_cat, activo=True).first()
    return None
