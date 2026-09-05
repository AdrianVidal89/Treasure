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
    AportacionRecurrente,
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


class AportacionRecurrenteTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('inversor', password='x')
        self.inv = Inversion.objects.create(
            usuario=self.user, nombre='Depósito Irene', tipo='DEPOSITO',
            plataforma='Banco',
        )

    def test_meses_pendientes_calcula_rango_completo(self):
        regla = AportacionRecurrente.objects.create(
            inversion=self.inv, importe=Decimal('100'), dia_mes=5,
            fecha_inicio=datetime.date(2026, 1, 1),
        )
        pendientes = regla.meses_pendientes(hasta=datetime.date(2026, 4, 15))
        self.assertEqual(pendientes, [(2026, 1), (2026, 2), (2026, 3), (2026, 4)])

    def test_meses_pendientes_excluye_ya_generados(self):
        regla = AportacionRecurrente.objects.create(
            inversion=self.inv, importe=Decimal('100'), dia_mes=5,
            fecha_inicio=datetime.date(2026, 1, 1),
        )
        MovimientoInversion.objects.create(
            inversion=self.inv, fecha=datetime.date(2026, 2, 5), tipo='COMPRA',
            cantidad=Decimal('100'), precio_unitario=Decimal('1'), origen_recurrente=regla,
        )
        pendientes = regla.meses_pendientes(hasta=datetime.date(2026, 3, 15))
        self.assertEqual(pendientes, [(2026, 1), (2026, 3)])

    def test_meses_pendientes_respeta_fecha_fin(self):
        regla = AportacionRecurrente.objects.create(
            inversion=self.inv, importe=Decimal('100'), dia_mes=5,
            fecha_inicio=datetime.date(2026, 1, 1), fecha_fin=datetime.date(2026, 2, 1),
        )
        pendientes = regla.meses_pendientes(hasta=datetime.date(2026, 12, 31))
        self.assertEqual(pendientes, [(2026, 1), (2026, 2)])


class AportacionRecurrenteGenerarViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('inversor', password='x')
        self.client.force_login(self.user)
        self.inv = Inversion.objects.create(
            usuario=self.user, nombre='Depósito Irene', tipo='DEPOSITO',
            plataforma='Banco',
        )
        # Regla que arrancó hace 3 meses (incluido el actual) para no depender de "hoy" fijo.
        hoy = datetime.date.today()
        inicio_mes = hoy.replace(day=1)
        hace_2_meses = (inicio_mes - datetime.timedelta(days=1)).replace(day=1)
        hace_2_meses = (hace_2_meses - datetime.timedelta(days=1)).replace(day=1)
        self.regla = AportacionRecurrente.objects.create(
            inversion=self.inv, importe=Decimal('150'), dia_mes=5,
            fecha_inicio=hace_2_meses,
        )

    def test_generar_crea_movimientos_pendientes(self):
        pendientes_antes = self.regla.meses_pendientes()
        self.assertEqual(len(pendientes_antes), 3)  # hace 2 meses, el mes pasado y el actual

        resp = self.client.post(reverse('finanzas:aportacion_recurrente_generar', args=[self.inv.id, self.regla.id]))
        self.assertEqual(resp.status_code, 302)

        movs = MovimientoInversion.objects.filter(inversion=self.inv, origen_recurrente=self.regla)
        self.assertEqual(movs.count(), 3)
        self.assertTrue(all(m.tipo == 'COMPRA' and m.cantidad == Decimal('150') for m in movs))

    def test_generar_es_idempotente(self):
        self.client.post(reverse('finanzas:aportacion_recurrente_generar', args=[self.inv.id, self.regla.id]))
        primera_cuenta = MovimientoInversion.objects.filter(origen_recurrente=self.regla).count()

        # Segunda llamada no debe duplicar nada: ya no quedan meses pendientes.
        self.client.post(reverse('finanzas:aportacion_recurrente_generar', args=[self.inv.id, self.regla.id]))
        segunda_cuenta = MovimientoInversion.objects.filter(origen_recurrente=self.regla).count()
        self.assertEqual(primera_cuenta, segunda_cuenta)


class DepositoExcluidoDePatrimonioTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('inversor', password='x')
        self.client.force_login(self.user)

    def test_deposito_no_suma_en_totales_del_listado(self):
        # Activo de mercado normal: aporta 100, vale 120.
        _crear_inversion(self.user, 'ETF A', 'ETF', 10, 10, 12)
        # Depósito excluido: aporta 500, no debe sumar en los totales.
        dep = Inversion.objects.create(
            usuario=self.user, nombre='Depósito', tipo='DEPOSITO',
            plataforma='Banco',
        )
        MovimientoInversion.objects.create(
            inversion=dep, fecha=datetime.date(2026, 1, 1), tipo='COMPRA',
            cantidad=Decimal('500'), precio_unitario=Decimal('1'),
        )

        resp = self.client.get(reverse('finanzas:listar'))
        self.assertEqual(resp.status_code, 200)
        # El total de aportado del bloque de mercado no debe incluir el depósito.
        self.assertEqual(resp.context['total_aportado'], Decimal('100'))
        self.assertEqual(resp.context['deposit_total_aportado'], Decimal('500'))
        self.assertEqual(len(resp.context['deposit_data']), 1)
        self.assertEqual(len(resp.context['inv_data']), 1)

    def test_fondo_familiar_excluye_deposito(self):
        from core.models import Hogar
        from .models import FondoFamiliar

        hogar = Hogar.objects.create(nombre='Casa', creado_por=self.user)
        fondo = FondoFamiliar.objects.create(hogar=hogar, nombre='Cartera', tipo_fondo='inversion')
        inv = _crear_inversion(self.user, 'ETF A', 'ETF', 10, 10, 12)
        inv.fondo = fondo
        inv.save()

        dep = Inversion.objects.create(
            usuario=self.user, nombre='Depósito', tipo='DEPOSITO', fondo=fondo,
        )
        MovimientoInversion.objects.create(
            inversion=dep, fecha=datetime.date(2026, 1, 1), tipo='COMPRA',
            cantidad=Decimal('500'), precio_unitario=Decimal('1'),
        )

        self.assertEqual(fondo.total_aportado_cartera, Decimal('100'))


class DepositoMotorInteresTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('inversor', password='x')

    def _deposito(self, tipo_interes, frecuencia='anual', liquidacion=None):
        return Inversion.objects.create(
            usuario=self.user, nombre='Depósito', tipo='DEPOSITO',
            deposito_tipo_interes=Decimal(str(tipo_interes)),
            deposito_frecuencia=frecuencia, deposito_fecha_liquidacion=liquidacion,
        )

    def _aportacion(self, dep, fecha, importe, tipo='COMPRA'):
        return MovimientoInversion.objects.create(
            inversion=dep, fecha=fecha, tipo=tipo,
            cantidad=Decimal(str(importe)), precio_unitario=Decimal('1'),
        )

    def test_valor_con_interes_anual(self):
        dep = self._deposito('3', 'anual', liquidacion=datetime.date(2026, 1, 1))
        self._aportacion(dep, datetime.date(2024, 1, 1), 10000)
        valor, aportado = dep.deposito_valor_y_aportado()
        # 2 años (con año bisiesto) al 3% anual ≈ 10.609,86
        self.assertEqual(aportado, Decimal('10000.00'))
        self.assertTrue(Decimal('10609') < valor < Decimal('10611'))
        self.assertEqual(dep.deposito_fecha_apertura, datetime.date(2024, 1, 1))

    def test_valor_con_aportaciones_y_retiradas(self):
        dep = self._deposito('2', 'mensual', liquidacion=datetime.date(2026, 1, 1))
        self._aportacion(dep, datetime.date(2024, 1, 1), 10000)
        self._aportacion(dep, datetime.date(2025, 1, 1), 5000)
        self._aportacion(dep, datetime.date(2025, 7, 1), 2000, tipo='VENTA')  # retirada
        valor, aportado = dep.deposito_valor_y_aportado()
        self.assertEqual(aportado, Decimal('13000.00'))   # 10000 + 5000 - 2000
        self.assertTrue(valor > aportado)  # generó interés neto

    def test_interes_por_anio(self):
        dep = self._deposito('3', 'anual')
        self._aportacion(dep, datetime.date(2024, 1, 1), 10000)
        # El interés de 2025 ≈ 300 (3% sobre ~10.300)
        interes_2025 = dep.deposito_interes_anio(2025)
        self.assertTrue(Decimal('280') < interes_2025 < Decimal('330'))

    def test_liquidacion_congela_el_valor(self):
        dep = self._deposito('5', 'anual', liquidacion=datetime.date(2025, 1, 1))
        self._aportacion(dep, datetime.date(2024, 1, 1), 10000)
        # Sin liquidación crecería más; con liquidación en 2025 el valor a hoy = valor a 2025.
        valor_hoy, _ = dep.deposito_valor_y_aportado()
        valor_liq, _ = dep.deposito_valor_y_aportado(hasta=datetime.date(2030, 1, 1))
        self.assertEqual(valor_hoy, valor_liq)  # no acumula tras la liquidación


class InformeDepositosTests(TestCase):
    def setUp(self):
        from core.models import Hogar, UserProfile
        self.user = User.objects.create_user('inversor', password='x')
        self.hogar = Hogar.objects.create(nombre='Casa', creado_por=self.user)
        perfil, _ = UserProfile.objects.get_or_create(user=self.user)
        perfil.hogar = self.hogar
        perfil.save()

    def test_deposito_no_entra_en_plusvalias_y_suma_rendimiento(self):
        from .informe_hacienda import calcular_informe_ventas

        dep = Inversion.objects.create(
            usuario=self.user, nombre='Depósito Irene', tipo='DEPOSITO',
            deposito_tipo_interes=Decimal('4'), deposito_frecuencia='anual',
        )
        MovimientoInversion.objects.create(
            inversion=dep, fecha=datetime.date(2024, 1, 1), tipo='COMPRA',
            cantidad=Decimal('10000'), precio_unitario=Decimal('1'),
        )
        anio = datetime.date.today().year
        informe = calcular_informe_ventas(self.hogar, anio)
        # No aparece como venta/plusvalía
        self.assertEqual(informe['num_ventas'], 0)
        # Sí aparece como rendimiento de depósito y suma a la base del ahorro
        self.assertTrue(len(informe['depositos']) >= 0)  # depende del año
        self.assertGreaterEqual(informe['base_ahorro'], informe['interes_depositos'])


