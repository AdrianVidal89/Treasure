"""Herramientas de LECTURA que el asistente puede usar durante una conversación.

Todas se construyen sobre el registro de esquema (`registro.py`): no hay
ninguna tool ad-hoc que consulte un modelo por su cuenta. Se ejecutan de
forma INMEDIATA dentro del turno — nunca mutan nada, así que no requieren
confirmación del usuario. Cualquier escritura pasa por `acciones.py`.
"""
import ipaddress
import json
import re
import socket
from urllib.parse import urlparse, quote_plus

import requests

from . import registro

MAX_FILAS = 500
FILAS_POR_DEFECTO = 150
MAX_FILAS_BUSQUEDA = 60
MAX_CARACTERES_RESULTADO = 12000
TIMEOUT_WEB = 15
MAX_BYTES_PAGINA = 400_000


class ErrorHerramienta(Exception):
    """Error de una herramienta, apto para devolverse al modelo como resultado."""


def _clamp_limite(limite, por_defecto=FILAS_POR_DEFECTO, tope=MAX_FILAS):
    try:
        return max(1, min(int(limite), tope))
    except (TypeError, ValueError):
        return por_defecto


# ─── Lectura genérica sobre el registro ────────────────────────────────────

def search(hogar, texto, entidades=None, limite=MAX_FILAS_BUSQUEDA):
    """Búsqueda de texto libre sobre los campos de texto de las entidades
    indicadas (o de todas, si no se indica ninguna). Útil cuando no se sabe
    en qué entidad concreta está lo que se busca."""
    texto = (texto or '').strip()
    if not texto:
        raise ErrorHerramienta('Indica qué texto quieres buscar.')

    if entidades:
        entidades_a_buscar = [e for e in entidades if e in registro.ENTIDADES]
        if not entidades_a_buscar:
            raise ErrorHerramienta(
                f"Ninguna de las entidades indicadas es válida. Disponibles: "
                f"{', '.join(registro.listar_entidades())}"
            )
    else:
        entidades_a_buscar = registro.listar_entidades()

    limite = _clamp_limite(limite, por_defecto=MAX_FILAS_BUSQUEDA, tope=MAX_FILAS)
    resultados = []
    for nombre_entidad in entidades_a_buscar:
        if len(resultados) >= limite:
            break
        modelo, ruta_hogar = registro.obtener_modelo(nombre_entidad)
        if not registro.campos_texto_de(modelo):
            continue
        consulta = registro.filtrar_por_hogar(modelo.objects.all(), hogar, ruta_hogar)
        q = registro.construir_q_texto(modelo, texto)
        restantes = limite - len(resultados)
        for obj in consulta.filter(q)[:restantes]:
            fila = registro.serializar_instancia(obj)
            fila['_entidad'] = nombre_entidad
            resultados.append(fila)

    return {'texto': texto, 'devueltas': len(resultados), 'filas': resultados}


def get_item(hogar, entidad, id):
    """Un registro completo por id, incluyendo un adelanto de sus relaciones
    inversas (movimientos, historial…) para no tener que encadenar `query`."""
    if id in (None, ''):
        raise ErrorHerramienta('Indica el id del registro.')
    modelo, ruta_hogar = registro.obtener_modelo(entidad)
    consulta = registro.filtrar_por_hogar(modelo.objects.all(), hogar, ruta_hogar)
    obj = consulta.filter(pk=id).first()
    if not obj:
        raise ErrorHerramienta(f"No se encontró '{entidad}' con id {id} en este hogar.")

    fila = registro.serializar_instancia(obj)
    relaciones = {}
    for rel in obj._meta.related_objects:
        if not (rel.one_to_many or rel.one_to_one):
            continue
        try:
            accesor = rel.get_accessor_name()
            gestor = getattr(obj, accesor, None)
            if gestor is None or not hasattr(gestor, 'all'):
                continue
            filas_rel = [registro.serializar_instancia(o) for o in gestor.all()[:20]]
            if filas_rel:
                relaciones[accesor] = {'total': gestor.count(), 'filas': filas_rel}
        except Exception:
            continue
    if relaciones:
        fila['_relaciones'] = relaciones
    return fila


