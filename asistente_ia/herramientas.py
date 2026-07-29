"""Herramientas que el asistente puede usar durante una conversación.

Son de SOLO LECTURA: consultar la base de datos del hogar, buscar en internet
y leer una página web. Cualquier escritura sigue pasando por el flujo de
propuesta → confirmación del usuario definido en `acciones.py`.

Las consultas a la base de datos están siempre acotadas al hogar del usuario:
cada entidad declara explícitamente cómo se filtra por hogar y no hay forma de
consultar una entidad que no esté en este registro.
"""
import ipaddress
import json
import re
import socket
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import urlparse, quote_plus

import requests
from django.apps import apps

MAX_FILAS = 500
FILAS_POR_DEFECTO = 150
MAX_CARACTERES_RESULTADO = 12000
TIMEOUT_WEB = 15
MAX_BYTES_PAGINA = 400_000


class ErrorHerramienta(Exception):
    """Error de una herramienta, apto para devolverse al modelo como resultado."""


# ─── Registro de entidades consultables ──────────────────────────────────
# nombre_expuesto -> (app, modelo, ruta_de_filtrado_por_hogar | None)
# None = tabla de referencia global (no contiene datos de ningún hogar).

ENTIDADES = {
    # Núcleo
    'hogar': ('core', 'Hogar', 'id'),
    'miembros': ('core', 'UserProfile', 'hogar'),

    # Cuentas, tarjetas y préstamos
    'cuentas_bancarias': ('finanzas', 'CuentaBancaria', 'usuario__userprofile__hogar'),
    'saldos_cuentas': ('finanzas', 'SaldoMensualCuenta', 'cuenta__usuario__userprofile__hogar'),
    'cuentas_credito': ('finanzas', 'CuentaCredito', 'usuario__userprofile__hogar'),
    'deudas_credito': ('finanzas', 'DeudaMensualCredito', 'cuenta__usuario__userprofile__hogar'),
    'tarjetas': ('finanzas', 'TarjetaCredito', 'usuario__userprofile__hogar'),
    'saldos_tarjetas': ('finanzas', 'SaldoMensualTarjeta', 'tarjeta__usuario__userprofile__hogar'),
    'prestamos': ('finanzas', 'PrestamoSimple', 'usuario__userprofile__hogar'),

    # Inversiones
    'inversiones': ('finanzas', 'Inversion', 'usuario__userprofile__hogar'),
    'movimientos_inversion': ('finanzas', 'MovimientoInversion', 'inversion__usuario__userprofile__hogar'),
    'valores_actuales_inversion': ('finanzas', 'ValorActualInversion', 'inversion__usuario__userprofile__hogar'),
    'historial_valores_inversion': ('finanzas', 'HistorialValorInversion', 'inversion__usuario__userprofile__hogar'),
    'resumenes_inversiones': ('finanzas', 'ResumenInversionesMensual', 'usuario__userprofile__hogar'),

    # Ingresos
    'fuentes_ingreso': ('finanzas', 'FuenteIngreso', 'hogar'),
    'ajustes_ingreso': ('finanzas', 'AjusteIngresoMensual', 'fuente__hogar'),
    'ingresos_extraordinarios': ('finanzas', 'IngresoExtraordinario', 'hogar'),
    'ingresos_reales': ('finanzas', 'IngresoRealMes', 'hogar'),
    'destinos_ingreso': ('finanzas', 'DestinoIngreso', 'hogar'),

    # Gastos
    'categorias_gasto': ('finanzas', 'CategoriaGasto', 'hogar'),
    'partidas_gasto': ('finanzas', 'PartidaGasto', 'hogar'),

    # Distribución
    'fondos': ('finanzas', 'FondoFamiliar', 'hogar'),
    'reglas_reparto': ('finanzas', 'ReglaReparto', 'hogar'),
    'subsobres': ('finanzas', 'SubsobreFondo', 'fondo__hogar'),
    'saldos_fondos': ('finanzas', 'SaldoRealFondo', 'fondo__hogar'),

    # Patrimonio inmobiliario
    'propiedades': ('finanzas', 'Propiedad', 'hogar'),
    'historial_propiedades': ('finanzas', 'HistorialPropiedad', 'propiedad__hogar'),

    # Registros mensuales
    'registros_mensuales': ('finanzas', 'RegistroMensual', 'usuario__userprofile__hogar'),

    # Tablas de referencia (sin datos personales)
    'tablas_irpf': ('finanzas', 'TablaIRPF', None),
    'cotizaciones_ss': ('finanzas', 'CotizacionSS', None),
    'catalogo_tickers': ('finanzas', 'TickerCatalogo', None),
}

# Campos que nunca se exponen al modelo, aunque pertenezcan al hogar.
CAMPOS_OCULTOS = {'password', 'api_key', 'avatar'}


def _serializar_valor(valor):
    if isinstance(valor, Decimal):
        return float(valor)
    if isinstance(valor, (datetime, date)):
        return valor.isoformat()
    if isinstance(valor, (bytes, memoryview)):
        return '<binario>'
    return valor


