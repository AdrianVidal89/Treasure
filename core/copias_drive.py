"""Copia de seguridad de la base de datos a Google Drive.

Cómo encaja:

- CONEXIÓN. Google no deja volver a una IP privada tras el login, que es como
  se abre Treasure en casa. Por eso se usa el flujo "de dispositivo": la app
  pide un código, el usuario lo mete en google.com/device desde cualquier
  navegador y la app recoge el permiso consultando cada pocos segundos. Se
  guarda solo el refresh token, cifrado.

- PERMISO MÍNIMO. Se pide `drive.file`: la app solo ve los archivos y carpetas
  que ella misma crea, no el resto del Drive. Por eso la carpeta de destino la
  crea la app con la ruta que elige el usuario; luego se puede mover a
  cualquier sitio del Drive y las copias siguen llegando a ella.

- PROGRAMACIÓN. `debe_copiar()` dice si toca según la periodicidad; el comando
  `copias_drive --cada 300` (servicio `copias` del docker-compose) lo consulta
  en bucle y llama a `hacer_copia()`.
"""
import datetime
import os
import subprocess
import tempfile
import time

import requests
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from asistente_ia.crypto import cifrar, descifrar
from .models import CopiaDriveConfig, CopiaDriveRegistro

SCOPE = 'https://www.googleapis.com/auth/drive.file'
URL_DEVICE = 'https://oauth2.googleapis.com/device/code'
URL_TOKEN = 'https://oauth2.googleapis.com/token'
URL_REVOKE = 'https://oauth2.googleapis.com/revoke'
URL_FILES = 'https://www.googleapis.com/drive/v3/files'
URL_UPLOAD = 'https://www.googleapis.com/upload/drive/v3/files'
URL_ABOUT = 'https://www.googleapis.com/drive/v3/about'
MIME_CARPETA = 'application/vnd.google-apps.folder'
PREFIJO = 'treasure_backup_'

TIMEOUT_HTTP = 30
TIMEOUT_DUMP = 600
# Tras un fallo no se reintenta en cada vuelta del bucle (cada 5 min), sino
# pasado este margen: si Google está caído no hace falta martillearlo.
REINTENTO_TRAS_ERROR = datetime.timedelta(minutes=30)
# Una copia que lleva más de esto "en curso" se da por muerta (proceso
# reiniciado a mitad) y se permite otra.
BLOQUEO_MAXIMO = datetime.timedelta(minutes=30)


class ErrorCopia(Exception):
    """Error con un mensaje que se puede enseñar tal cual al usuario."""


# ─── Credenciales ───────────────────────────────────────────────────────────

def credenciales(cfg):
    """(client_id, client_secret): los de la pantalla si se pusieron, si no
    los del .env."""
    client_id = cfg.client_id or settings.GOOGLE_DRIVE_CLIENT_ID
    secreto = ''
    if cfg.client_secret_cifrado:
        secreto = descifrar(cfg.client_secret_cifrado) or ''
    secreto = secreto or settings.GOOGLE_DRIVE_CLIENT_SECRET
    return client_id, secreto


def guardar_credenciales(cfg, client_id, client_secret):
    """Guarda lo que venga en el formulario. Un secreto vacío deja el anterior
    (el campo no se rellena al editar, para no exponerlo en la página)."""
    client_id = (client_id or '').strip()
    if client_id != cfg.client_id:
        cfg.client_id = client_id
    if client_secret and client_secret.strip():
        cfg.client_secret_cifrado = cifrar(client_secret.strip())


def _error_google(resp):
    try:
        datos = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}"
    err = datos.get('error')
    if isinstance(err, dict):
        return err.get('message') or str(err)
    desc = datos.get('error_description')
    return f"{err}: {desc}" if desc else str(err or datos)


# ─── Conexión (flujo de dispositivo) ────────────────────────────────────────

def iniciar_conexion(cfg):
    """Pide a Google un código de usuario. Devuelve lo que hay que enseñar."""
    client_id, _ = credenciales(cfg)
    if not client_id:
        raise ErrorCopia("Falta el Client ID de Google. Ponlo en el paso 1.")
    resp = requests.post(URL_DEVICE, data={'client_id': client_id, 'scope': SCOPE},
                         timeout=TIMEOUT_HTTP)
    if resp.status_code != 200:
        raise ErrorCopia(f"Google no ha dado código: {_error_google(resp)}. "
                         "Comprueba que el cliente OAuth es de tipo «TV y dispositivos "
                         "de entrada limitada» y que la API de Drive está habilitada.")
    datos = resp.json()
    cfg.device_code = datos['device_code']
    cfg.device_user_code = datos['user_code']
    cfg.device_url = datos.get('verification_url') or datos.get('verification_uri') or 'https://www.google.com/device'
    cfg.device_expira = timezone.now() + datetime.timedelta(seconds=int(datos.get('expires_in', 1800)))
    cfg.device_intervalo = int(datos.get('interval', 5))
    cfg.save()
    return {'codigo': cfg.device_user_code, 'url': cfg.device_url,
            'intervalo': cfg.device_intervalo, 'expira': cfg.device_expira.isoformat()}