def query(hogar, entidad, filtros=None, orden=None, limite=FILAS_POR_DEFECTO):
    """Consulta estilo ORM: filtros y orden validados contra el esquema
    permitido (todos los segmentos del lookup, no solo el primero)."""
    modelo, ruta_hogar = registro.obtener_modelo(entidad)
    consulta = registro.filtrar_por_hogar(modelo.objects.all(), hogar, ruta_hogar)

    if filtros:
        seguros = {}
        for clave, valor in filtros.items():
            if not registro.validar_lookup(modelo, clave):
                raise ErrorHerramienta(f"Filtro no permitido: '{clave}'")
            seguros[clave] = valor
        try:
            consulta = consulta.filter(**seguros)
        except Exception as e:
            raise ErrorHerramienta(f'Filtro no válido: {e}')

    if orden:
        campo_orden = orden.lstrip('-')
        if not registro.validar_lookup(modelo, campo_orden):
            raise ErrorHerramienta(f"Orden no permitido: '{orden}'")
        try:
            consulta = consulta.order_by(orden)
        except Exception as e:
            raise ErrorHerramienta(f'Orden no válido: {e}')

    limite = _clamp_limite(limite)
    total = consulta.count()
    filas = [registro.serializar_instancia(obj) for obj in consulta[:limite]]
    return {'entidad': entidad, 'total': total, 'devueltas': len(filas), 'filas': filas}


def count(hogar, entidad, filtros=None):
    modelo, ruta_hogar = registro.obtener_modelo(entidad)
    consulta = registro.filtrar_por_hogar(modelo.objects.all(), hogar, ruta_hogar)
    if filtros:
        seguros = {}
        for clave, valor in filtros.items():
            if not registro.validar_lookup(modelo, clave):
                raise ErrorHerramienta(f"Filtro no permitido: '{clave}'")
            seguros[clave] = valor
        try:
            consulta = consulta.filter(**seguros)
        except Exception as e:
            raise ErrorHerramienta(f'Filtro no válido: {e}')
    return {'entidad': entidad, 'total': consulta.count()}


# ─── Internet ────────────────────────────────────────────────────────────

def _url_segura(url):
    """Valida esquema y bloquea destinos internos (protección SSRF).

    El modelo controla la URL, así que hay que impedir que pueda alcanzar la
    red privada del NAS, localhost o metadatos del proveedor de nube.
    """
    partes = urlparse(url)
    if partes.scheme not in ('http', 'https'):
        raise ErrorHerramienta('Solo se permiten URLs http o https.')
    if not partes.hostname:
        raise ErrorHerramienta('URL sin host válido.')
    try:
        infos = socket.getaddrinfo(partes.hostname, None)
    except socket.gaierror:
        raise ErrorHerramienta('No se pudo resolver el host de esa URL.')
    for info in infos:
        direccion = ipaddress.ip_address(info[4][0])
        if (direccion.is_private or direccion.is_loopback or direccion.is_link_local
                or direccion.is_reserved or direccion.is_multicast):
            raise ErrorHerramienta('Esa URL apunta a una dirección interna y está bloqueada.')
    return url


def _texto_de_html(html):
    html = re.sub(r'(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>', ' ', html)
    html = re.sub(r'(?s)<[^>]+>', ' ', html)
    texto = re.sub(r'&nbsp;?', ' ', html)
    texto = re.sub(r'&amp;?', '&', texto)
    texto = re.sub(r'&lt;?', '<', texto)
    texto = re.sub(r'&gt;?', '>', texto)
    texto = re.sub(r'&quot;?', '"', texto)
    texto = re.sub(r'&#\d+;', ' ', texto)
    return re.sub(r'\s+', ' ', texto).strip()


