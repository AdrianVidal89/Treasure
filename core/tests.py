from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import ConsultaSQL, Hogar, UserProfile


class ConsolaSQLPermisosTests(TestCase):
    """La consola da acceso directo a la base de datos: solo superusuarios."""

    def setUp(self):
        self.jefe = User.objects.create_superuser('jefe', 'j@x.com', 'x')
        self.admin_hogar = User.objects.create_user('admin', password='x')
        perfil, _ = UserProfile.objects.get_or_create(user=self.admin_hogar)
        perfil.rol = 'admin'
        perfil.hogar = Hogar.objects.create(nombre='Casa', creado_por=self.admin_hogar)
        perfil.save()

    def test_el_superusuario_entra(self):
        self.client.force_login(self.jefe)
        self.assertEqual(self.client.get(reverse('consola_sql')).status_code, 200)

    def test_ser_admin_de_un_hogar_no_basta(self):
        self.client.force_login(self.admin_hogar)
        resp = self.client.get(reverse('consola_sql'))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'], reverse('panel_admin'))

    def test_sin_sesion_no_se_entra(self):
        resp = self.client.get(reverse('consola_sql'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('login'), resp['Location'])

    def test_el_panel_solo_enseña_el_enlace_al_superusuario(self):
        self.client.force_login(self.admin_hogar)
        self.assertNotContains(self.client.get(reverse('panel_admin')), reverse('consola_sql'))
        self.client.force_login(self.jefe)
        self.assertContains(self.client.get(reverse('panel_admin')), reverse('consola_sql'))


class ConsolaSQLSalvaguardasTests(TestCase):
    """Lo que la consola NO deja hacer, que es lo que evita el desastre."""

    def setUp(self):
        self.jefe = User.objects.create_superuser('jefe', 'j@x.com', 'x')
        self.client.force_login(self.jefe)
        self.hogar = Hogar.objects.create(nombre='Casa', creado_por=self.jefe)

    def _ejecutar(self, sql, accion='probar'):
        return self.client.post(reverse('consola_sql'), {'sql': sql, 'accion': accion})

    def test_no_se_admiten_varias_sentencias_a_la_vez(self):
        resp = self._ejecutar("SELECT 1; DELETE FROM core_hogar")
        self.assertIn('una sentencia', resp.context['error'])
        self.assertTrue(Hogar.objects.filter(id=self.hogar.id).exists())

    def test_no_se_admite_borrar_una_tabla(self):
        resp = self._ejecutar("DROP TABLE core_hogar")
        self.assertIn('DROP', resp.context['error'])

    def test_no_se_admite_cambiar_la_estructura(self):
        for sql in ["ALTER TABLE core_hogar ADD COLUMN x int",
                    "TRUNCATE TABLE core_hogar",
                    "CREATE TABLE t (a int)"]:
            with self.subTest(sql=sql):
                self.assertIsNotNone(self._ejecutar(sql).context['error'])

    def test_una_consulta_vacia_no_hace_nada(self):
        resp = self.client.post(reverse('consola_sql'), {'sql': '   ', 'accion': 'probar'})
        self.assertIsNone(resp.context['resultado'])
        self.assertIsNone(resp.context['error'])

    def test_un_error_de_sql_se_enseña_y_no_rompe_la_pagina(self):
        resp = self._ejecutar("SELECT * FROM tabla_que_no_existe")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(resp.context['error'])


class ConsolaSQLEjecucionTests(TestCase):
    """Leer se ejecuta; escribir se prueba primero y solo se aplica al confirmar."""

    def setUp(self):
        self.jefe = User.objects.create_superuser('jefe', 'j@x.com', 'x')
        self.client.force_login(self.jefe)
        self.hogar = Hogar.objects.create(nombre='Casa', creado_por=self.jefe)

    def _ejecutar(self, sql, accion='probar'):
        return self.client.post(reverse('consola_sql'), {'sql': sql, 'accion': accion})

    def test_un_select_devuelve_columnas_y_filas(self):
        resp = self._ejecutar("SELECT nombre FROM core_hogar")
        r = resp.context['resultado']
        self.assertFalse(r['es_escritura'])
        self.assertEqual(r['columnas'], ['nombre'])
        self.assertEqual(r['filas'], [['Casa']])

    def test_una_escritura_sin_confirmar_solo_cuenta_filas_y_no_cambia_nada(self):
        r = self._ejecutar("UPDATE core_hogar SET nombre = 'Cambiada'").context['resultado']
        self.assertTrue(r['es_escritura'])
        self.assertFalse(r['aplicado'])
        self.assertEqual(r['filas_afectadas'], 1)

        self.hogar.refresh_from_db()
        self.assertEqual(self.hogar.nombre, 'Casa')

    def test_al_confirmar_si_se_aplica(self):
        self._ejecutar("UPDATE core_hogar SET nombre = 'Cambiada'")
        self.hogar.refresh_from_db()
        self.assertEqual(self.hogar.nombre, 'Casa')

        self._ejecutar("UPDATE core_hogar SET nombre = 'Cambiada'", accion='aplicar')
        self.hogar.refresh_from_db()
        self.assertEqual(self.hogar.nombre, 'Cambiada')

    def test_queda_registro_de_la_prueba_y_de_la_aplicacion(self):
        self._ejecutar("UPDATE core_hogar SET nombre = 'Cambiada'")
        self._ejecutar("UPDATE core_hogar SET nombre = 'Cambiada'", accion='aplicar')

        registros = list(ConsultaSQL.objects.order_by('id'))
        self.assertEqual([r.aplicado for r in registros], [False, True])
        self.assertEqual([r.verbo for r in registros], ['UPDATE', 'UPDATE'])
        self.assertEqual([r.usuario for r in registros], [self.jefe, self.jefe])

    def test_tambien_queda_registro_de_lo_que_se_rechaza(self):
        self._ejecutar("DROP TABLE core_hogar")
        registro = ConsultaSQL.objects.get()
        self.assertIn('DROP', registro.error)
        self.assertFalse(registro.aplicado)

    def test_el_resultado_se_recorta_para_no_traer_la_tabla_entera(self):
        from .sql_consola import LIMITE_FILAS, ejecutar

        for i in range(LIMITE_FILAS + 5):
            Hogar.objects.create(nombre=f'Casa {i}', creado_por=self.jefe)

        r = ejecutar("SELECT nombre FROM core_hogar")
        self.assertEqual(len(r['filas']), LIMITE_FILAS)
        self.assertTrue(r['hay_mas'])

    def test_el_esquema_lista_las_tablas_con_sus_columnas(self):
        tablas = self.client.get(reverse('consola_sql')).context['esquema']
        hogar = next(t for t in tablas if t['nombre'] == 'core_hogar')
        self.assertIn('nombre', hogar['columnas'])
        self.assertIn('moneda_principal', hogar['columnas'])


# ─── Copias a Google Drive ──────────────────────────────────────────────────
import datetime as _dt
from unittest import mock as _mock

from django.test import TestCase as _TC
from django.urls import reverse as _reverse
from django.utils import timezone as _tz


class _Resp:
    def __init__(self, status=200, datos=None, headers=None):
        self.status_code = status
        self._datos = datos or {}
        self.headers = headers or {}
        self.content = b'x'

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._datos


def _cfg_conectada(**extra):
    from asistente_ia.crypto import cifrar
    from core.models import CopiaDriveConfig
    cfg = CopiaDriveConfig.obtener()
    cfg.client_id = 'cid'
    cfg.client_secret_cifrado = cifrar('secreto')
    cfg.refresh_token_cifrado = cifrar('refresh')
    for k, v in extra.items():
        setattr(cfg, k, v)
    cfg.save()
    return cfg


def _local(*args):
    return _tz.make_aware(_dt.datetime(*args))


class ProgramacionCopiasTests(_TC):
    def test_diaria_antes_de_la_hora_mira_ayer(self):
        from core.copias_drive import ultimo_hito
        cfg = _cfg_conectada(periodicidad='diaria', hora=3)
        self.assertEqual(ultimo_hito(cfg, _local(2026, 10, 16, 2, 0)), _local(2026, 10, 15, 3))
        self.assertEqual(ultimo_hito(cfg, _local(2026, 10, 16, 4, 0)), _local(2026, 10, 16, 3))

    def test_semanal_y_mensual(self):
        from core.copias_drive import ultimo_hito
        cfg = _cfg_conectada(periodicidad='semanal', hora=3, dia_semana=0)  # lunes
        # 16/10/2026 es viernes → el lunes anterior es el 12.
        self.assertEqual(ultimo_hito(cfg, _local(2026, 10, 16, 12)), _local(2026, 10, 12, 3))
        cfg.periodicidad, cfg.dia_mes = 'mensual', 20
        self.assertEqual(ultimo_hito(cfg, _local(2026, 10, 16, 12)), _local(2026, 9, 20, 3))

    def test_debe_copiar(self):
        from core.copias_drive import debe_copiar
        from core.models import CopiaDriveRegistro
        ahora = _local(2026, 10, 16, 10)
        cfg = _cfg_conectada(periodicidad='diaria', hora=3)
        self.assertTrue(debe_copiar(cfg, ahora))  # nunca se ha hecho
        cfg.ultima_copia_en = _local(2026, 10, 16, 3, 5)
        self.assertFalse(debe_copiar(cfg, ahora))  # ya hecha hoy
        cfg.ultima_copia_en = _local(2026, 10, 15, 3, 5)
        self.assertTrue(debe_copiar(cfg, ahora))
        # Un fallo reciente frena el reintento.
        CopiaDriveRegistro.objects.create(origen='auto', ok=False, mensaje='x')
        self.assertFalse(debe_copiar(cfg, _tz.now()))
        cfg.periodicidad = 'desactivada'
        self.assertFalse(debe_copiar(cfg, ahora))

    def test_sin_cuenta_no_copia(self):
        from core.copias_drive import debe_copiar
        from core.models import CopiaDriveConfig
        cfg = CopiaDriveConfig.obtener()
        cfg.periodicidad = 'diaria'
        self.assertFalse(debe_copiar(cfg))


class ConexionDriveTests(_TC):
    def test_flujo_de_dispositivo(self):
        from core import copias_drive as m
        from asistente_ia.crypto import descifrar
        cfg = _cfg_conectada(refresh_token_cifrado=None)
        with _mock.patch.object(m.requests, 'post', return_value=_Resp(200, {
            'device_code': 'dc', 'user_code': 'ABCD-EFGH',
            'verification_url': 'https://www.google.com/device', 'expires_in': 1800, 'interval': 5,
        })):
            info = m.iniciar_conexion(cfg)
        self.assertEqual(info['codigo'], 'ABCD-EFGH')

        with _mock.patch.object(m.requests, 'post', return_value=_Resp(428, {'error': 'authorization_pending'})):
            self.assertEqual(m.comprobar_conexion(cfg)['estado'], 'pendiente')

        with _mock.patch.object(m.requests, 'post', return_value=_Resp(200, {
                'access_token': 'at', 'refresh_token': 'nuevo'})), \
             _mock.patch.object(m.requests, 'get', return_value=_Resp(200, {'user': {'emailAddress': 'yo@x.com'}})):
            res = m.comprobar_conexion(cfg)
        self.assertEqual(res['estado'], 'conectado')
        cfg.refresh_from_db()
        self.assertEqual(descifrar(cfg.refresh_token_cifrado), 'nuevo')
        self.assertEqual(cfg.cuenta_email, 'yo@x.com')
        self.assertEqual(cfg.device_code, '')


class HacerCopiaTests(_TC):
    def _fake_dump(self, args, **kw):
        destino = args[args.index('-f') + 1]
        with open(destino, 'wb') as fh:
            fh.write(b'PGDMP' + b'0' * 100)
        return _mock.Mock(returncode=0)

    def test_copia_completa_crea_carpetas_sube_y_rota(self):
        from core import copias_drive as m
        from core.models import CopiaDriveConfig
        _cfg_conectada(carpeta_ruta='Treasure/Copias', conservar=2, periodicidad='diaria')

        def post(url, **kw):
            if url == m.URL_TOKEN:
                return _Resp(200, {'access_token': 'at'})
            if url == m.URL_FILES:  # crear carpeta
                return _Resp(200, {'id': 'carpeta-' + kw['json']['name']})
            if url == m.URL_UPLOAD:
                return _Resp(200, {}, {'Location': 'https://subida'})
            raise AssertionError(url)

        def get(url, params=None, **kw):
            if 'in parents' in params['q'] and 'mimeType' in params['q']:
                return _Resp(200, {'files': []})  # ninguna carpeta aún
            return _Resp(200, {'files': [{'id': f'f{i}', 'name': f'treasure_backup_{i}'} for i in range(4)]})

        with _mock.patch.object(m.subprocess, 'run', side_effect=self._fake_dump), \
             _mock.patch.object(m.requests, 'post', side_effect=post), \
             _mock.patch.object(m.requests, 'get', side_effect=get), \
             _mock.patch.object(m.requests, 'put', return_value=_Resp(200, {'id': 'nuevo'})) as put, \
             _mock.patch.object(m.requests, 'patch', return_value=_Resp(200)) as patch:
            reg = m.hacer_copia()

        self.assertTrue(reg.ok, reg.mensaje)
        self.assertEqual(reg.drive_file_id, 'nuevo')
        self.assertEqual(reg.tamano, 105)
        put.assert_called_once()
        self.assertEqual(patch.call_count, 2)  # 4 copias, se conservan 2
        cfg = CopiaDriveConfig.obtener()
        self.assertEqual(cfg.carpeta_id, 'carpeta-Copias')
        self.assertIsNotNone(cfg.ultima_copia_en)
        self.assertIsNone(cfg.en_curso_desde)

    def test_fallo_queda_en_historial_y_suelta_bloqueo(self):
        from core import copias_drive as m
        from core.models import CopiaDriveConfig
        _cfg_conectada()
        with _mock.patch.object(m.requests, 'post', return_value=_Resp(400, {'error': 'invalid_grant'})):
            reg = m.hacer_copia()
        self.assertFalse(reg.ok)
        self.assertIn('Vuelve a conectar', reg.mensaje)
        self.assertIsNone(CopiaDriveConfig.obtener().en_curso_desde)


class VistaCopiasTests(_TC):
    def setUp(self):
        from django.contrib.auth.models import User
        self.admin = User.objects.create_superuser('root', 'r@x.com', 'pw')
        self.normal = User.objects.create_user('pepe', password='pw')

    def test_solo_admin(self):
        self.client.force_login(self.normal)
        self.assertEqual(self.client.get(_reverse('copias_drive')).status_code, 302)

    def test_pagina_y_guardar_programacion(self):
        from core.models import CopiaDriveConfig
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(_reverse('copias_drive')), 'Copias de seguridad')
        self.client.post(_reverse('copias_drive'), {
            'accion': 'programacion', 'periodicidad': 'semanal', 'hora': '4',
            'dia_semana': '6', 'dia_mes': '1', 'conservar': '5', 'carpeta_ruta': ' Backups / Casa ',
        })
        cfg = CopiaDriveConfig.obtener()
        self.assertEqual((cfg.periodicidad, cfg.hora, cfg.dia_semana, cfg.conservar), ('semanal', 4, 6, 5))
        self.assertEqual(cfg.carpeta_ruta, 'Backups/Casa')

    def test_credenciales_se_cifran(self):
        from core.models import CopiaDriveConfig
        self.client.force_login(self.admin)
        self.client.post(_reverse('copias_drive'), {'accion': 'credenciales', 'client_id': 'abc', 'client_secret': 's3'})
        cfg = CopiaDriveConfig.obtener()
        self.assertEqual(cfg.client_id, 'abc')
        self.assertNotIn(b's3', bytes(cfg.client_secret_cifrado))