def _limpiar_device(cfg):
    cfg.device_code = ''
    cfg.device_user_code = ''
    cfg.device_url = ''
    cfg.device_expira = None


def comprobar_conexion(cfg):
    """Pregunta a Google si el usuario ya ha metido el código.

    Devuelve un estado: 'pendiente', 'conectado', 'caducado', 'denegado' o
    'sin_solicitud'."""
    if not cfg.device_code:
        return {'estado': 'conectado' if cfg.conectado else 'sin_solicitud'}
    if cfg.device_expira and timezone.now() > cfg.device_expira:
        _limpiar_device(cfg)
        cfg.save()
        return {'estado': 'caducado'}

    client_id, secreto = credenciales(cfg)
    resp = requests.post(URL_TOKEN, data={
        'client_id': client_id,
        'client_secret': secreto,
        'device_code': cfg.device_code,
        'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
    }, timeout=TIMEOUT_HTTP)
    datos = resp.json() if resp.content else {}

    if resp.status_code == 200 and datos.get('refresh_token'):
        cfg.refresh_token_cifrado = cifrar(datos['refresh_token'])
        cfg.conectado_en = timezone.now()
        cfg.carpeta_id = ''  # cuenta nueva: la carpeta se busca/crea de nuevo
        cfg.carpeta_ruta_creada = ''
        _limpiar_device(cfg)
        cfg.cuenta_email = _email_de_la_cuenta(datos.get('access_token'))
        cfg.save()
        return {'estado': 'conectado', 'email': cfg.cuenta_email}

    error = datos.get('error', '')
    if error in ('authorization_pending', 'slow_down'):
        return {'estado': 'pendiente'}
    if error == 'access_denied':
        _limpiar_device(cfg)
        cfg.save()
        return {'estado': 'denegado'}
    if error == 'expired_token':
        _limpiar_device(cfg)
        cfg.save()
        return {'estado': 'caducado'}
    raise ErrorCopia(f"Google ha rechazado la conexión: {_error_google(resp)}")


def _email_de_la_cuenta(access_token):
    if not access_token:
        return ''
    try:
        resp = requests.get(URL_ABOUT, params={'fields': 'user(emailAddress)'},
                            headers={'Authorization': f'Bearer {access_token}'},
                            timeout=TIMEOUT_HTTP)
        return resp.json().get('user', {}).get('emailAddress', '') if resp.ok else ''
    except (requests.RequestException, ValueError):
        return ''


def desconectar(cfg):
    token = descifrar(cfg.refresh_token_cifrado) if cfg.refresh_token_cifrado else None
    if token:
        try:  # mejor esfuerzo: si falla, igualmente olvidamos el token aquí
            requests.post(URL_REVOKE, data={'token': token}, timeout=TIMEOUT_HTTP)
        except requests.RequestException:
            pass
    cfg.refresh_token_cifrado = None
    cfg.cuenta_email = ''
    cfg.conectado_en = None
    cfg.carpeta_id = ''
    cfg.carpeta_ruta_creada = ''
    _limpiar_device(cfg)
    cfg.save()


def _access_token(cfg):
    refresh = descifrar(cfg.refresh_token_cifrado) if cfg.refresh_token_cifrado else None
    if not refresh:
        raise ErrorCopia("No hay ninguna cuenta de Google conectada.")
    client_id, secreto = credenciales(cfg)
    resp = requests.post(URL_TOKEN, data={
        'client_id': client_id, 'client_secret': secreto,
        'refresh_token': refresh, 'grant_type': 'refresh_token',
    }, timeout=TIMEOUT_HTTP)
    if resp.status_code != 200:
        detalle = _error_google(resp)
        if 'invalid_grant' in detalle:
            raise ErrorCopia("Google ha retirado el permiso (caducado o revocado). "
                             "Vuelve a conectar la cuenta.")
        raise ErrorCopia(f"No se pudo renovar el acceso a Google: {detalle}")
    return resp.json()['access_token']


# ─── Drive ──────────────────────────────────────────────────────────────────

def _cabeceras(token):
    return {'Authorization': f'Bearer {token}'}


def _segmentos(ruta):
    return [s.strip() for s in (ruta or '').split('/') if s.strip()]


def _escapar(nombre):
    return nombre.replace('\\', '\\\\').replace("'", "\\'")


def _carpeta_sigue_viva(token, carpeta_id):
    resp = requests.get(f'{URL_FILES}/{carpeta_id}', params={'fields': 'id,trashed'},
                        headers=_cabeceras(token), timeout=TIMEOUT_HTTP)
    return resp.ok and not resp.json().get('trashed')


