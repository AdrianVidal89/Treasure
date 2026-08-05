"""
Normalización de textos de extractos bancarios.

Punto único del que dependen el parser, el diccionario de comercios, las reglas
aprendidas y la agrupación de movimientos sin categorizar. La idea es que
«Cafetería», «CAFETERIA» y «Cafeteria» sean el mismo texto a efectos de
comparación, y que «Repsol, S.L.U.-REPSOL COMERCIALIZADORA…» y
«REPSOL COMERCIALIZADORA DE ELECTRICIDAD Y GAS SL» acaben en la misma clave de
comercio para poder agruparlos y aprender una sola regla.
"""

import re
import unicodedata

# Prefijos de operación que el banco antepone al comercio real y que no aportan
# nada para identificarlo. Se eliminan solo si aparecen al principio.
_PREFIJOS = (
    'compra en', 'compra tarjeta', 'compra', 'pago con tarjeta', 'pago tarjeta',
    'pago en', 'pago de', 'pago a', 'recibo de', 'recibo', 'adeudo por',
    'adeudo', 'transferencia a favor de', 'transferencia a', 'transferencia de',
    'transferencia', 'traspaso a', 'traspaso de', 'traspaso', 'trf', 'tarjeta',
    'www', 'reintegro', 'bizum a', 'bizum de', 'bizum',
)

# Formas societarias: ruido puro para identificar el comercio.
_SOCIETARIAS = (
    'sociedad limitada', 'sociedad anonima', 's l u', 's a u', 's coop',
    's l', 's a', 'slu', 'sau', 'sll', 'scp', 'sl', 'sa', 'cb', 'sc',
)

# Coletillas de fecha de operación que algunos bancos meten en la descripción.
_RE_FECHA_OPERACION = re.compile(
    r'\bfecha\s+(?:de\s+)?(?:operacion|valor|compra)\b.*$'
)
# Tokens con 4 o más dígitos: máscaras de tarjeta, referencias, nº de préstamo.
_RE_TOKEN_NUMERICO = re.compile(r'\b\w*\d{4,}\w*\b')
# Tokens puramente numéricos (restos de fechas, importes, nº de recibo): no
# identifican al comercio y romperían la agrupación mes a mes.
_RE_SOLO_DIGITOS = re.compile(r'\b\d+\b')

_RE_NO_ALFANUM = re.compile(r'[^0-9a-z]+')
_RE_ESPACIOS = re.compile(r'\s+')

MAX_COMERCIO = 120


def sin_acentos(texto):
    """Quita tildes y diacríticos: 'Cafetería' → 'Cafeteria', 'Cafè' → 'Cafe'."""
    if not texto:
        return ''
    descompuesto = unicodedata.normalize('NFKD', str(texto))
    return ''.join(c for c in descompuesto if not unicodedata.combining(c))


def normalizar_texto(texto):
    """Forma canónica para TODA comparación de textos de extractos.

    Minúsculas, sin acentos y con cualquier signo (puntos, guiones, comas,
    símbolos) convertido en espacio, de modo que 'WWW.AMAZON' → 'www amazon' y
    'Repsol, S.L.U.-REPSOL' → 'repsol s l u repsol'. Así las claves del
    diccionario y los patrones de las reglas pueden buscarse con límites de
    palabra sin depender de cómo puntúe cada banco.
    """
    if not texto:
        return ''
    texto = sin_acentos(str(texto)).lower()
    texto = _RE_NO_ALFANUM.sub(' ', texto)
    return _RE_ESPACIOS.sub(' ', texto).strip()


def _quitar_prefijos(texto):
    """Elimina repetidamente los prefijos de operación del principio del texto."""
    cambiado = True
    while cambiado:
        cambiado = False
        for prefijo in _PREFIJOS:
            if texto == prefijo:
                return ''
            if texto.startswith(prefijo + ' '):
                texto = texto[len(prefijo) + 1:]
                cambiado = True
                break
    return texto


