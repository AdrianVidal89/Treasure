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


class DashboardCapitalLiquidoTests(TestCase):
    """El capital líquido del dashboard es dinero DISPONIBLE: saldo de los
    fondos común/ahorro más el valor de los depósitos (que también se puede
    disponer), igual que la vista de Evolución."""

    def setUp(self):
        import datetime
        from decimal import Decimal

        from core.models import Hogar
        from finanzas.models import (
            FondoFamiliar, Inversion, MovimientoInversion, SaldoRealFondo,
        )

        self.date = datetime.date
        self.Decimal = Decimal
        self.Inversion = Inversion
        self.MovimientoInversion = MovimientoInversion

        self.user = User.objects.create_user(username='luis', password='clave12345')
        self.hogar = Hogar.objects.create(nombre='Casa', creado_por=self.user)
        profile, _ = UserProfile.objects.get_or_create(user=self.user)
        profile.hogar = self.hogar
        profile.save()
        self.client.force_login(self.user)

        self.hoy = datetime.date.today()
        self.fondo = FondoFamiliar.objects.create(
            hogar=self.hogar, nombre='Común', tipo_fondo='comun')
        SaldoRealFondo.objects.create(
            fondo=self.fondo, año=self.hoy.year, mes=self.hoy.month,
            saldo=Decimal('10000'))

    def _deposito(self, nombre, importe, fondo=None):
        dep = self.Inversion.objects.create(
            usuario=self.user, nombre=nombre, tipo='DEPOSITO', fondo=fondo,
            deposito_tipo_interes=self.Decimal('0'), deposito_frecuencia='anual')
        self.MovimientoInversion.objects.create(
            inversion=dep, fecha=self.date(self.hoy.year, 1, 1), tipo='COMPRA',
            cantidad=self.Decimal(importe), precio_unitario=self.Decimal('1'))
        return dep

    def _capital(self):
        resp = self.client.get(reverse('dashboard'))
        self.assertEqual(resp.status_code, 200)
        return resp.context['capital_liquido_total']

    def test_deposito_suma_al_capital_liquido(self):
        self._deposito('Depo', '6000')
        self.assertEqual(self._capital(), self.Decimal('16000.00'))

    def test_deposito_vinculado_a_fondo_no_se_cuenta_dos_veces(self):
        self._deposito('Depo', '6000', fondo=self.fondo)
        # El saldo manual del fondo lo sustituye el valor real del depósito.
        self.assertEqual(self._capital(), self.Decimal('6000.00'))

    def test_sin_saldos_registrados_el_deposito_sigue_siendo_liquido(self):
        from finanzas.models import SaldoRealFondo
        SaldoRealFondo.objects.all().delete()
        self._deposito('Depo', '4000')
        self.assertEqual(self._capital(), self.Decimal('4000.00'))

    def test_sin_depositos_el_capital_es_el_saldo_de_los_fondos(self):
        self.assertEqual(self._capital(), self.Decimal('10000'))
