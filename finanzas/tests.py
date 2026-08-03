import datetime
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.contrib.auth.models import User

from .models import (
    Inversion,
    MovimientoInversion,
    ValorActualInversion,
    GrupoInversion,
)


def _crear_inversion(usuario, nombre, tipo, compra_cantidad, compra_precio, valor_unitario, grupo=None):
    inv = Inversion.objects.create(
        usuario=usuario, nombre=nombre, tipo=tipo, plataforma='Revolut', grupo=grupo,
    )
    MovimientoInversion.objects.create(
        inversion=inv, fecha=datetime.date(2026, 1, 15), tipo='COMPRA',
        cantidad=Decimal(str(compra_cantidad)), precio_unitario=Decimal(str(compra_precio)),
    )
    ValorActualInversion.objects.create(
        inversion=inv, valor_unitario=Decimal(str(valor_unitario)), fuente='Test',
    )
    return inv


class GrupoInversionRentabilidadTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('inversor', password='x')

    def test_rentabilidad_agregada_de_la_cartera(self):
        cartera = GrupoInversion.objects.create(usuario=self.user, nombre='Growth')
        # Aporta 100 (10 x 10), vale 120 (10 x 12) -> +20%
        _crear_inversion(self.user, 'ETF A', 'ETF', 10, 10, 12, grupo=cartera)
        # Aporta 100 (5 x 20), vale 90 (5 x 18) -> -10%
        _crear_inversion(self.user, 'Acción B', 'ACCION', 5, 20, 18, grupo=cartera)

        # Total aportado 200, valor 210 -> +5%
        self.assertEqual(cartera.total_aportado, Decimal('200'))
        self.assertEqual(cartera.valor_cartera, Decimal('210.00'))
        self.assertEqual(cartera.rentabilidad, 5.0)
        self.assertEqual(cartera.num_activos, 2)

    def test_cartera_vacia_rentabilidad_none(self):
        cartera = GrupoInversion.objects.create(usuario=self.user, nombre='Vacía')
        self.assertIsNone(cartera.rentabilidad)
        self.assertEqual(cartera.num_activos, 0)


class AccionMasivaTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('inversor', password='x')
        self.otro = User.objects.create_user('otro', password='x')
        self.client.force_login(self.user)
        self.inv1 = _crear_inversion(self.user, 'ETF A', 'ETF', 10, 10, 12)
        self.inv2 = _crear_inversion(self.user, 'Acción B', 'ACCION', 5, 20, 18)
        self.ajena = _crear_inversion(self.otro, 'Cripto C', 'CRIPTO', 1, 100, 110)

    def test_cambiar_tipo_en_bloque(self):
        resp = self.client.post(reverse('finanzas:inversiones_accion_masiva'), {
            'inversion_ids': [self.inv1.id, self.inv2.id],
            'accion_masiva': 'set_tipo',
            'valor_tipo': 'FONDO',
        })
        self.assertEqual(resp.status_code, 302)
        self.inv1.refresh_from_db()
        self.inv2.refresh_from_db()
        self.assertEqual(self.inv1.tipo, 'FONDO')
        self.assertEqual(self.inv2.tipo, 'FONDO')

    def test_asignar_cartera_en_bloque(self):
        cartera = GrupoInversion.objects.create(usuario=self.user, nombre='Div')
        self.client.post(reverse('finanzas:inversiones_accion_masiva'), {
            'inversion_ids': [self.inv1.id, self.inv2.id],
            'accion_masiva': 'set_grupo',
            'valor_grupo': cartera.id,
        })
        self.inv1.refresh_from_db()
        self.inv2.refresh_from_db()
        self.assertEqual(self.inv1.grupo_id, cartera.id)
        self.assertEqual(self.inv2.grupo_id, cartera.id)

    def test_no_afecta_inversiones_de_otro_usuario(self):
        self.client.post(reverse('finanzas:inversiones_accion_masiva'), {
            'inversion_ids': [self.ajena.id],
            'accion_masiva': 'set_tipo',
            'valor_tipo': 'FONDO',
        })
        self.ajena.refresh_from_db()
        self.assertEqual(self.ajena.tipo, 'CRIPTO')  # sin cambios

    def test_tipo_invalido_no_aplica(self):
        self.client.post(reverse('finanzas:inversiones_accion_masiva'), {
            'inversion_ids': [self.inv1.id],
            'accion_masiva': 'set_tipo',
            'valor_tipo': 'NO_EXISTE',
        })
        self.inv1.refresh_from_db()
        self.assertEqual(self.inv1.tipo, 'ETF')  # sin cambios


class CarterasCrudTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('inversor', password='x')
        self.client.force_login(self.user)

    def test_crear_y_borrar_cartera(self):
        self.client.post(reverse('finanzas:carteras'), {'accion': 'crear', 'nombre': 'Mi Cartera', 'color': '#123456'})
        cartera = GrupoInversion.objects.get(usuario=self.user, nombre='Mi Cartera')
        self.assertEqual(cartera.color, '#123456')

        # Un activo asignado no debe borrarse al borrar la cartera (SET_NULL)
        inv = _crear_inversion(self.user, 'ETF A', 'ETF', 10, 10, 12, grupo=cartera)
        self.client.post(reverse('finanzas:carteras'), {'accion': 'borrar', 'cartera_id': cartera.id})
        self.assertFalse(GrupoInversion.objects.filter(id=cartera.id).exists())
        inv.refresh_from_db()
        self.assertIsNone(inv.grupo_id)
