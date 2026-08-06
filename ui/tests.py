import json

from django.test import TestCase
from django.urls import reverse
from django.contrib.auth.models import User

from core.models import UserProfile


class TemaInterfazTests(TestCase):
    """El tema elegido se guarda en el perfil y se mantiene hasta cambiarlo."""

    def setUp(self):
        self.user = User.objects.create_user(username='ana', password='clave12345')
        self.profile, _ = UserProfile.objects.get_or_create(user=self.user)
        self.client.force_login(self.user)

    def _tema_renderizado(self):
        html = self.client.get(reverse('dashboard')).content.decode()
        return 'black' if 'data-theme="black"' in html else 'claro'

    def test_tema_por_defecto_es_claro(self):
        self.assertEqual(self.profile.tema, 'claro')
        self.assertEqual(self._tema_renderizado(), 'claro')

    def test_cambiar_tema_lo_guarda_en_el_perfil(self):
        resp = self.client.post(
            reverse('api_cambiar_tema'),
            data=json.dumps({'tema': 'black'}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {'ok': True, 'tema': 'black'})

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.tema, 'black')
        self.assertEqual(self._tema_renderizado(), 'black')

    def test_el_tema_sobrevive_al_cierre_de_sesion(self):
        self.profile.tema = 'black'
        self.profile.save()

        self.client.logout()
        self.client.force_login(self.user)

        self.assertEqual(self._tema_renderizado(), 'black')

    def test_tema_desconocido_no_toca_el_perfil(self):
        resp = self.client.post(
            reverse('api_cambiar_tema'),
            data=json.dumps({'tema': 'rosa'}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 400)

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.tema, 'claro')

    def test_solo_se_admite_post(self):
        resp = self.client.get(reverse('api_cambiar_tema'))
        self.assertEqual(resp.status_code, 405)

    def test_usuario_anonimo_no_puede_cambiar_el_tema(self):
        self.client.logout()
        resp = self.client.post(
            reverse('api_cambiar_tema'),
            data=json.dumps({'tema': 'black'}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('login'), resp['Location'])

    def test_el_formulario_de_perfil_tambien_guarda_el_tema(self):
        resp = self.client.post(reverse('editar_perfil'), {
            'tema': 'black',
            'idioma': 'es',
            'moneda': 'EUR',
            'inflacion_referencia': '2.0',
            'porcentaje_max_endeudamiento': '30.0',
        })
        self.assertEqual(resp.status_code, 302)

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.tema, 'black')