class DepositoContabilidadTests(TestCase):
    """Ajustes: la retirada total no debe dejar el interés negativo, y el saldo
    real indicado manda sobre el interés calculado."""
    def setUp(self):
        self.user = User.objects.create_user('inversor', password='x')

    def test_retirada_total_no_deja_interes_negativo(self):
        dep = Inversion.objects.create(
            usuario=self.user, nombre='D', tipo='DEPOSITO',
            deposito_tipo_interes=Decimal('2'), deposito_frecuencia='diaria')
        MovimientoInversion.objects.create(
            inversion=dep, fecha=datetime.date(2025, 1, 1), tipo='COMPRA',
            cantidad=Decimal('6000'), precio_unitario=Decimal('1'))
        valor_antes = dep.deposito_estado()['valor']
        # Retirar TODO (capital + interés)
        MovimientoInversion.objects.create(
            inversion=dep, fecha=datetime.date.today(), tipo='VENTA',
            cantidad=valor_antes, precio_unitario=Decimal('1'))
        estado = dep.deposito_estado()
        self.assertEqual(estado['valor'], Decimal('0.00'))
        self.assertEqual(estado['aportado'], Decimal('6000.00'))
        # El interés NO es negativo: es lo retirado (>6000) menos lo aportado.
        self.assertGreater(estado['interes'], Decimal('0'))
        self.assertEqual(estado['interes'], round(valor_antes - Decimal('6000'), 2))

    def test_saldo_real_manda_sobre_el_calculado(self):
        dep = Inversion.objects.create(
            usuario=self.user, nombre='D', tipo='DEPOSITO',
            deposito_tipo_interes=Decimal('2'), deposito_frecuencia='diaria',
            deposito_saldo_manual=Decimal('6080'), deposito_saldo_fecha=datetime.date.today())
        MovimientoInversion.objects.create(
            inversion=dep, fecha=datetime.date(2025, 1, 1), tipo='COMPRA',
            cantidad=Decimal('6000'), precio_unitario=Decimal('1'))
        estado = dep.deposito_estado()
        self.assertEqual(estado['valor'], Decimal('6080.00'))
        self.assertEqual(estado['interes'], Decimal('80.00'))


class DepositoEnEvolucionTests(TestCase):
    def setUp(self):
        from core.models import Hogar, UserProfile
        from .models import FondoFamiliar, SaldoRealFondo
        self.user = User.objects.create_user('inversor', password='x')
        self.hogar = Hogar.objects.create(nombre='Casa', creado_por=self.user)
        perfil, _ = UserProfile.objects.get_or_create(user=self.user)
        perfil.hogar = self.hogar
        perfil.save()
        # Un fondo común con saldo en enero para que el mes tenga datos.
        self.fondo = FondoFamiliar.objects.create(hogar=self.hogar, nombre='Común', tipo_fondo='comun')
        SaldoRealFondo.objects.create(fondo=self.fondo, año=datetime.date.today().year, mes=1, saldo=Decimal('1000'))

    def test_deposito_suma_al_patrimonio_automaticamente(self):
        from .views_evolucion import valor_depositos_hogar
        dep = Inversion.objects.create(
            usuario=self.user, nombre='Depo', tipo='DEPOSITO',
            deposito_tipo_interes=Decimal('0'), deposito_frecuencia='anual')
        MovimientoInversion.objects.create(
            inversion=dep, fecha=datetime.date(datetime.date.today().year, 1, 1),
            tipo='COMPRA', cantidad=Decimal('5000'), precio_unitario=Decimal('1'))
        total = valor_depositos_hogar(self.hogar, datetime.date.today())
        self.assertEqual(total, Decimal('5000.00'))