def asegurar_carpeta(cfg, token):
    """ID de la carpeta de destino, creándola si hace falta.

    Si la ruta no ha cambiado y la carpeta sigue existiendo se reutiliza
    aunque el usuario la haya movido o renombrado en Drive."""
    ruta = '/'.join(_segmentos(cfg.carpeta_ruta)) or 'Treasure'
    if cfg.carpeta_id and cfg.carpeta_ruta_creada == ruta and _carpeta_sigue_viva(token, cfg.carpeta_id):
        return cfg.carpeta_id

    padre = 'root'
    for nombre in _segmentos(ruta):
        q = (f"name = '{_escapar(nombre)}' and mimeType = '{MIME_CARPETA}' "
             f"and '{padre}' in parents and trashed = false")
        resp = requests.get(URL_FILES, params={'q': q, 'fields': 'files(id)', 'spaces': 'drive'},
                            headers=_cabeceras(token), timeout=TIMEOUT_HTTP)
        if not resp.ok:
            raise ErrorCopia(f"No se pudo buscar la carpeta en Drive: {_error_google(resp)}")
        encontradas = resp.json().get('files', [])
        if encontradas:
            padre = encontradas[0]['id']
            continue
        resp = requests.post(URL_FILES, params={'fields': 'id'}, json={
            'name': nombre, 'mimeType': MIME_CARPETA, 'parents': [padre],
        }, headers=_cabeceras(token), timeout=TIMEOUT_HTTP)
        if not resp.ok:
            raise ErrorCopia(f"No se pudo crear la carpeta «{nombre}» en Drive: {_error_google(resp)}")
        padre = resp.json()['id']

    cfg.carpeta_id = padre
    cfg.carpeta_ruta_creada = ruta
    cfg.save(update_fields=['carpeta_id', 'carpeta_ruta_creada'])
    return padre


def _subir(token, carpeta_id, ruta_local, nombre):
    tamano = os.path.getsize(ruta_local)
    inicio = requests.post(URL_UPLOAD, params={'uploadType': 'resumable', 'fields': 'id'},
                           json={'name': nombre, 'parents': [carpeta_id]},
                           headers={**_cabeceras(token),
                                    'X-Upload-Content-Type': 'application/octet-stream',
                                    'X-Upload-Content-Length': str(tamano)},
                           timeout=TIMEOUT_HTTP)
    if not inicio.ok or 'Location' not in inicio.headers:
        raise ErrorCopia(f"Drive no aceptó la subida: {_error_google(inicio)}")
    with open(ruta_local, 'rb') as fh:
        resp = requests.put(inicio.headers['Location'], data=fh,
                            headers={'Content-Type': 'application/octet-stream',
                                     'Content-Length': str(tamano)},
                            timeout=TIMEOUT_DUMP)
    if not resp.ok:
        raise ErrorCopia(f"Falló la subida a Drive: {_error_google(resp)}")
    return resp.json()['id'], tamano


def rotar(cfg, token, carpeta_id):
    """Manda a la papelera de Drive las copias que sobran (las más antiguas).
    Papelera y no borrado: si hiciera falta, Drive las guarda 30 días."""
    if not cfg.conservar:
        return 0
    q = f"'{carpeta_id}' in parents and trashed = false and name contains '{PREFIJO}'"
    resp = requests.get(URL_FILES, params={'q': q, 'fields': 'files(id,name)',
                                           'orderBy': 'createdTime desc', 'pageSize': 1000},
                        headers=_cabeceras(token), timeout=TIMEOUT_HTTP)
    if not resp.ok:
        return 0
    sobrantes = resp.json().get('files', [])[cfg.conservar:]
    for f in sobrantes:
        requests.patch(f"{URL_FILES}/{f['id']}", json={'trashed': True},
                       headers=_cabeceras(token), timeout=TIMEOUT_HTTP)
    return len(sobrantes)


# ─── Volcado ────────────────────────────────────────────────────────────────

def volcar_bd(destino):
    """pg_dump en formato custom (el mismo que descarga y restaura el panel)."""
    database_url = os.environ.get('DATABASE_URL', '')
    try:
        subprocess.run(['pg_dump', '-Fc', '-f', destino, database_url],
                       capture_output=True, check=True, timeout=TIMEOUT_DUMP)
    except FileNotFoundError:
        raise ErrorCopia("pg_dump no está instalado en este servidor.")
    except subprocess.CalledProcessError as e:
        raise ErrorCopia(f"pg_dump falló: {e.stderr.decode('utf-8', 'ignore').strip()}")
    except subprocess.TimeoutExpired:
        raise ErrorCopia("pg_dump ha tardado demasiado y se ha cancelado.")


