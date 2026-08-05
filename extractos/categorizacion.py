"""
Categorización "por código": heurística de palabras clave sobre el concepto.

Asocia comercios/conceptos habituales a las categorías de gasto predefinidas
(ver finanzas.views_gastos.CATEGORIAS_PREDEFINIDAS). Lo que no encaje aquí
queda 'sin_categorizar' a la espera de que el usuario lo nombre una vez en la
pantalla «Sin categorizar» (que crea una ReglaCategorizacion) o de la
categorización con IA.

Dos decisiones de diseño importantes:

* **Todo se compara normalizado** (minúsculas, sin acentos, signos convertidos
  en espacios) y con límite de palabra, así que «Cafetería del Hospital» casa
  con 'cafeteria' y 'ibi' no casa dentro de «recibí».
* **Gana la clave más larga, no la primera.** Es lo que permite que
  'repsol comercializadora' (la factura del gas) no acabe en Gasolina solo
  porque 'repsol' aparezca antes en el diccionario.
"""

from .normalizacion import contiene_patron, normalizar_texto

# nombre de categoría (tal cual en CATEGORIAS_PREDEFINIDAS) → palabras clave
REGLAS = {
    'Alimentacion': (
        'mercadona', 'carrefour', 'lidl', 'aldi', 'dia', 'consum', 'alcampo',
        'eroski', 'supermercado', 'super', 'hipercor', 'ahorramas', 'gadis',
        'froiz', 'condis', 'caprabo', 'masymas', 'supersol', 'coviran', 'spar',
        'el jamon', 'bm supermercados', 'carniceria', 'fruteria', 'panaderia',
        'pescaderia', 'charcuteria', 'mercado de abastos',
    ),
    'Gasolina': (
        'repsol', 'cepsa', 'galp', 'bp', 'shell', 'gasolin', 'carburante',
        'petronor', 'ballenoil', 'plenoil', 'petroprix', 'gasexpress', 'avia',
        'moeve', 'estacion de servicio',
    ),
    'Luz': (
        'iberdrola', 'endesa', 'naturgy', 'holaluz', 'electricidad', 'luz',
        'octopus energy', 'lucera', 'som energia', 'edp energia', 'curenergia',
        'factura de luz',
    ),
    'Agua': (
        'canal isabel', 'canal de isabel', 'aguas de', 'emasesa', 'aqualia',
        'factura agua', 'factura de agua', 'recibo de agua', 'aljarafesa',
        'hidralia', 'giahsa', 'emacsa', 'emaya', 'emivasa', 'consorcio de aguas',
    ),
    'Gas': (
        'gas natural', 'redexis', 'nortegas', 'butano', 'repsol butano',
        'repsol comercializadora', 'naturgy gas', 'madrilena red de gas',
        'nedgia', 'factura de gas',
    ),
    'Internet / Telefono': (
        'movistar', 'vodafone', 'orange', 'yoigo', 'jazztel', 'masmovil',
        'pepephone', 'pepe mobile', 'digi', 'telefono', 'internet', 'fibra',
        'lowi', 'simyo', 'finetwork', 'avatel', 'telefonica', 'euskaltel',
        'r cable', 'parlem',
    ),
    'Suscripciones': (
        'netflix', 'spotify', 'hbo', 'disney', 'amazon prime', 'prime video',
        'youtube premium', 'apple', 'apple com', 'itunes', 'icloud', 'dropbox',
        'suscripcion', 'audible', 'kindle', 'filmin', 'crunchyroll', 'duolingo',
        'linkedin premium',
    ),
    'Seguros': (
        'seguro', 'seguros', 'mapfre', 'axa', 'allianz', 'mutua', 'linea directa',
        'zurich', 'generali', 'sanitas', 'adeslas', 'dkv', 'asisa', 'caser',
        'reale', 'pelayo', 'ocaso', 'santalucia', 'catalana occidente', 'verti',
        'balumba', 'qualitas auto', 'fiatc', 'mybox', 'poliza',
    ),
    'Hipoteca / Alquiler': (
        'hipoteca', 'prestamo hipotec', 'alquiler', 'arrendamiento', 'prestamo',
        'pres', 'cuota prestamo', 'amortizacion prestamo',
    ),
    'Comunidad': (
        'comunidad de propietarios', 'cuota comunidad', 'administrador de fincas',
        'administracion de fincas', 'recibo de fincas', 'junta de propietarios',
        'derrama',
    ),
    'Gimnasio': (
        'gimnasio', 'basic fit', 'mcfit', 'anytime fitness', 'gym', 'altafit',
        'vivagym', 'synergym', 'smart fit', 'crossfit', 'padel', 'piscina municipal',
    ),
    'Restaurantes': (
        'restaurante', 'restaurant', 'cafeteria', 'bar', 'mcdonald', 'burger',
        'telepizza', 'glovo', 'just eat', 'uber eats', 'chiringuito', 'grill',
        'taberna', 'meson', 'pizzeria', 'heladeria', 'cerveceria', 'tapas',
        'kebab', 'sushi', 'starbucks', 'goiko', 'foster hollywood', 'rodilla',
        'dominos', 'papa johns', 'kfc', 'taco bell', 'subway', 'brasa', 'asador',
        'gastrobar', 'marisqueria',
    ),
    'Transporte': (
        'renfe', 'metro', 'emt', 'uber', 'cabify', 'taxi', 'peaje', 'parking',
        'blablacar', 'aparcamiento', 'aparcamientos', 'ipark', 'saba',
        'empark', 'interparking', 'bip drive', 'via t', 'viat', 'autopista',
        'avanza', 'alsa', 'damas', 'tussam', 'bicimad', 'bolt', 'flixbus',
        'vueling', 'ryanair', 'iberia', 'easyjet', 'aena', 'billete de tren',
    ),
    'Ropa': (
        'zara', 'primark', 'h m', 'decathlon', 'el corte ingles', 'mango',
        'pull bear', 'bershka', 'stradivarius', 'massimo dutti', 'oysho',
        'springfield', 'cortefiel', 'nike', 'adidas', 'snipes', 'foot locker',
        'kiabi', 'lefties', 'shein', 'zalando', 'sprinter', 'jd sports',
        'soccer factory',
    ),
    'Ocio': (
        'cine', 'teatro', 'entradas', 'steam', 'playstation', 'nintendo',
        'xbox', 'twitch', 'patreon', 'museo', 'bolera', 'karting',
        'escape room', 'movistar plus', 'dazn', 'yelmo', 'cinesa', 'kinepolis',
        'ticketmaster', 'fnac', 'concierto', 'festival',
    ),
    'IBI': ('ibi', 'impuesto bienes inmuebles', 'impuesto sobre bienes inmuebles'),
    'Basura': ('basura', 'residuos', 'tasa residuos'),
    # Categorías anuales específicas: sus claves son más largas que el 'seguro'
    # genérico de Seguros, así que ganan cuando el banco las detalla.
    'Seguro coche': ('seguro coche', 'seguro de coche', 'seguro auto', 'seguro del vehiculo'),
    'Seguro hogar': ('seguro hogar', 'seguro de hogar', 'seguro del hogar', 'seguro vivienda'),
    'Salud / Farmacia': (
        'farmacia', 'fcia', 'parafarmacia', 'hospital', 'hospitalario',
        'hospitalarios', 'clinica', 'dentista', 'dental', 'optica',
        'fisioterapia', 'podologo', 'psicologo', 'laboratorio', 'quiron',
        'vithas', 'analisis clinicos', 'centro medico', 'ortopedia',
    ),
    'Hogar / Bricolaje': (
        'leroy merlin', 'bricomart', 'bricodepot', 'brico depot', 'ikea',
        'ferreteria', 'bauhaus', 'conforama', 'maisons du monde', 'casa viva',
        'ale hop', 'flying tiger', 'tiendanimal', 'jardineria', 'muebles',
        'electrodomesticos',
    ),
    'Tecnologia / Software': (
        'anthropic', 'claude', 'openai', 'chatgpt', 'cloudflare', 'github',
        'gitlab', 'digitalocean', 'amazon web services', 'google cloud',
        'microsoft', 'office 365', 'adobe', 'jetbrains', 'namecheap', 'godaddy',
        'ovh', 'hetzner', 'vercel', 'notion', 'figma', 'pccomponentes',
        'mediamarkt', 'worten', 'dominio web', 'hosting',
    ),
    'Impuestos y comisiones': (
        'comision', 'comisiones', 'embargo', 'embargos', 'aeat',
        'agencia tributaria', 'hacienda publica', 'seguridad social',
        'tesoreria general', 'multa', 'sancion', 'dgt', 'mantenimiento cuenta',
        'intereses deudores', 'descubierto', 'impuesto',
    ),
    'Mantenimiento vehicular': (
        'taller', 'norauto', 'feu vert', 'aurgi', 'midas', 'confortauto',
        'euromaster', 'rodi', 'neumaticos', 'autolavado', 'lavado de coche',
        'grua',
    ),
    'ITV': ('itv', 'inspeccion tecnica de vehiculos'),
}