def _descargar(url, params=None):
    respuesta = requests.get(
        url, params=params, timeout=TIMEOUT_WEB, stream=True,
        headers={'User-Agent': 'Mozilla/5.0 (compatible; TreasureAsistenteIA/1.0)'},
    )
    if respuesta.status_code != 200:
        raise ErrorHerramienta(f'La página respondió con código {respuesta.status_code}.')
    contenido = respuesta.raw.read(MAX_BYTES_PAGINA, decode_content=True) or b''
    return contenido.decode(respuesta.encoding or 'utf-8', errors='replace')


def buscar_web(consulta, max_resultados=6):
    """Busca en internet (DuckDuckGo) y devuelve título, enlace y extracto."""
    consulta = (consulta or '').strip()
    if not consulta:
        raise ErrorHerramienta('Indica qué quieres buscar.')
    # Mismo host fijo que _url_segura validaría igualmente; se descarga
    # directamente porque la consulta va en la query string, no la controla
    # el modelo como URL arbitraria.
    try:
        html = _descargar(f'https://html.duckduckgo.com/html/?q={quote_plus(consulta)}')
    except requests.RequestException:
        raise ErrorHerramienta('No se pudo conectar con el buscador.')

    resultados = []
    patron = re.compile(
        r'class="result__a"[^>]*href="(?P<url>[^"]+)"[^>]*>(?P<titulo>.*?)</a>'
        r'(?:.*?class="result__snippet"[^>]*>(?P<extracto>.*?)</a>)?',
        re.DOTALL,
    )
    for coincidencia in patron.finditer(html):
        resultados.append({
            'titulo': _texto_de_html(coincidencia.group('titulo'))[:200],
            'url': coincidencia.group('url'),
            'extracto': _texto_de_html(coincidencia.group('extracto') or '')[:400],
        })
        if len(resultados) >= max_resultados:
            break

    if not resultados:
        return {'consulta': consulta, 'resultados': [], 'texto': _texto_de_html(html)[:4000]}
    return {'consulta': consulta, 'resultados': resultados}


def leer_url(url):
    """Descarga una página y devuelve su texto legible."""
    url = (url or '').strip()
    if not url:
        raise ErrorHerramienta('Indica la URL que quieres leer.')
    _url_segura(url)
    try:
        html = _descargar(url)
    except requests.RequestException:
        raise ErrorHerramienta('No se pudo descargar esa página.')
    return {'url': url, 'texto': _texto_de_html(html)[:MAX_CARACTERES_RESULTADO]}


# ─── Declaración de tools (JSON schema) + despacho ────────────────────────

SCHEMA_LECTURA = [
    {
        'name': 'search',
        'description': (
            'Busca texto libre en todas las entidades del hogar, o en una lista '
            'concreta de entidades. Úsala cuando no sepas en qué entidad está '
            'lo que buscas.'
        ),
        'parametros': {
            'type': 'object',
            'properties': {
                'texto': {'type': 'string', 'description': 'Texto a buscar'},
                'entidades': {
                    'type': 'array', 'items': {'type': 'string'},
                    'description': 'Opcional: limita la búsqueda a estas entidades',
                },
            },
            'required': ['texto'],
        },
    },
    {
        'name': 'get_item',
        'description': (
            'Obtiene un registro completo por id, incluyendo un adelanto de sus '
            'relaciones (movimientos, historial…). Úsala tras encontrar un id con '
            'search o query.'
        ),
        'parametros': {
            'type': 'object',
            'properties': {
                'entidad': {'type': 'string', 'enum': registro.listar_entidades()},
                'id': {'type': 'integer'},
            },
            'required': ['entidad', 'id'],
        },
    },
    {
        'name': 'query',
        'description': (
            'Consulta tipo ORM sobre una entidad: filtros (dict de campo→valor, '
            'admite lookups como "importe__gte"), orden ("-fecha") y límite.'
        ),
        'parametros': {
            'type': 'object',
            'properties': {
                'entidad': {'type': 'string', 'enum': registro.listar_entidades()},
                'filtros': {'type': 'object', 'description': 'p.ej. {"categoria__nombre": "Ocio"}'},
                'orden': {'type': 'string', 'description': 'p.ej. "-fecha"'},
                'limite': {'type': 'integer'},
            },
            'required': ['entidad'],
        },
    },
    {
        'name': 'count',
        'description': 'Cuenta registros de una entidad, con filtros opcionales.',
        'parametros': {
            'type': 'object',
            'properties': {
                'entidad': {'type': 'string', 'enum': registro.listar_entidades()},
                'filtros': {'type': 'object'},
            },
            'required': ['entidad'],
        },
    },
    {
        'name': 'buscar_web',
        'description': (
            'Busca en internet. Útil para cotizaciones, tipos de interés o '
            'normativa fiscal actualizada — datos que no están en la base de datos.'
        ),
        'parametros': {
            'type': 'object',
            'properties': {
                'consulta': {'type': 'string'},
                'max_resultados': {'type': 'integer'},
            },
            'required': ['consulta'],
        },
    },
    {
        'name': 'leer_url',
        'description': 'Descarga y lee el texto de una URL concreta.',
        'parametros': {
            'type': 'object',
            'properties': {'url': {'type': 'string'}},
            'required': ['url'],
        },
    },
]