def _serializar_instancia(obj):
    fila = {'id': obj.pk}
    for campo in obj._meta.fields:
        if campo.name == 'id' or campo.name in CAMPOS_OCULTOS:
            continue
        if campo.is_relation:
            relacionado = getattr(obj, campo.name, None)
            fila[campo.name] = str(relacionado) if relacionado else None
            fila[f'{campo.name}_id'] = getattr(obj, f'{campo.name}_id', None)
        else:
            fila[campo.name] = _serializar_valor(getattr(obj, campo.name, None))
    return fila


def consultar_datos(hogar, entidad, limite=FILAS_POR_DEFECTO, filtros=None):
    """Devuelve filas de una entidad del hogar. Solo lectura."""
    if entidad not in ENTIDADES:
        raise ErrorHerramienta(
            f"Entidad '{entidad}' no disponible. Entidades válidas: {', '.join(sorted(ENTIDADES))}"
        )
    nombre_app, nombre_modelo, ruta_hogar = ENTIDADES[entidad]
    modelo = apps.get_model(nombre_app, nombre_modelo)

    consulta = modelo.objects.all()
    if ruta_hogar:
        # Para el propio Hogar el filtro es sobre su clave primaria; para el
        # resto, sobre la relación que lo enlaza con el hogar.
        valor = hogar.pk if ruta_hogar in ('id', 'pk') else hogar
        consulta = consulta.filter(**{ruta_hogar: valor})

    # Filtros extra opcionales, restringidos a campos reales del modelo para
    # que el modelo no pueda atravesar relaciones fuera del hogar.
    if filtros:
        nombres_validos = {c.name for c in modelo._meta.fields}
        seguros = {
            clave: valor for clave, valor in filtros.items()
            if clave.split('__')[0] in nombres_validos
        }
        if seguros:
            try:
                consulta = consulta.filter(**seguros)
            except Exception as e:
                raise ErrorHerramienta(f'Filtro no válido: {e}')

    try:
        limite = max(1, min(int(limite), MAX_FILAS))
    except (TypeError, ValueError):
        limite = FILAS_POR_DEFECTO

    total = consulta.count()
    filas = [_serializar_instancia(obj) for obj in consulta[:limite]]
    return {'entidad': entidad, 'total': total, 'devueltas': len(filas), 'filas': filas}


def listar_entidades():
    return sorted(ENTIDADES)


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
        # Sin resolución local (p. ej. detrás de un proxy) no se puede
        # comprobar la IP; el resto de límites siguen aplicando.
        return url
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
        # Si cambia el formato del buscador, devolvemos el texto plano para que
        # el modelo pueda aprovecharlo igualmente.
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


# ─── Despacho ────────────────────────────────────────────────────────────

HERRAMIENTAS = {
    'consultar_datos': lambda hogar, args: consultar_datos(
        hogar,
        args.get('entidad'),
        args.get('limite', FILAS_POR_DEFECTO),
        args.get('filtros'),
    ),
    'buscar_web': lambda hogar, args: buscar_web(args.get('consulta'), args.get('max_resultados', 6)),
    'leer_url': lambda hogar, args: leer_url(args.get('url')),
}


def ejecutar_herramienta(nombre, argumentos, hogar):
    """Ejecuta una herramienta y devuelve su resultado serializado en texto."""
    funcion = HERRAMIENTAS.get(nombre)
    if not funcion:
        return json.dumps({'error': f"Herramienta '{nombre}' no existe. Disponibles: {', '.join(HERRAMIENTAS)}"},
                          ensure_ascii=False)
    try:
        resultado = funcion(hogar, argumentos or {})
    except ErrorHerramienta as e:
        return json.dumps({'error': str(e)}, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001 — nunca debe tumbar el chat
        return json.dumps({'error': f'Fallo al ejecutar la herramienta: {e}'}, ensure_ascii=False)

    texto = json.dumps(resultado, ensure_ascii=False, default=str)
    if len(texto) > MAX_CARACTERES_RESULTADO:
        texto = texto[:MAX_CARACTERES_RESULTADO] + '… (resultado truncado)'
    return texto


def descripcion_para_prompt():
    """Instrucciones que se añaden al system prompt explicando las herramientas."""
    return (
        "\n\nHERRAMIENTAS DISPONIBLES\n"
        "Puedes consultar datos reales antes de responder. Para usar una herramienta, "
        "responde ÚNICAMENTE con un bloque como este y nada más:\n"
        "```herramienta_ia\n"
        '{"herramienta": "consultar_datos", "argumentos": {"entidad": "partidas_gasto", "limite": 100}}\n'
        "```\n"
        "Recibirás el resultado y podrás pedir otra herramienta o dar la respuesta final.\n\n"
        "1) consultar_datos — lee la base de datos del hogar (solo lectura).\n"
        f"   argumentos: entidad (obligatorio), limite (opcional), filtros (opcional, dict de campos).\n"
        f"   entidades: {', '.join(listar_entidades())}\n"
        "2) buscar_web — busca en internet. argumentos: consulta, max_resultados (opcional).\n"
        "3) leer_url — lee el contenido de una página. argumentos: url.\n\n"
        "Usa consultar_datos siempre que necesites cifras exactas: el resumen inicial "
        "es solo orientativo. Usa buscar_web/leer_url para datos de mercado, cotizaciones, "
        "tipos de interés o normativa fiscal actualizada. Cuando tengas lo necesario, "
        "responde en Markdown, en español y con cifras concretas."
    )
