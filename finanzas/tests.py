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
    # La cartera es a nivel de compra: la compra hereda el grupo por defecto del activo.
    MovimientoInversion.objects.create(
        inversion=inv, fecha=datetime.date(2026, 1, 15), tipo='COMPRA',
        cantidad=Decimal(str(compra_cantidad)), precio_unitario=Decimal(str(compra_precio)),
        grupo=grupo,
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

    def test_carteras_por_compra_en_el_mismo_activo(self):
        """Dos compras del MISMO activo pueden ir a carteras distintas."""
        c1 = GrupoInversion.objects.create(usuario=self.user, nombre='C1')
        c2 = GrupoInversion.objects.create(usuario=self.user, nombre='C2')
        inv = Inversion.objects.create(usuario=self.user, nombre='VWCE', tipo='ETF', plataforma='Revolut')
        ValorActualInversion.objects.create(inversion=inv, valor_unitario=Decimal('12'), fuente='t')
        # Compra 1 -> C1: aporta 100 (10x10), vale 120 (10x12) -> +20%
        MovimientoInversion.objects.create(inversion=inv, fecha=datetime.date(2026, 1, 5),
            tipo='COMPRA', cantidad=Decimal('10'), precio_unitario=Decimal('10'), grupo=c1)
        # Compra 2 -> C2: aporta 100 (10x10), vale 120 -> +20%
        MovimientoInversion.objects.create(inversion=inv, fecha=datetime.date(2026, 3, 5),
            tipo='COMPRA', cantidad=Decimal('10'), precio_unitario=Decimal('10'), grupo=c2)

        self.assertEqual(c1.total_aportado, Decimal('100'))
        self.assertEqual(c1.valor_cartera, Decimal('120'))
        self.assertEqual(c1.rentabilidad, 20.0)
        self.assertEqual(c1.num_compras, 1)
        self.assertEqual(c2.rentabilidad, 20.0)
        self.assertEqual(c2.num_activos, 1)


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
        # Cartera por defecto del activo
        self.assertEqual(self.inv1.grupo_id, cartera.id)
        self.assertEqual(self.inv2.grupo_id, cartera.id)
        # Y cascada a las compras (nivel autoritativo)
        compras = MovimientoInversion.objects.filter(
            inversion__in=[self.inv1, self.inv2], tipo='COMPRA'
        )
        self.assertTrue(all(m.grupo_id == cartera.id for m in compras))
        self.assertEqual(cartera.num_compras, 2)

    def test_filtro_cartera_incluye_activos_por_compra(self):
        """Al abrir una cartera, deben aparecer los activos cuya COMPRA está
        asignada a ella aunque el activo no tenga 'cartera por defecto'."""
        cartera = GrupoInversion.objects.create(usuario=self.user, nombre='wesop')
        inv = Inversion.objects.create(usuario=self.user, nombre='Apple', tipo='ACCION', plataforma='Uptevia')
        ValorActualInversion.objects.create(inversion=inv, valor_unitario=Decimal('200'), fuente='t')
        MovimientoInversion.objects.create(inversion=inv, fecha=datetime.date(2026, 2, 1),
            tipo='COMPRA', cantidad=Decimal('2'), precio_unitario=Decimal('150'), grupo=cartera)
        self.assertIsNone(inv.grupo_id)  # sin cartera por defecto

        resp = self.client.get(reverse('finanzas:listar') + f'?cartera={cartera.id}')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Apple')

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