def normalizar_comercio(concepto):
    """Clave de comercio: el concepto normalizado y despojado del ruido de la
    operación (prefijos del banco, formas societarias, referencias numéricas).

    Es lo que se usa para agrupar movimientos «similares» y como patrón por
    defecto al aprender una regla, así que interesa que sea estable entre
    importaciones aunque el banco varíe la puntuación o la referencia.

        'WWW.AMAZON'                          → 'amazon'
        'Compra en MERCADONA 1234 SEVILLA'    → 'mercadona sevilla'
        'Pepe Mobile, S.L.U.'                 → 'pepe mobile'
    """
    texto = normalizar_texto(concepto)
    if not texto:
        return ''

    texto = _RE_FECHA_OPERACION.sub(' ', texto)
    texto = _RE_TOKEN_NUMERICO.sub(' ', texto)
    texto = _RE_SOLO_DIGITOS.sub(' ', texto)
    texto = _RE_ESPACIOS.sub(' ', texto).strip()
    texto = _quitar_prefijos(texto)

    # Formas societarias en cualquier posición (ya sin puntos tras normalizar).
    palabras = texto.split()
    limpio = []
    i = 0
    while i < len(palabras):
        encontrada = False
        for societaria in _SOCIETARIAS:
            trozos = societaria.split()
            if palabras[i:i + len(trozos)] == trozos:
                i += len(trozos)
                encontrada = True
                break
        if not encontrada:
            limpio.append(palabras[i])
            i += 1

    return ' '.join(limpio)[:MAX_COMERCIO].strip()


def contiene_patron(texto_normalizado, patron_normalizado):
    """True si `patron` aparece en `texto` como palabra(s) completa(s).

    Evita que 'ibi' case dentro de 'recibi' o 'bar' dentro de 'barcelona', que
    es justo lo que obligaba a escribir las claves con espacios ('dia ', 'bar ')
    en la versión anterior del diccionario.
    """
    if not texto_normalizado or not patron_normalizado:
        return False
    return re.search(
        r'(?<!\w)' + re.escape(patron_normalizado) + r'(?!\w)',
        texto_normalizado,
    ) is not None


# --- Traspasos internos ------------------------------------------------------

# Verbos que indican movimiento de dinero entre cuentas propias, no un gasto.
_VERBOS_TRASPASO = ('transferencia', 'traspaso', 'pago de', 'bizum')

_TRASPASOS_EXPLICITOS = (
    'traspaso entre cuentas', 'transferencia entre cuentas',
    'traspaso interno', 'entre cuentas propias',
)


def nombres_del_hogar(hogar):
    """Nombres normalizados de los miembros del hogar, para reconocer los
    traspasos entre cuentas propias («Transferencia a ADRIAN VIDAL RODRIGUEZ»).

    Devuelve tanto el nombre completo como los apellidos/nombre por separado
    (solo tokens de más de 2 letras, para no casar con partículas)."""
    nombres = set()
    miembros = getattr(hogar, 'miembros', None)
    if miembros is None:
        return nombres
    for perfil in miembros.select_related('user').all():
        user = perfil.user
        if not user:
            continue
        partes = [user.first_name or '', user.last_name or '']
        completo = normalizar_texto(' '.join(p for p in partes if p))
        if completo:
            nombres.add(completo)
        for token in completo.split():
            if len(token) > 2:
                nombres.add(token)
    return nombres


def es_traspaso_interno(concepto, nombres_hogar):
    """True si el movimiento es un traspaso entre cuentas propias.

    Exige las dos cosas: que el concepto hable de transferencia/traspaso Y que
    mencione a algún miembro del hogar. Así «Transferencia a ADRIAN VIDAL
    RODRIGUEZ» se marca como traspaso, pero «Transferencia a Pepe Mobile, S.L.U.»
    (el recibo del móvil) sigue siendo un gasto normal.
    """
    texto = normalizar_texto(concepto)
    if not texto:
        return False
    if any(marca in texto for marca in _TRASPASOS_EXPLICITOS):
        return True
    if not any(verbo in texto for verbo in _VERBOS_TRASPASO):
        return False
    return any(contiene_patron(texto, nombre) for nombre in nombres_hogar if nombre)