HERRAMIENTAS = {
    'search': lambda hogar, args: search(hogar, args.get('texto'), args.get('entidades'), args.get('limite', MAX_FILAS_BUSQUEDA)),
    'get_item': lambda hogar, args: get_item(hogar, args.get('entidad'), args.get('id')),
    'query': lambda hogar, args: query(hogar, args.get('entidad'), args.get('filtros'), args.get('orden'), args.get('limite', FILAS_POR_DEFECTO)),
    'count': lambda hogar, args: count(hogar, args.get('entidad'), args.get('filtros')),
    'buscar_web': lambda hogar, args: buscar_web(args.get('consulta'), args.get('max_resultados', 6)),
    'leer_url': lambda hogar, args: leer_url(args.get('url')),
}


def ejecutar_herramienta(nombre, argumentos, hogar):
    """Ejecuta una tool de lectura y devuelve su resultado serializado en texto.
    Nunca lanza: cualquier fallo se convierte en un `{"error": ...}` para que
    el modelo pueda reaccionar (reintentar con otros argumentos, etc.)."""
    funcion = HERRAMIENTAS.get(nombre)
    if not funcion:
        return json.dumps(
            {'error': f"Herramienta '{nombre}' no existe. Disponibles: {', '.join(HERRAMIENTAS)}"},
            ensure_ascii=False,
        )
    try:
        resultado = funcion(hogar, argumentos or {})
    except (ErrorHerramienta, registro.ErrorRegistro) as e:
        return json.dumps({'error': str(e)}, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001 — nunca debe tumbar el chat
        return json.dumps({'error': f'Fallo al ejecutar la herramienta: {e}'}, ensure_ascii=False)

    texto = json.dumps(resultado, ensure_ascii=False, default=str)
    if len(texto) > MAX_CARACTERES_RESULTADO:
        texto = texto[:MAX_CARACTERES_RESULTADO] + '… (resultado truncado, acota con filtros/limite)'
    return texto


def resumen_paso(nombre, argumentos):
    """Texto corto que se muestra en el chat mientras el asistente investiga."""
    argumentos = argumentos or {}
    if nombre == 'search':
        return f"Buscando: {argumentos.get('texto', '?')}"
    if nombre == 'get_item':
        return f"Consultando {argumentos.get('entidad', '?')} #{argumentos.get('id', '?')}"
    if nombre == 'query':
        return f"Consultando la base de datos: {argumentos.get('entidad', '?')}"
    if nombre == 'count':
        return f"Contando: {argumentos.get('entidad', '?')}"
    if nombre == 'buscar_web':
        return f"Buscando en internet: {argumentos.get('consulta', '?')}"
    if nombre == 'leer_url':
        return f"Leyendo: {argumentos.get('url', '?')}"
    return f"Ejecutando {nombre}"