def _construir_indice():
    """Índice plano [(clave_normalizada, categoria)] ordenado de la clave más
    larga a la más corta, para que la búsqueda devuelva siempre la coincidencia
    más específica."""
    indice = []
    for categoria, claves in REGLAS.items():
        for clave in claves:
            clave_norm = normalizar_texto(clave)
            if clave_norm:
                indice.append((clave_norm, categoria))
    indice.sort(key=lambda par: len(par[0]), reverse=True)
    return indice


_INDICE = _construir_indice()


def categorizar_por_codigo(concepto):
    """Devuelve el nombre de categoría sugerido por las reglas ESTÁTICAS
    (heurística de comercios habituales) o None si no hay coincidencia."""
    texto = normalizar_texto(concepto)
    if not texto:
        return None
    for clave, categoria in _INDICE:
        if contiene_patron(texto, clave):
            return categoria
    return None


def _reglas_del_hogar(hogar):
    from .models import ReglaCategorizacion
    return list(
        ReglaCategorizacion.objects.filter(hogar=hogar, activo=True)
        .select_related('categoria')
    )


def _mejor_regla(texto_norm, reglas):
    """Regla aprendida más específica (patrón más largo) que casa con el texto.

    A diferencia del diccionario estático, aquí se busca por subcadena y no por
    palabra completa: el patrón lo escribe el usuario y su expectativa es
    «el concepto contiene esto», tal como dice el help_text del modelo."""
    mejor = None
    mejor_len = -1
    for regla in reglas:
        patron = normalizar_texto(regla.patron)
        if patron and patron in texto_norm and len(patron) > mejor_len:
            mejor = regla
            mejor_len = len(patron)
    return mejor


