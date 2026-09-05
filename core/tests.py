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