# ─── Copia completa ─────────────────────────────────────────────────────────

def _tomar_bloqueo():
    ahora = timezone.now()
    return CopiaDriveConfig.objects.filter(pk=1).filter(
        Q(en_curso_desde__isnull=True) | Q(en_curso_desde__lt=ahora - BLOQUEO_MAXIMO)
    ).update(en_curso_desde=ahora) == 1


def _soltar_bloqueo():
    CopiaDriveConfig.objects.filter(pk=1).update(en_curso_desde=None)


def hacer_copia(origen='auto'):
    """Vuelca, sube y rota. Siempre deja un registro en el historial."""
    cfg = CopiaDriveConfig.obtener()
    if not _tomar_bloqueo():
        raise ErrorCopia("Ya hay una copia en marcha.")
    inicio = time.monotonic()
    ahora_local = timezone.localtime()
    nombre = f"{PREFIJO}{ahora_local:%Y%m%d_%H%M%S}.dump"
    registro = CopiaDriveRegistro(origen=origen, nombre_archivo=nombre)
    try:
        cfg.refresh_from_db()
        token = _access_token(cfg)
        carpeta = asegurar_carpeta(cfg, token)
        with tempfile.TemporaryDirectory() as tmp:
            ruta = os.path.join(tmp, nombre)
            volcar_bd(ruta)
            file_id, tamano = _subir(token, carpeta, ruta, nombre)
        borradas = rotar(cfg, token, carpeta)
        registro.ok = True
        registro.drive_file_id = file_id
        registro.tamano = tamano
        registro.mensaje = (f"{borradas} copia{'s' if borradas != 1 else ''} antigua"
                            f"{'s' if borradas != 1 else ''} a la papelera.") if borradas else ''
        CopiaDriveConfig.objects.filter(pk=1).update(ultima_copia_en=timezone.now())
    except ErrorCopia as e:
        registro.mensaje = str(e)
    except requests.RequestException as e:
        registro.mensaje = f"No se pudo hablar con Google: {e}"
    except Exception as e:  # que un fallo raro quede en el historial, no en silencio
        registro.mensaje = f"Error inesperado: {e}"
    finally:
        registro.duracion_seg = round(time.monotonic() - inicio, 1)
        registro.save()
        _soltar_bloqueo()
    return registro


# ─── Programación ───────────────────────────────────────────────────────────

def _hito(cfg, fecha):
    return timezone.make_aware(datetime.datetime.combine(fecha, datetime.time(cfg.hora)))


def ultimo_hito(cfg, ahora=None):
    """El último momento programado que ya ha pasado (o None si desactivada)."""
    ahora = timezone.localtime(ahora or timezone.now())
    hoy = ahora.date()
    if cfg.periodicidad == 'diaria':
        h = _hito(cfg, hoy)
        return h if h <= ahora else _hito(cfg, hoy - datetime.timedelta(days=1))
    if cfg.periodicidad == 'semanal':
        dias = (hoy.weekday() - cfg.dia_semana) % 7
        h = _hito(cfg, hoy - datetime.timedelta(days=dias))
        return h if h <= ahora else h - datetime.timedelta(days=7)
    if cfg.periodicidad == 'mensual':
        dia = min(max(cfg.dia_mes, 1), 28)
        h = _hito(cfg, hoy.replace(day=dia))
        if h <= ahora:
            return h
        mes_ant = hoy.replace(day=1) - datetime.timedelta(days=1)
        return _hito(cfg, mes_ant.replace(day=dia))
    return None


def proxima_copia(cfg, ahora=None):
    ahora = timezone.localtime(ahora or timezone.now())
    ultimo = ultimo_hito(cfg, ahora)
    if ultimo is None:
        return None
    if cfg.ultima_copia_en is None or cfg.ultima_copia_en < ultimo:
        return ahora  # pendiente: se hará en la próxima vuelta
    if cfg.periodicidad == 'diaria':
        return ultimo + datetime.timedelta(days=1)
    if cfg.periodicidad == 'semanal':
        return ultimo + datetime.timedelta(days=7)
    siguiente = (ultimo.date().replace(day=1) + datetime.timedelta(days=32)).replace(day=ultimo.day)
    return _hito(cfg, siguiente)


def debe_copiar(cfg, ahora=None):
    ahora = ahora or timezone.now()
    if not cfg.conectado:
        return False
    ultimo = ultimo_hito(cfg, ahora)
    if ultimo is None:
        return False
    if cfg.ultima_copia_en and cfg.ultima_copia_en >= ultimo:
        return False
    # Tras un fallo automático, esperar antes de reintentar.
    fallo = CopiaDriveRegistro.objects.filter(origen='auto').first()
    if fallo and not fallo.ok and fallo.fecha > ahora - REINTENTO_TRAS_ERROR:
        return False
    return True
