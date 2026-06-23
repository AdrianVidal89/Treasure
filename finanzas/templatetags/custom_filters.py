from django import template

register = template.Library()


@register.filter
def get_item(dictionary, key):
    if dictionary is None:
        return None
    return dictionary.get(key)


# ---------------------------------------------------------------------------
# Sistema de color coherente para personas y entidades compartidas
# ---------------------------------------------------------------------------
#
# Idea: cada persona del hogar tiene SIEMPRE el mismo color (asignado de forma
# determinista a partir de su id), de modo que se reconoce de un vistazo en
# toda la app. Lo compartido (hogar / fondos comunes) usa un color neutro
# propio distinto al de cualquier persona.

# Paleta cuidada, distinguible y agradable sobre fondo oscuro.
PALETA_USUARIOS = [
    "#5b8cff",  # azul
    "#ff8f3f",  # naranja
    "#1fd1a5",  # verde agua
    "#ff5d8f",  # rosa
    "#c084fc",  # violeta
    "#ffce5c",  # ámbar
    "#4fd1ff",  # cian
    "#9ae85b",  # lima
    "#ff7a7a",  # coral
    "#7a8cff",  # índigo
]

# Color reservado para lo compartido del hogar (no aparece en la paleta).
COLOR_HOGAR = "#a259ff"


def _user_id(value):
    """Acepta un objeto User, un id numérico o algo con .user.id."""
    if value is None:
        return 0
    # Miembro u objeto con .user
    if hasattr(value, "user"):
        value = value.user
    # User con pk
    if hasattr(value, "pk") and value.pk is not None:
        return int(value.pk)
    try:
        return int(value)
    except (TypeError, ValueError):
        return abs(hash(str(value)))


@register.filter
def color_usuario(value):
    """Devuelve un color hex estable para una persona (User/Miembro/id)."""
    idx = _user_id(value) % len(PALETA_USUARIOS)
    return PALETA_USUARIOS[idx]


@register.filter
def color_hogar(_value=None):
    """Color reservado para lo compartido del hogar."""
    return COLOR_HOGAR


@register.filter
def texto_sobre(hex_color):
    """Negro o blanco según el contraste, para texto sobre un color sólido."""
    if not hex_color:
        return "#ffffff"
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except (ValueError, IndexError):
        return "#ffffff"
    # Luminancia relativa aproximada
    lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255
    return "#101010" if lum > 0.6 else "#ffffff"


@register.filter
def pct_de(value, total):
    """Porcentaje de value sobre total, acotado a [0, 100]. Para barras."""
    try:
        value = float(value)
        total = float(total)
    except (TypeError, ValueError):
        return 0
    if total <= 0:
        return 0
    p = value / total * 100
    if p < 0:
        return 0
    if p > 100:
        return 100
    return round(p, 2)