def categorizar(concepto, hogar):
    """Sugiere una CategoriaGasto para un concepto, priorizando las reglas
    APRENDIDAS del hogar (creadas al categorizar movimientos manualmente o con
    la IA) sobre la heurística estática.

    Devuelve una tupla (CategoriaGasto|None, origen) con
    origen ∈ {'regla', 'codigo', None}, para que quien la llame pueda distinguir
    un acierto del diccionario de uno de un criterio propio del usuario.
    """
    resultados = categorizar_lote([concepto], hogar)
    return resultados[0]


def categorizar_lote(conceptos, hogar):
    """Versión por lotes de `categorizar`: carga las reglas del hogar UNA vez
    para toda la importación en lugar de reconsultarlas por movimiento.

    Devuelve una lista de tuplas (CategoriaGasto|None, origen) alineada con
    `conceptos`."""
    from django.db.models import F

    from finanzas.models import CategoriaGasto
    from .models import ReglaCategorizacion

    reglas = _reglas_del_hogar(hogar)
    cache_categorias = {}
    usos = {}
    resultados = []

    for concepto in conceptos:
        texto = normalizar_texto(concepto)
        if not texto:
            resultados.append((None, None))
            continue

        regla = _mejor_regla(texto, reglas)
        if regla:
            usos[regla.pk] = usos.get(regla.pk, 0) + 1
            resultados.append((regla.categoria, 'regla'))
            continue

        nombre_cat = categorizar_por_codigo(texto)
        if not nombre_cat:
            resultados.append((None, None))
            continue

        if nombre_cat not in cache_categorias:
            cache_categorias[nombre_cat] = CategoriaGasto.objects.filter(
                hogar=hogar, nombre=nombre_cat, activo=True,
            ).first()
        categoria = cache_categorias[nombre_cat]
        resultados.append((categoria, 'codigo' if categoria else None))

    for pk, veces in usos.items():
        ReglaCategorizacion.objects.filter(pk=pk).update(
            veces_aplicada=F('veces_aplicada') + veces,
        )

    return resultados