class DepositoEnTablaEvolucionTests(TestCase):
    """El depósito debe aparecer como celda propia en la tabla de Evolución y
    sumar al patrimonio del mes, sin que el usuario registre su saldo."""
    def setUp(self):
        from core.models import Hogar, UserProfile
        from .models import FondoFamiliar, SaldoRealFondo
        self.user = User.objects.create_user('inversor', password='x')
        self.hogar = Hogar.objects.create(nombre='Casa', creado_por=self.user)
        perfil, _ = UserProfile.objects.get_or_create(user=self.user)
        perfil.hogar = self.hogar
        perfil.save()
        self.year = datetime.date.today().year
        fondo = FondoFamiliar.objects.create(hogar=self.hogar, nombre='Común', tipo_fondo='comun')
        SaldoRealFondo.objects.create(fondo=fondo, año=self.year, mes=1, saldo=Decimal('10000'))

    def test_celda_de_deposito_y_patrimonio(self):
        from .views_evolucion import _construir_tabla, _flujos_por_mes
        dep = Inversion.objects.create(
            usuario=self.user, nombre='Depo', tipo='DEPOSITO',
            deposito_tipo_interes=Decimal('0'), deposito_frecuencia='anual')
        MovimientoInversion.objects.create(
            inversion=dep, fecha=datetime.date(self.year, 1, 10), tipo='COMPRA',
            cantidad=Decimal('6000'), precio_unitario=Decimal('1'))

        _, filas = _construir_tabla(self.hogar, self.year, _flujos_por_mes(self.hogar, self.year))
        fila_enero = next(f for f in filas if f['mes'] == 1)
        # El depósito aparece como celda propia con su valor
        self.assertEqual(len(fila_enero['celdas_depositos']), 1)
        self.assertEqual(fila_enero['celdas_depositos'][0]['valor'], Decimal('6000.00'))
        # Un depósito es dinero disponible: suma en LIQUIDEZ (y por tanto en patrimonio)
        self.assertEqual(fila_enero['liquidez'], Decimal('16000.00'))
        self.assertEqual(fila_enero['patrimonio'], Decimal('16000.00'))

    def test_deposito_aparece_en_distribucion(self):
        dep = Inversion.objects.create(
            usuario=self.user, nombre='DepoDistrib', tipo='DEPOSITO',
            deposito_tipo_interes=Decimal('0'), deposito_frecuencia='anual')
        MovimientoInversion.objects.create(
            inversion=dep, fecha=datetime.date(self.year, 1, 10), tipo='COMPRA',
            cantidad=Decimal('3000'), precio_unitario=Decimal('1'))
        self.client.force_login(self.user)
        resp = self.client.get(reverse('finanzas:vista_distribucion'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'DepoDistrib')
        self.assertEqual(resp.context['depositos_total'], Decimal('3000.00'))


class DepositoVinculadoAFondoTests(TestCase):
    """Un depósito vinculado a un fondo aporta su valor a través de ese fondo
    (para heredar sus reglas/transferencias) sin contarse dos veces."""
    def setUp(self):
        from core.models import Hogar, UserProfile
        self.user = User.objects.create_user('inversor', password='x')
        self.hogar = Hogar.objects.create(nombre='Casa', creado_por=self.user)
        perfil, _ = UserProfile.objects.get_or_create(user=self.user)
        perfil.hogar = self.hogar
        perfil.save()
        self.year = datetime.date.today().year

    def test_deposito_vinculado_no_duplica_y_manda_sobre_el_saldo_manual(self):
        from .models import FondoFamiliar, SaldoRealFondo
        from .views_evolucion import _construir_tabla, _flujos_por_mes

        fondo = FondoFamiliar.objects.create(hogar=self.hogar, nombre='Depósito Revolut', tipo_fondo='ahorro')
        # Saldo manual antiguo que debe quedar ignorado
        SaldoRealFondo.objects.create(fondo=fondo, año=self.year, mes=1, saldo=Decimal('999'))
        dep = Inversion.objects.create(
            usuario=self.user, nombre='Depo', tipo='DEPOSITO', fondo=fondo,
            deposito_tipo_interes=Decimal('0'), deposito_frecuencia='anual')
        MovimientoInversion.objects.create(
            inversion=dep, fecha=datetime.date(self.year, 1, 5), tipo='COMPRA',
            cantidad=Decimal('6000'), precio_unitario=Decimal('1'))

        _, filas = _construir_tabla(self.hogar, self.year, _flujos_por_mes(self.hogar, self.year))
        enero = next(f for f in filas if f['mes'] == 1)
        celda = next(c for c in enero['celdas'] if c['fondo'].id == fondo.id)
        self.assertTrue(celda['auto_deposito'])
        self.assertEqual(celda['saldo_valor'], Decimal('6000.00'))
        # No hay tarjeta suelta para este depósito (ya va dentro del fondo)
        self.assertEqual(len(enero['celdas_depositos']), 0)
        # Liquidez = solo el depósito (6000), no 6999
        self.assertEqual(enero['liquidez'], Decimal('6000.00'))

    def test_deposito_sin_fondo_suma_en_liquidez_como_tarjeta(self):
        from .views_evolucion import _construir_tabla, _flujos_por_mes
        dep = Inversion.objects.create(
            usuario=self.user, nombre='Suelto', tipo='DEPOSITO',
            deposito_tipo_interes=Decimal('0'), deposito_frecuencia='anual')
        MovimientoInversion.objects.create(
            inversion=dep, fecha=datetime.date(self.year, 1, 5), tipo='COMPRA',
            cantidad=Decimal('2000'), precio_unitario=Decimal('1'))

        _, filas = _construir_tabla(self.hogar, self.year, _flujos_por_mes(self.hogar, self.year))
        enero = next(f for f in filas if f['mes'] == 1)
        self.assertEqual(len(enero['celdas_depositos']), 1)
        self.assertEqual(enero['liquidez'], Decimal('2000.00'))


class FondoPropietarioTests(TestCase):
    """El fondo tiene titular, y de él sale quién declara los rendimientos del
    depósito vinculado en el Informe Hacienda."""
    def setUp(self):
        from core.models import Hogar, UserProfile
        self.user = User.objects.create_user('adrian', password='x', first_name='Adrián')
        self.irene = User.objects.create_user('irene', password='x', first_name='Irene')
        self.hogar = Hogar.objects.create(nombre='Casa', creado_por=self.user)
        for u in (self.user, self.irene):
            perfil, _ = UserProfile.objects.get_or_create(user=u)
            perfil.hogar = self.hogar
            perfil.save()
        self.year = datetime.date.today().year
        self.client.force_login(self.user)

    def _deposito(self, fondo=None):
        dep = Inversion.objects.create(
            usuario=self.user, nombre='Depo', tipo='DEPOSITO', fondo=fondo,
            deposito_tipo_interes=Decimal('4'), deposito_frecuencia='anual')
        MovimientoInversion.objects.create(
            inversion=dep, fecha=datetime.date(self.year - 1, 1, 1), tipo='COMPRA',
            cantidad=Decimal('10000'), precio_unitario=Decimal('1'))
        return dep

    def test_titular_nombre_por_defecto_es_compartido(self):
        from .models import FondoFamiliar
        fondo = FondoFamiliar.objects.create(hogar=self.hogar, nombre='Conjunta', tipo_fondo='comun')
        self.assertEqual(fondo.titular_nombre, 'Compartido')
        fondo.propietario = self.irene
        fondo.save()
        self.assertEqual(fondo.titular_nombre, 'Irene')

    def test_informe_usa_el_propietario_del_fondo_como_titular(self):
        from .models import FondoFamiliar
        from .informe_hacienda import calcular_informe_depositos
        fondo = FondoFamiliar.objects.create(
            hogar=self.hogar, nombre='Depósito Irene', tipo_fondo='ahorro', propietario=self.irene)
        self._deposito(fondo=fondo)
        informe = calcular_informe_depositos(self.hogar, self.year)
        self.assertEqual(len(informe['depositos']), 1)
        # El depósito lo registró Adrián, pero el fondo es de Irene → declara Irene
        self.assertEqual(informe['depositos'][0]['titular'], 'Irene')
        self.assertEqual(informe['depositos'][0]['fondo'], 'Depósito Irene')

    def test_sin_propietario_declara_quien_registro_el_deposito(self):
        from .informe_hacienda import calcular_informe_depositos
        self._deposito(fondo=None)
        informe = calcular_informe_depositos(self.hogar, self.year)
        self.assertEqual(informe['depositos'][0]['titular'], 'Adrián')

    def test_guardar_propietario_desde_la_vista(self):
        from .models import FondoFamiliar
        self.client.post(reverse('finanzas:crear_fondo'), {
            'nombre': 'Ahorro Irene', 'tipo_fondo': 'ahorro',
            'color': '#2d6a4f', 'cuenta_asociada': '', 'propietario_id': str(self.irene.id),
        })
        fondo = FondoFamiliar.objects.get(hogar=self.hogar, nombre='Ahorro Irene')
        self.assertEqual(fondo.propietario_id, self.irene.id)

        # Y se puede volver a dejar compartido
        self.client.post(reverse('finanzas:editar_fondo', args=[fondo.id]), {
            'nombre': 'Ahorro Irene', 'tipo_fondo': 'ahorro',
            'color': '#2d6a4f', 'cuenta_asociada': '', 'propietario_id': '',
        })
        fondo.refresh_from_db()
        self.assertIsNone(fondo.propietario_id)


class DepositoSaldoRealYRetiradasTests(TestCase):
    """El saldo real es una FOTO en su fecha, no un valor fijo: las retiradas y
    aportaciones posteriores se aplican sobre él, y el rendimiento se mantiene."""
    def setUp(self):
        self.user = User.objects.create_user('inversor', password='x')
        self.hoy = datetime.date.today()

    def _dep(self, saldo=None, dias_apertura=200):
        dep = Inversion.objects.create(
            usuario=self.user, nombre='D', tipo='DEPOSITO',
            deposito_tipo_interes=Decimal('2.07'), deposito_frecuencia='diaria',
            deposito_saldo_manual=saldo,
            deposito_saldo_fecha=self.hoy if saldo is not None else None)
        MovimientoInversion.objects.create(
            inversion=dep, fecha=self.hoy - datetime.timedelta(days=dias_apertura),
            tipo='COMPRA', cantidad=Decimal('6000'), precio_unitario=Decimal('1'))
        return dep

    def _mov(self, dep, tipo, importe, fecha=None):
        MovimientoInversion.objects.create(
            inversion=dep, fecha=fecha or self.hoy, tipo=tipo,
            cantidad=Decimal(str(importe)), precio_unitario=Decimal('1'))

    def test_saldo_real_sin_retiradas(self):
        e = self._dep(Decimal('6066')).deposito_estado()
        self.assertEqual(e['valor'], Decimal('6066.00'))
        self.assertEqual(e['interes'], Decimal('66.00'))
        self.assertEqual(e['interes_pct'], Decimal('1.10'))

    def test_retirada_total_deja_saldo_cero_y_conserva_rendimiento(self):
        dep = self._dep(Decimal('6066'))
        self._mov(dep, 'VENTA', '6066')
        e = dep.deposito_estado()
        self.assertEqual(e['valor'], Decimal('0'))          # no queda nada
        self.assertEqual(e['retirado'], Decimal('6066.00'))
        self.assertEqual(e['interes'], Decimal('66.00'))    # el rendimiento se mantiene
        self.assertEqual(e['interes_pct'], Decimal('1.10'))

    def test_retirada_parcial_resta_del_saldo(self):
        dep = self._dep(Decimal('6066'))
        self._mov(dep, 'VENTA', '1000')
        e = dep.deposito_estado()
        self.assertEqual(e['valor'], Decimal('5066.00'))    # 6066 − 1000
        self.assertEqual(e['interes'], Decimal('66.00'))

    def test_aportacion_posterior_al_saldo_real_suma(self):
        dep = self._dep(Decimal('6066'))
        self._mov(dep, 'COMPRA', '500')
        e = dep.deposito_estado()
        self.assertEqual(e['valor'], Decimal('6566.00'))    # 6066 + 500
        self.assertEqual(e['aportado'], Decimal('6500.00'))
        self.assertEqual(e['interes'], Decimal('66.00'))

    def test_saldo_real_el_dia_de_apertura_no_duplica(self):
        dep = Inversion.objects.create(
            usuario=self.user, nombre='F', tipo='DEPOSITO',
            deposito_tipo_interes=Decimal('0'), deposito_frecuencia='anual',
            deposito_saldo_manual=Decimal('6000'), deposito_saldo_fecha=self.hoy)
        self._mov(dep, 'COMPRA', '6000')
        e = dep.deposito_estado()
        self.assertEqual(e['valor'], Decimal('6000.00'))
        self.assertEqual(e['interes'], Decimal('0.00'))

    def test_rendimiento_en_pct_sin_saldo_real(self):
        dep = self._dep()  # interés teórico
        e = dep.deposito_estado()
        self.assertGreater(e['interes'], Decimal('0'))
        self.assertIsNotNone(e['interes_pct'])
        self.assertEqual(e['interes_pct'], round(e['interes'] / e['aportado'] * 100, 2))


class EvolucionUsaSaldoRealTests(TestCase):
    """Evolución registra lo REAL: el mes en curso se valora a día de hoy, no
    proyectando el interés hasta fin de mes."""
    def setUp(self):
        from core.models import Hogar, UserProfile
        from .models import FondoFamiliar
        self.user = User.objects.create_user('inversor', password='x')
        self.hogar = Hogar.objects.create(nombre='Casa', creado_por=self.user)
        perfil, _ = UserProfile.objects.get_or_create(user=self.user)
        perfil.hogar = self.hogar
        perfil.save()
        self.hoy = datetime.date.today()
        self.fondo = FondoFamiliar.objects.create(
            hogar=self.hogar, nombre='Depo Revolut', tipo_fondo='ahorro')

    def test_mes_en_curso_muestra_el_saldo_real_no_el_proyectado(self):
        from .views_evolucion import _construir_tabla, _flujos_por_mes
        dep = Inversion.objects.create(
            usuario=self.user, nombre='Depo', tipo='DEPOSITO', fondo=self.fondo,
            deposito_tipo_interes=Decimal('2.07'), deposito_frecuencia='diaria',
            deposito_saldo_manual=Decimal('6066'), deposito_saldo_fecha=self.hoy)
        MovimientoInversion.objects.create(
            inversion=dep, fecha=self.hoy - datetime.timedelta(days=200),
            tipo='COMPRA', cantidad=Decimal('6000'), precio_unitario=Decimal('1'))

        _, filas = _construir_tabla(self.hogar, self.hoy.year,
                                    _flujos_por_mes(self.hogar, self.hoy.year))
        actual = next(f for f in filas if f['mes'] == self.hoy.month)
        celda = next(c for c in actual['celdas'] if c['fondo'].id == self.fondo.id)
        # Exactamente el saldo real indicado, sin interés proyectado a fin de mes
        self.assertEqual(celda['saldo_valor'], Decimal('6066.00'))
        self.assertEqual(actual['liquidez'], Decimal('6066.00'))

    def test_fecha_corte_no_va_al_futuro(self):
        from .views_evolucion import _fecha_corte_mes
        # Mes en curso → hoy
        self.assertEqual(_fecha_corte_mes(self.hoy.year, self.hoy.month), self.hoy)
        # Mes pasado → su último día (histórico real)
        if self.hoy.month > 1:
            self.assertEqual(_fecha_corte_mes(self.hoy.year, 1), datetime.date(self.hoy.year, 1, 31))


class CierreMensualEvolucionTests(TestCase):
    """Evolución es el registro de lo que pasó: una vez cerrado el mes, sus
    cifras quedan fijadas. Cambiar hoy el sueldo solo puede mover el mes en
    curso (y lo que venga después), nunca el pasado."""

    def setUp(self):
        from core.models import Hogar, UserProfile
        from .models import FondoFamiliar, FuenteIngreso, SaldoRealFondo

        self.user = User.objects.create_user('adri', password='x')
        self.hogar = Hogar.objects.create(nombre='Casa', creado_por=self.user)
        perfil, _ = UserProfile.objects.get_or_create(user=self.user)
        perfil.hogar = self.hogar
        perfil.save()

        self.hoy = datetime.date.today()
        self.año = self.hoy.year
        # El test necesita al menos un mes cerrado en el año en curso.
        self.mes_cerrado = self.hoy.month - 1
        self.fondo = FondoFamiliar.objects.create(
            hogar=self.hogar, nombre='Común', tipo_fondo='comun')
        SaldoRealFondo.objects.create(
            fondo=self.fondo, año=self.año, mes=self.hoy.month, saldo=Decimal('1000'))

        self.fuente = FuenteIngreso.objects.create(
            hogar=self.hogar, usuario=self.user, nombre='Nómina', tipo='fijo',
            modo_entrada='anual', importe_declarado=Decimal('24000'),
            es_bruto=False, num_pagas=12, activo=True,
        )

    def _ingresos_por_mes(self):
        from .views_evolucion import _flujos_por_mes
        flujos = _flujos_por_mes(self.hogar, self.año)
        return {m: flujos[m]['ingreso_base_hogar'] for m in flujos}

    def _subir_sueldo(self, importe):
        self.fuente.importe_declarado = Decimal(importe)
        self.fuente.save()

    def test_subir_el_sueldo_no_reescribe_los_meses_cerrados(self):
        if not self.mes_cerrado:
            self.skipTest('En enero no hay ningún mes cerrado de este año')

        antes = self._ingresos_por_mes()
        self._subir_sueldo('48000')
        despues = self._ingresos_por_mes()

        for mes in range(1, self.mes_cerrado + 1):
            self.assertEqual(despues[mes], antes[mes],
                             f'El mes cerrado {mes} ha cambiado al subir el sueldo')

    def test_el_mes_en_curso_y_los_futuros_si_se_actualizan(self):
        antes = self._ingresos_por_mes()
        self._subir_sueldo('48000')
        despues = self._ingresos_por_mes()

        self.assertGreater(despues[self.hoy.month], antes[self.hoy.month])
        if self.hoy.month < 12:
            self.assertGreater(despues[12], antes[12])

    def test_el_cierre_se_hace_con_los_valores_de_antes_del_cambio(self):
        """La foto se toma al guardar el cambio, no después: guarda lo que
        había, no lo nuevo. Sin esto, quien cambia el sueldo sin haber abierto
        Evolución antes congelaría el pasado ya corrompido."""
        from .models import CierreMensual
        if not self.mes_cerrado:
            self.skipTest('En enero no hay ningún mes cerrado de este año')

        self.assertFalse(CierreMensual.objects.filter(hogar=self.hogar).exists())
        antes = self._ingresos_por_mes()[self.mes_cerrado]

        self._subir_sueldo('48000')

        cierre = CierreMensual.objects.get(hogar=self.hogar, año=self.año, mes=self.mes_cerrado)
        self.assertEqual(cierre.ingreso, antes)

    def test_el_mes_en_curso_no_se_congela(self):
        from .models import CierreMensual
        self._subir_sueldo('48000')
        self.assertFalse(
            CierreMensual.objects.filter(
                hogar=self.hogar, año=self.año, mes=self.hoy.month).exists())

    def test_un_gasto_nuevo_no_cambia_el_ahorro_esperado_del_pasado(self):
        from .models import CategoriaGasto, PartidaGasto
        from .views_evolucion import _flujos_por_mes
        if not self.mes_cerrado:
            self.skipTest('En enero no hay ningún mes cerrado de este año')

        antes = _flujos_por_mes(self.hogar, self.año)[self.mes_cerrado]['total_gastos_all']

        cat = CategoriaGasto.objects.create(hogar=self.hogar, nombre='Vivienda', tipo='fijo')
        PartidaGasto.objects.create(
            hogar=self.hogar, categoria=cat, nombre='Hipoteca',
            importe=Decimal('900'), periodicidad='mensual', activo=True)

        despues = _flujos_por_mes(self.hogar, self.año)
        self.assertEqual(despues[self.mes_cerrado]['total_gastos_all'], antes)
        self.assertGreater(despues[self.hoy.month]['total_gastos_all'], antes)

    def test_corregir_un_ajuste_de_un_mes_cerrado_si_rehace_su_cierre(self):
        """Un ajuste de ingreso es un dato del propio mes: si el usuario
        corrige lo que cobró en un mes cerrado, la foto se rehace."""
        from .models import AjusteIngresoMensual
        if not self.mes_cerrado:
            self.skipTest('En enero no hay ningún mes cerrado de este año')

        antes = self._ingresos_por_mes()[self.mes_cerrado]

        AjusteIngresoMensual.objects.create(
            fuente=self.fuente, año=self.año, mes=self.mes_cerrado,
            importe_real=antes + Decimal('500'), nota='Bonus',
        )

        despues = self._ingresos_por_mes()[self.mes_cerrado]
        self.assertEqual(despues, antes + Decimal('500'))

    def test_los_meses_cerrados_quedan_registrados_al_abrir_evolucion(self):
        from .models import CierreMensual
        if not self.mes_cerrado:
            self.skipTest('En enero no hay ningún mes cerrado de este año')

        self.client.force_login(self.user)
        resp = self.client.get(reverse('finanzas:vista_evolucion'))
        self.assertEqual(resp.status_code, 200)

        registrados = set(
            CierreMensual.objects.filter(hogar=self.hogar, año=self.año)
            .values_list('mes', flat=True))
        self.assertEqual(registrados, set(range(1, self.hoy.month)))


class IngresoFueraDeLaDistribucionTests(TestCase):
    """Un ingreso puede estar declarado (cuenta para el total anual y para el
    IRPF) y aun así quedar fuera del reparto mensual del hogar: el alquiler de
    un piso, por ejemplo, que existe pero se gestiona aparte."""

    def setUp(self):
        from core.models import Hogar, UserProfile
        from .models import FuenteIngreso

        self.user = User.objects.create_user('irene', password='x')
        self.hogar = Hogar.objects.create(nombre='Casa', creado_por=self.user)
        perfil, _ = UserProfile.objects.get_or_create(user=self.user)
        perfil.hogar = self.hogar
        perfil.save()

        self.nomina = FuenteIngreso.objects.create(
            hogar=self.hogar, usuario=self.user, nombre='Nómina', tipo='fijo',
            modo_entrada='anual', importe_declarado=Decimal('24000'),
            es_bruto=False, num_pagas=12, activo=True,
        )
        self.alquiler = FuenteIngreso.objects.create(
            hogar=self.hogar, usuario=self.user, nombre='Alquiler piso', tipo='fijo',
            modo_entrada='anual', importe_declarado=Decimal('12000'),
            es_bruto=False, num_pagas=12, activo=True,
        )

    def _ingreso_distribuido(self):
        from .distribucion import calcular_flujos
        return calcular_flujos(self.hogar)['ingreso_base_hogar']

    def test_por_defecto_todo_ingreso_entra_en_la_distribucion(self):
        self.assertTrue(self.alquiler.incluir_en_distribucion)
        self.assertEqual(self._ingreso_distribuido(), Decimal('3000'))  # 2000 + 1000

    def test_excluir_un_ingreso_lo_saca_del_reparto_del_mes(self):
        self.alquiler.incluir_en_distribucion = False
        self.alquiler.save()
        self.assertEqual(self._ingreso_distribuido(), Decimal('2000'))

    def test_el_ingreso_excluido_sigue_declarado(self):
        """No desaparece: sigue en la lista de ingresos y en el total anual."""
        self.alquiler.incluir_en_distribucion = False
        self.alquiler.save()

        self.client.force_login(self.user)
        resp = self.client.get(reverse('finanzas:listar_ingresos'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Alquiler piso')
        self.assertContains(resp, 'fuera del reparto')
        # El total anual del hogar sigue contando los 12.000 del alquiler.
        self.assertEqual(resp.context['total_anual_hogar'], Decimal('36000'))
        self.assertEqual(resp.context['total_fuera_reparto_hogar'], Decimal('1000'))
        self.assertEqual(resp.context['total_reparto_hogar'], Decimal('2000'))

    def test_no_cuenta_para_repartir_los_gastos_comunes(self):
        """La proporción con la que cada miembro cubre los gastos del hogar sale
        del ingreso que sí se reparte."""
        from core.models import Hogar
        from .models import FuenteIngreso, CategoriaGasto, PartidaGasto
        from .distribucion import calcular_flujos

        from core.models import UserProfile
        otro = User.objects.create_user('adri', password='x')
        perfil_otro, _ = UserProfile.objects.get_or_create(user=otro)
        perfil_otro.hogar = self.hogar
        perfil_otro.save()
        FuenteIngreso.objects.create(
            hogar=self.hogar, usuario=otro, nombre='Nómina', tipo='fijo',
            modo_entrada='anual', importe_declarado=Decimal('24000'),
            es_bruto=False, num_pagas=12, activo=True,
        )
        cat = CategoriaGasto.objects.create(hogar=self.hogar, nombre='Vivienda', tipo='fijo')
        PartidaGasto.objects.create(
            hogar=self.hogar, categoria=cat, nombre='Alquiler',
            importe=Decimal('1000'), periodicidad='mensual', activo=True)

        def proporcion_de_irene():
            d = calcular_flujos(self.hogar)
            dm = next(x for x in d['datos_miembros']
                      if x['miembro'].user_id == self.user.id)
            return dm['proporcion']

        # Con el alquiler dentro, Irene ingresa 3000 de 5000 → aporta más.
        prop_con = proporcion_de_irene()

        self.alquiler.incluir_en_distribucion = False
        self.alquiler.save()

        prop_sin = proporcion_de_irene()

        self.assertEqual(prop_con, Decimal('60.0'))   # 3000 de 5000
        self.assertEqual(prop_sin, Decimal('50.0'))   # 2000 de 4000

    def test_el_formulario_guarda_la_casilla(self):
        self.client.force_login(self.user)
        resp = self.client.post(
            reverse('finanzas:editar_ingreso', args=[self.alquiler.id]), {
                'usuario_id': self.user.id,
                'nombre': 'Alquiler piso',
                'tipo': 'fijo',
                'modo_entrada': 'anual',
                'importe_declarado': '12000',
                'es_bruto': 'false',
                'pais_fiscal': 'ES',
                'num_pagas': '12',
                'meses_pagas_extras': '6,12',
                'periodicidad': 'mensual',
                'porcentaje_variabilidad': '0',
                'incluir_en_mensual': 'on',
                # sin 'incluir_en_distribucion' → fuera del reparto
            })
        self.assertEqual(resp.status_code, 302)
        self.alquiler.refresh_from_db()
        self.assertFalse(self.alquiler.incluir_en_distribucion)
        self.assertEqual(self._ingreso_distribuido(), Decimal('2000'))


class MesCerradoEnTodaLaAppTests(TestCase):
    """La regla del mes cerrado no es solo de Evolución: cualquier pantalla que
    pueda enseñar un mes pasado tiene que enseñar lo que quedó registrado."""

    def setUp(self):
        from core.models import Hogar, UserProfile
        from .models import CategoriaGasto, FuenteIngreso, PartidaGasto

        self.user = User.objects.create_user('adri', password='x')
        self.hogar = Hogar.objects.create(nombre='Casa', creado_por=self.user)
        perfil, _ = UserProfile.objects.get_or_create(user=self.user)
        perfil.hogar = self.hogar
        perfil.save()
        self.client.force_login(self.user)

        self.hoy = datetime.date.today()
        self.año = self.hoy.year
        self.mes_cerrado = self.hoy.month - 1

        FuenteIngreso.objects.create(
            hogar=self.hogar, usuario=self.user, nombre='Nómina', tipo='fijo',
            modo_entrada='anual', importe_declarado=Decimal('24000'),
            es_bruto=False, num_pagas=12, activo=True,
        )
        cat = CategoriaGasto.objects.create(hogar=self.hogar, nombre='Anuales', tipo='anual')
        self.gasto = PartidaGasto.objects.create(
            hogar=self.hogar, categoria=cat, nombre='Seguros',
            importe=Decimal('1200'), periodicidad='anual', mes_pago=3, activo=True)

    def _subir_gasto(self):
        self.gasto.importe = Decimal('6000')
        self.gasto.save()

    def test_distribucion_de_un_mes_cerrado_no_se_recalcula(self):
        if not self.mes_cerrado:
            self.skipTest('En enero no hay ningún mes cerrado de este año')

        url = reverse('finanzas:vista_distribucion')
        antes = self.client.get(
            f'{url}?mes={self.mes_cerrado}&anio={self.año}').context['d']['total_gastos_all']

        self._subir_gasto()

        despues = self.client.get(
            f'{url}?mes={self.mes_cerrado}&anio={self.año}').context['d']
        self.assertEqual(despues['total_gastos_all'], antes)
        self.assertTrue(despues['mes_cerrado'])

    def test_distribucion_del_mes_en_curso_si_se_actualiza(self):
        url = reverse('finanzas:vista_distribucion')
        antes = self.client.get(
            f'{url}?mes={self.hoy.month}&anio={self.año}').context['d']['total_gastos_all']

        self._subir_gasto()

        despues = self.client.get(
            f'{url}?mes={self.hoy.month}&anio={self.año}').context['d']
        self.assertGreater(despues['total_gastos_all'], antes)
        self.assertFalse(despues['mes_cerrado'])

    def test_el_resumen_anual_tampoco_reescribe_los_meses_cerrados(self):
        if not self.mes_cerrado:
            self.skipTest('En enero no hay ningún mes cerrado de este año')

        url = reverse('finanzas:resumen_anual')
        antes = {m['mes']: m['gastos']
                 for m in self.client.get(f'{url}?anio={self.año}').context['resumen']['meses']}

        self._subir_gasto()

        despues = {m['mes']: m['gastos']
                   for m in self.client.get(f'{url}?anio={self.año}').context['resumen']['meses']}

        for mes in range(1, self.mes_cerrado + 1):
            self.assertEqual(despues[mes], antes[mes], f'El mes cerrado {mes} ha cambiado')
        self.assertGreater(despues[self.hoy.month], antes[self.hoy.month])

    def test_los_porcentajes_del_mes_cerrado_cuadran_con_sus_cifras(self):
        """Si el total se congela pero la tasa de ahorro se recalcula en vivo,
        la pantalla se contradice a sí misma."""
        if not self.mes_cerrado:
            self.skipTest('En enero no hay ningún mes cerrado de este año')

        self._subir_gasto()

        d = self.client.get(
            f'{reverse("finanzas:vista_distribucion")}?mes={self.mes_cerrado}&anio={self.año}'
        ).context['d']
        esperado = round(
            (d['ingreso_base_hogar'] - d['total_gastos_all']) / d['ingreso_base_hogar'] * 100, 1)
        self.assertEqual(d['tasa_ahorro'], esperado)

    def test_evolucion_y_distribucion_dan_la_misma_cifra_del_mes_cerrado(self):
        from .views_evolucion import _flujos_por_mes
        if not self.mes_cerrado:
            self.skipTest('En enero no hay ningún mes cerrado de este año')

        self._subir_gasto()

        evo = _flujos_por_mes(self.hogar, self.año)[self.mes_cerrado]
        dist = self.client.get(
            f'{reverse("finanzas:vista_distribucion")}?mes={self.mes_cerrado}&anio={self.año}'
        ).context['d']
        self.assertEqual(evo['ingreso_base_hogar'], dist['ingreso_base_hogar'])
        self.assertEqual(evo['total_gastos_all'], dist['total_gastos_all'])
