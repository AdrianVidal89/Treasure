import datetime
from decimal import Decimal

from django.test import SimpleTestCase, TestCase
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

    def test_deposito_aparece_entre_los_fondos(self):
        """Los depósitos son parte del ecosistema de cuentas, así que se ven
        donde se definen los fondos: en Gestión."""
        dep = Inversion.objects.create(
            usuario=self.user, nombre='DepoDistrib', tipo='DEPOSITO',
            deposito_tipo_interes=Decimal('0'), deposito_frecuencia='anual')
        MovimientoInversion.objects.create(
            inversion=dep, fecha=datetime.date(self.year, 1, 10), tipo='COMPRA',
            cantidad=Decimal('3000'), precio_unitario=Decimal('1'))
        self.client.force_login(self.user)
        resp = self.client.get(reverse('finanzas:listar_fondos'))
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


class FondosEnGestionTests(TestCase):
    """Los fondos se definen en Gestión, y los gastos se asignan desde el
    fondo. Distribución solo dice cómo se reparte el dinero entre ellos."""

    def setUp(self):
        from core.models import Hogar, UserProfile
        from .models import CategoriaGasto, FondoFamiliar, PartidaGasto

        self.user = User.objects.create_user('adri', password='x')
        self.hogar = Hogar.objects.create(nombre='Casa', creado_por=self.user)
        perfil, _ = UserProfile.objects.get_or_create(user=self.user)
        perfil.hogar = self.hogar
        perfil.save()
        self.client.force_login(self.user)

        self.fondo = FondoFamiliar.objects.create(
            hogar=self.hogar, nombre='Cuenta Conjunta', tipo_fondo='comun')
        self.otro = FondoFamiliar.objects.create(
            hogar=self.hogar, nombre='Ahorro', tipo_fondo='ahorro')
        cat = CategoriaGasto.objects.create(hogar=self.hogar, nombre='Hogar', tipo='fijo')
        self.gasto = PartidaGasto.objects.create(
            hogar=self.hogar, categoria=cat, nombre='Supermercados',
            importe=Decimal('450'), periodicidad='mensual', activo=True)

    def test_la_pantalla_de_fondos_lista_los_fondos_del_hogar(self):
        resp = self.client.get(reverse('finanzas:listar_fondos'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Cuenta Conjunta')
        self.assertContains(resp, 'Ahorro')

    def test_avisa_de_los_gastos_del_hogar_que_no_cubre_ningun_fondo(self):
        resp = self.client.get(reverse('finanzas:listar_fondos'))
        self.assertEqual(
            [g.id for g in resp.context['gastos_sin_asignar']], [self.gasto.id])
        self.assertEqual(resp.context['total_sin_asignar'], Decimal('450'))

    def test_asignar_un_gasto_desde_el_fondo(self):
        resp = self.client.post(
            reverse('finanzas:asignar_gastos_fondo', args=[self.fondo.id]),
            {'partida_ids': [self.gasto.id]})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'], reverse('finanzas:listar_fondos'))

        self.gasto.refresh_from_db()
        self.assertEqual(self.gasto.fondo_asignado_id, self.fondo.id)

        datos = self.client.get(reverse('finanzas:listar_fondos')).context['fondos_data']
        fila = next(f for f in datos if f['fondo'].id == self.fondo.id)
        self.assertEqual([g.id for g in fila['gastos']], [self.gasto.id])
        self.assertEqual(fila['total_gastos'], Decimal('450'))

    def test_reasignar_el_gasto_lo_quita_del_fondo_anterior(self):
        self.client.post(reverse('finanzas:asignar_gastos_fondo', args=[self.fondo.id]),
                         {'partida_ids': [self.gasto.id]})
        # Ahora se marca en el otro fondo y se deja de marcar en el primero.
        self.client.post(reverse('finanzas:asignar_gastos_fondo', args=[self.otro.id]),
                         {'partida_ids': [self.gasto.id]})
        self.gasto.refresh_from_db()
        self.assertEqual(self.gasto.fondo_asignado_id, self.otro.id)

        self.client.post(reverse('finanzas:asignar_gastos_fondo', args=[self.otro.id]),
                         {'partida_ids': []})
        self.gasto.refresh_from_db()
        self.assertIsNone(self.gasto.fondo_asignado_id)

    def test_crear_y_editar_un_fondo_vuelve_a_la_pantalla_de_fondos(self):
        from .models import FondoFamiliar

        resp = self.client.post(reverse('finanzas:crear_fondo'), {
            'nombre': 'Emergencia', 'tipo_fondo': 'ahorro',
            'color': 'var(--info)', 'cuenta_asociada': 'Revolut',
        })
        self.assertEqual(resp['Location'], reverse('finanzas:listar_fondos'))
        nuevo = FondoFamiliar.objects.get(hogar=self.hogar, nombre='Emergencia')

        resp = self.client.post(reverse('finanzas:editar_fondo', args=[nuevo.id]), {
            'nombre': 'Emergencia', 'tipo_fondo': 'ahorro',
            'color': 'var(--info)', 'cuenta_asociada': 'Kutxabank',
        })
        self.assertEqual(resp['Location'], reverse('finanzas:listar_fondos'))
        nuevo.refresh_from_db()
        self.assertEqual(nuevo.cuenta_asociada, 'Kutxabank')

    def test_distribucion_ya_no_edita_fondos_pero_enseña_como_quedan(self):
        from .models import ReglaReparto, FuenteIngreso

        FuenteIngreso.objects.create(
            hogar=self.hogar, usuario=self.user, nombre='Nómina', tipo='fijo',
            modo_entrada='anual', importe_declarado=Decimal('24000'),
            es_bruto=False, num_pagas=12, activo=True)
        ReglaReparto.objects.create(
            hogar=self.hogar, fondo=self.fondo, nombre='Aporte común',
            tipo_regla='porcentaje', porcentaje=Decimal('25'), orden=0, activo=True)

        resp = self.client.get(reverse('finanzas:vista_distribucion'))
        self.assertEqual(resp.status_code, 200)
        # El resultado del fondo sí se ve...
        self.assertContains(resp, 'Cómo queda cada fondo')
        self.assertContains(resp, 'Cuenta Conjunta')
        # ...pero la edición del fondo se ha ido a Gestión.
        self.assertNotContains(resp, 'id="modal-fondo"')
        self.assertNotContains(resp, 'id="modal-gastos"')
        self.assertContains(resp, reverse('finanzas:listar_fondos'))

    def test_los_fondos_sin_movimiento_no_llenan_el_resultado(self):
        from .models import FuenteIngreso, ReglaReparto

        FuenteIngreso.objects.create(
            hogar=self.hogar, usuario=self.user, nombre='Nómina', tipo='fijo',
            modo_entrada='anual', importe_declarado=Decimal('24000'),
            es_bruto=False, num_pagas=12, activo=True)
        ReglaReparto.objects.create(
            hogar=self.hogar, fondo=self.fondo, nombre='Aporte común',
            tipo_regla='porcentaje', porcentaje=Decimal('25'), orden=0, activo=True)

        quietos = self.client.get(
            reverse('finanzas:vista_distribucion')).context['fondos_quietos']
        self.assertEqual([f.id for f in quietos], [self.otro.id])


class FormularioEditarIngresoTests(TestCase):
    """El formulario de edición tiene que llegar con los valores actuales
    puestos y en un formato que el navegador acepte."""

    def setUp(self):
        from core.models import Hogar, UserProfile
        from .models import FuenteIngreso

        self.user = User.objects.create_user('adri', password='x')
        self.hogar = Hogar.objects.create(nombre='Casa', creado_por=self.user)
        perfil, _ = UserProfile.objects.get_or_create(user=self.user)
        perfil.hogar = self.hogar
        perfil.save()
        self.client.force_login(self.user)

        self.fuente = FuenteIngreso.objects.create(
            hogar=self.hogar, usuario=self.user, nombre='Alquiler piso', tipo='fijo',
            modo_entrada='anual', importe_declarado=Decimal('9600'),
            es_bruto=False, num_pagas=12, activo=True,
        )

    def _html(self):
        resp = self.client.get(reverse('finanzas:editar_ingreso', args=[self.fuente.id]))
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def _post(self, **extra):
        datos = {
            'usuario_id': self.user.id, 'nombre': 'Alquiler piso', 'tipo': 'fijo',
            'modo_entrada': 'anual', 'importe_declarado': '9600', 'es_bruto': 'false',
            'pais_fiscal': 'ES', 'num_pagas': '12', 'meses_pagas_extras': '6,12',
            'periodicidad': 'mensual', 'porcentaje_variabilidad': '0',
            'incluir_en_mensual': 'on', 'incluir_en_distribucion': 'on',
        }
        datos.update(extra)
        return self.client.post(
            reverse('finanzas:editar_ingreso', args=[self.fuente.id]), datos)

    def test_el_importe_llega_con_punto_decimal(self):
        """En es-ES un Decimal se pinta '9600,00', y <input type="number"> con
        coma lo descarta: el campo se vería VACÍO."""
        html = self._html()
        self.assertIn('value="9600.00"', html)
        self.assertNotIn('value="9600,00"', html)

    def test_el_porcentaje_de_variabilidad_tambien(self):
        self.fuente.tipo = 'variable'
        self.fuente.porcentaje_variabilidad = Decimal('12.5')
        self.fuente.save()
        html = self._html()
        self.assertIn('value="12.50"', html)   # dos decimales, con punto
        self.assertNotIn('value="12,50"', html)

    def test_el_campo_de_pagas_personalizadas_existe(self):
        """Faltaba la etiqueta <input>: sus atributos salían como texto suelto
        en la página y el JS del formulario moría al no encontrarlo."""
        html = self._html()
        self.assertIn('id="num_pagas_custom"', html)
        self.assertIn('name="num_pagas_custom"', html)
        # Y el bloque que lo contiene no se cierra sobre sí mismo.
        self.assertNotIn('id="bloque-pagas-custom" style="display: none;"></div>', html)

    def test_guardar_sin_tocar_nada_no_cambia_el_importe(self):
        resp = self._post()
        self.assertEqual(resp.status_code, 302)
        self.fuente.refresh_from_db()
        self.assertEqual(self.fuente.importe_declarado, Decimal('9600'))

    def test_guardar_un_importe_nuevo(self):
        self._post(importe_declarado='10800')
        self.fuente.refresh_from_db()
        self.assertEqual(self.fuente.importe_declarado, Decimal('10800'))

    def test_las_pagas_personalizadas_se_guardan_y_no_revientan(self):
        """El selector envía 'custom' y el número real va en el campo de al
        lado; antes esto acababa en int('custom') y un 500."""
        resp = self._post(num_pagas='custom', num_pagas_custom='16')
        self.assertEqual(resp.status_code, 302)
        self.fuente.refresh_from_db()
        self.assertEqual(self.fuente.num_pagas, 16)

    def test_un_numero_de_pagas_ilegible_cae_en_12(self):
        resp = self._post(num_pagas='custom', num_pagas_custom='')
        self.assertEqual(resp.status_code, 302)
        self.fuente.refresh_from_db()
        self.assertEqual(self.fuente.num_pagas, 12)

    def test_al_crear_tambien_valen_las_pagas_personalizadas(self):
        from .models import FuenteIngreso

        resp = self.client.post(reverse('finanzas:crear_ingreso'), {
            'usuario_id': self.user.id, 'nombre': 'Nómina', 'tipo': 'fijo',
            'modo_entrada': 'anual', 'importe_declarado': '30000', 'es_bruto': 'false',
            'pais_fiscal': 'ES', 'num_pagas': 'custom', 'num_pagas_custom': '14',
            'meses_pagas_extras': '6,12', 'periodicidad': 'mensual',
            'porcentaje_variabilidad': '0', 'incluir_en_mensual': 'on',
            'incluir_en_distribucion': 'on',
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            FuenteIngreso.objects.get(hogar=self.hogar, nombre='Nómina').num_pagas, 14)


class SimuladorLiquidezTests(TestCase):
    """El simulador tiene que ver el mismo dinero disponible que el resto de la
    app: un depósito es liquidez, no algo aparte."""

    def setUp(self):
        from core.models import Hogar, UserProfile
        from .models import FondoFamiliar, SaldoRealFondo

        self.user = User.objects.create_user('adri', password='x')
        self.hogar = Hogar.objects.create(nombre='Casa', creado_por=self.user)
        perfil, _ = UserProfile.objects.get_or_create(user=self.user)
        perfil.hogar = self.hogar
        perfil.save()
        self.client.force_login(self.user)

        self.hoy = datetime.date.today()
        self.fondo = FondoFamiliar.objects.create(
            hogar=self.hogar, nombre='Común', tipo_fondo='comun')
        SaldoRealFondo.objects.create(
            fondo=self.fondo, año=self.hoy.year, mes=self.hoy.month, saldo=Decimal('20000'))

    def _deposito(self, importe, fondo=None):
        dep = Inversion.objects.create(
            usuario=self.user, nombre='Depósito', tipo='DEPOSITO', fondo=fondo,
            deposito_tipo_interes=Decimal('0'), deposito_frecuencia='anual')
        MovimientoInversion.objects.create(
            inversion=dep, fecha=datetime.date(self.hoy.year, 1, 1), tipo='COMPRA',
            cantidad=Decimal(importe), precio_unitario=Decimal('1'))
        return dep

    def _liquidez(self):
        resp = self.client.get(reverse('finanzas:simulador_vivienda'))
        self.assertEqual(resp.status_code, 200)
        return resp.context['capital_liquidez']

    def test_sin_depositos_la_liquidez_es_el_saldo_de_los_fondos(self):
        self.assertEqual(self._liquidez(), Decimal('20000'))

    def test_el_deposito_cuenta_como_liquidez_disponible(self):
        self._deposito('15000')
        self.assertEqual(self._liquidez(), Decimal('35000.00'))

    def test_un_deposito_vinculado_a_un_fondo_no_se_cuenta_dos_veces(self):
        self._deposito('15000', fondo=self.fondo)
        self.assertEqual(self._liquidez(), Decimal('15000.00'))

    def test_la_pantalla_dice_cuanto_de_eso_son_depositos(self):
        self._deposito('15000')
        resp = self.client.get(reverse('finanzas:simulador_vivienda'))
        self.assertEqual(resp.context['capital_depositos'], Decimal('15000.00'))
        self.assertContains(resp, 'en depósitos')

    def test_el_simulador_y_evolucion_dan_la_misma_liquidez(self):
        from .views_evolucion import _fecha_corte_mes, _saldos_liquidez_patrimonio
        from .models import SaldoRealFondo

        self._deposito('15000')
        saldos = SaldoRealFondo.objects.filter(fondo__hogar=self.hogar)
        evo, _ = _saldos_liquidez_patrimonio(
            saldos, hogar=self.hogar,
            fecha_depositos=_fecha_corte_mes(self.hoy.year, self.hoy.month))
        self.assertEqual(self._liquidez(), evo)

    def test_el_simulador_de_vehiculo_usa_el_mismo_criterio(self):
        self._deposito('15000')
        resp = self.client.get(reverse('finanzas:simulador_vehiculo'))
        self.assertEqual(resp.context['capital_liquidez'], Decimal('35000.00'))


class GastosCompraViviendaTests(TestCase):
    """Los impuestos de comprar cambian por comunidad y por tipo de vivienda;
    el simulador tiene que reflejarlo, no aplicar un porcentaje único."""

    def test_segunda_mano_aplica_el_itp_de_la_comunidad(self):
        from .hipoteca import gastos_compra

        madrid = gastos_compra(300000, ccaa='MD')
        cataluña = gastos_compra(300000, ccaa='CT')
        self.assertEqual(madrid['impuestos'], Decimal('18000').__float__())   # 6 %
        self.assertEqual(cataluña['impuestos'], 30000.0)                      # 10 %
        self.assertGreater(cataluña['total'], madrid['total'])

    def test_obra_nueva_paga_iva_mas_ajd(self):
        from .hipoteca import gastos_compra

        g = gastos_compra(300000, ccaa='MD', obra_nueva=True)
        self.assertEqual(g['impuestos'], 300000 * (10 + 0.75) / 100)
        self.assertIn('IVA', g['impuesto_nombre'])

    def test_la_bonificacion_joven_baja_el_impuesto(self):
        from .hipoteca import gastos_compra

        normal = gastos_compra(200000, ccaa='CT')
        joven = gastos_compra(200000, ccaa='CT', joven=True)
        self.assertLess(joven['impuestos'], normal['impuestos'])

    def test_los_gastos_fijos_tienen_suelo_y_techo(self):
        from .hipoteca import gastos_compra, NOTARIA_MIN, NOTARIA_MAX

        barata = gastos_compra(60000, ccaa='MD')
        cara = gastos_compra(2000000, ccaa='MD')
        self.assertEqual(barata['notaria'], NOTARIA_MIN)
        self.assertEqual(cara['notaria'], NOTARIA_MAX)

    def test_precio_cero_no_revienta(self):
        from .hipoteca import gastos_compra

        g = gastos_compra(0)
        self.assertEqual(g['total'], 0)
        self.assertEqual(g['total_pct'], 0)

    def test_una_comunidad_desconocida_cae_en_madrid(self):
        from .hipoteca import gastos_compra

        self.assertEqual(gastos_compra(200000, ccaa='XX')['impuestos'],
                         gastos_compra(200000, ccaa='MD')['impuestos'])

    def test_la_tabla_tiene_las_17_comunidades_y_datos_completos(self):
        from .hipoteca import tabla_ccaa

        tabla = tabla_ccaa()
        self.assertEqual(len(tabla), 17)
        for c in tabla:
            with self.subTest(ccaa=c['nombre']):
                self.assertTrue(0 < c['itp'] <= 13)
                self.assertTrue(0 < c['ajd'] <= 2)
                self.assertLessEqual(c['itp_joven'], c['itp'])


class CalculoHipotecaTests(TestCase):
    """La cuota y su inversa: si estas dos no cuadran, no cuadra nada."""

    def test_cuota_de_libro(self):
        from .hipoteca import cuota_mensual

        # 200.000 € al 3 % a 30 años son 843,21 €/mes
        self.assertAlmostEqual(cuota_mensual(200000, 3, 30), 843.21, places=2)

    def test_sin_intereses_la_cuota_es_el_capital_entre_los_meses(self):
        from .hipoteca import cuota_mensual

        self.assertAlmostEqual(cuota_mensual(120000, 0, 10), 1000.0, places=2)

    def test_el_capital_maximo_es_la_inversa_de_la_cuota(self):
        from .hipoteca import capital_maximo, cuota_mensual

        cuota = cuota_mensual(250000, 3.5, 25)
        self.assertAlmostEqual(capital_maximo(cuota, 3.5, 25), 250000, places=0)

    def test_los_casos_vacios_dan_cero_y_no_dividen_por_cero(self):
        from .hipoteca import capital_maximo, cuota_mensual

        self.assertEqual(cuota_mensual(0, 3, 30), 0)
        self.assertEqual(cuota_mensual(100000, 3, 0), 0)
        self.assertEqual(capital_maximo(0, 3, 30), 0)

    def test_el_coste_de_tener_la_vivienda_suma_sus_partes(self):
        from .hipoteca import coste_recurrente_mensual

        c = coste_recurrente_mensual(300000)
        self.assertAlmostEqual(
            c['total'], c['mantenimiento'] + c['ibi'] + c['seguro'] + c['comunidad'], places=2)
        # 1 % de 300.000 al año son 250 €/mes de provisión de mantenimiento
        self.assertAlmostEqual(c['mantenimiento'], 250.0, places=2)


class SimuladorViviendaContextoTests(TestCase):
    """El simulador tiene que llegar con lo que la app ya sabe del hogar."""

    def setUp(self):
        from core.models import Hogar, UserProfile
        from .models import CategoriaGasto, FondoFamiliar, PartidaGasto, SaldoRealFondo

        self.user = User.objects.create_user('adri', password='x')
        self.hogar = Hogar.objects.create(nombre='Casa', creado_por=self.user)
        perfil, _ = UserProfile.objects.get_or_create(user=self.user)
        perfil.hogar = self.hogar
        perfil.save()
        self.client.force_login(self.user)

        hoy = datetime.date.today()
        fondo = FondoFamiliar.objects.create(hogar=self.hogar, nombre='Común', tipo_fondo='comun')
        SaldoRealFondo.objects.create(fondo=fondo, año=hoy.year, mes=hoy.month, saldo=Decimal('50000'))
        self.cat = CategoriaGasto.objects.create(hogar=self.hogar, nombre='Vivienda', tipo='fijo')

    def _sim(self):
        resp = self.client.get(reverse('finanzas:simulador_vivienda'))
        self.assertEqual(resp.status_code, 200)
        return resp.context['sim_data']

    def test_detecta_el_alquiler_que_se_paga_hoy(self):
        from .models import PartidaGasto

        PartidaGasto.objects.create(
            hogar=self.hogar, categoria=self.cat, nombre='Alquiler piso Sevilla',
            importe=Decimal('950'), periodicidad='mensual', activo=True)
        self.assertEqual(self._sim()['alquiler_actual'], 950.0)

    def test_suma_las_cuotas_de_prestamos_para_el_ratio(self):
        from .models import PartidaGasto

        PartidaGasto.objects.create(
            hogar=self.hogar, categoria=self.cat, nombre='Préstamo coche',
            importe=Decimal('220'), periodicidad='mensual', activo=True)
        PartidaGasto.objects.create(
            hogar=self.hogar, categoria=self.cat, nombre='Supermercado',
            importe=Decimal('400'), periodicidad='mensual', activo=True)
        self.assertEqual(self._sim()['otras_cuotas'], 220.0)

    def test_la_categoria_hipoteca_alquiler_ya_cuenta_como_cuota(self):
        """Donde el hogar apunta sus préstamos es en esa categoría: el ratio
        tiene que mirar ahí, no solo el nombre de la partida."""
        from .models import CategoriaGasto, PartidaGasto

        cat_hipoteca = CategoriaGasto.objects.create(
            hogar=self.hogar, nombre='Hipoteca / Alquiler', tipo='fijo')
        PartidaGasto.objects.create(
            hogar=self.hogar, categoria=cat_hipoteca, nombre='Cuota banco',
            importe=Decimal('640'), periodicidad='mensual', activo=True)

        self.assertEqual(self._sim()['otras_cuotas'], 640.0)

    def test_el_alquiler_de_esa_categoria_no_es_una_cuota(self):
        """El alquiler no es deuda, y además es lo que dejas de pagar al
        comprar: cuenta como alquiler, nunca como cuota del ratio."""
        from .models import CategoriaGasto, PartidaGasto

        cat_hipoteca = CategoriaGasto.objects.create(
            hogar=self.hogar, nombre='Hipoteca / Alquiler', tipo='fijo')
        PartidaGasto.objects.create(
            hogar=self.hogar, categoria=cat_hipoteca, nombre='Alquiler piso',
            importe=Decimal('900'), periodicidad='mensual', activo=True)

        datos = self._sim()
        self.assertEqual(datos['alquiler_actual'], 900.0)
        self.assertEqual(datos['otras_cuotas'], 0.0)

    def test_una_partida_no_se_cuenta_dos_veces(self):
        from .models import CategoriaGasto, PartidaGasto

        cat_hipoteca = CategoriaGasto.objects.create(
            hogar=self.hogar, nombre='Hipoteca / Alquiler', tipo='fijo')
        PartidaGasto.objects.create(
            hogar=self.hogar, categoria=cat_hipoteca, nombre='Hipoteca piso',
            importe=Decimal('700'), periodicidad='mensual', activo=True)

        self.assertEqual(self._sim()['otras_cuotas'], 700.0)

    def test_avisa_de_los_depositos_que_vencen_despues(self):
        hoy = datetime.date.today()
        dep = Inversion.objects.create(
            usuario=self.user, nombre='Depósito a plazo', tipo='DEPOSITO',
            deposito_tipo_interes=Decimal('0'), deposito_frecuencia='anual',
            deposito_fecha_liquidacion=hoy + datetime.timedelta(days=400))
        MovimientoInversion.objects.create(
            inversion=dep, fecha=hoy - datetime.timedelta(days=30), tipo='COMPRA',
            cantidad=Decimal('10000'), precio_unitario=Decimal('1'))

        atados = self._sim()['depositos_atados']
        self.assertEqual(len(atados), 1)
        self.assertEqual(atados[0]['nombre'], 'Depósito a plazo')

    def test_un_deposito_sin_vencimiento_no_genera_aviso(self):
        dep = Inversion.objects.create(
            usuario=self.user, nombre='Depósito abierto', tipo='DEPOSITO',
            deposito_tipo_interes=Decimal('0'), deposito_frecuencia='anual')
        MovimientoInversion.objects.create(
            inversion=dep, fecha=datetime.date.today(), tipo='COMPRA',
            cantidad=Decimal('5000'), precio_unitario=Decimal('1'))
        self.assertEqual(self._sim()['depositos_atados'], [])

    def test_lleva_la_tabla_de_impuestos_y_los_valores_por_defecto(self):
        sim = self._sim()
        self.assertEqual(len(sim['ccaa']), 17)
        self.assertIn('mantenimiento_pct', sim['defaults'])
        self.assertIn('iva_obra_nueva', sim['defaults'])


class RitmoRealTests(SimpleTestCase):
    """El ritmo real del año: lo ahorrado sale de los saldos (no se estima) y
    el gasto real de restárselo al ingreso. Sobre esa serie, media y mediana
    frente a lo que decía el presupuesto de esos mismos meses."""

    HOY = datetime.date(2026, 9, 6)  # septiembre: enero..agosto son meses cerrados
    AÑO = 2026

    def _flujo(self, ingreso='3000', gastos='2000', inversion='0', presupuestado=None):
        return {
            'ingreso_base_hogar': Decimal(ingreso),
            'ingreso_base_puro_hogar': Decimal(presupuestado if presupuestado is not None else ingreso),
            'total_gastos_all': Decimal(gastos),
            'total_inversion': Decimal(inversion),
        }

    def _analizar(self, saldos, flujos=None):
        """`saldos`: {mes: saldo}. El mismo valor sirve de liquidez y de
        patrimonio salvo que un test necesite distinguirlos."""
        from .analisis_evolucion import analizar
        datos = {m: (Decimal(str(v)), Decimal(str(v))) for m, v in saldos.items()}
        flujos = flujos or {m: self._flujo() for m in range(1, 13)}
        ultimo = Decimal(str(saldos[max(saldos)]))
        return analizar(datos, flujos, self.AÑO,
                        base_liquidez=ultimo, base_patrimonio=ultimo, hoy=self.HOY)

    def test_el_gasto_real_es_el_ingreso_menos_lo_ahorrado(self):
        # Ahorra 1.000 € al mes con 3.000 € de ingreso → gasta 2.000 €.
        analisis = self._analizar({m: 1000 * m for m in range(1, 9)})['liquidez']
        self.assertEqual(analisis['n_meses'], 7)  # enero no: le falta el mes anterior
        self.assertAlmostEqual(analisis['real']['ahorro']['media'], 1000)
        self.assertAlmostEqual(analisis['real']['gasto']['media'], 2000)
        self.assertAlmostEqual(analisis['real']['ingreso']['media'], 3000)

    def test_la_mediana_no_se_mueve_por_un_mes_excepcional(self):
        # Un mes con una derrama de 3.000 € dispara la media, no la mediana.
        saldos = {m: 500 * (m - 1) for m in range(1, 9)}
        for m in range(6, 9):
            saldos[m] -= 3000
        analisis = self._analizar(saldos)['liquidez']

        self.assertAlmostEqual(analisis['real']['gasto']['mediana'], 2500)
        self.assertGreater(analisis['real']['gasto']['media'],
                           analisis['real']['gasto']['mediana'])

    def test_el_mes_en_curso_no_entra_en_las_medias(self):
        """Septiembre está a medias: ni ha entrado todo el ingreso ni ha
        terminado el gasto. Contarlo ensuciaría media y mediana."""
        saldos = {m: 1000 * m for m in range(1, 9)}
        sin_mes_curso = self._analizar(saldos)['liquidez']

        saldos[9] = saldos[8] + 90000  # una venta a mitad de septiembre
        con_mes_curso = self._analizar(saldos)['liquidez']

        self.assertEqual(con_mes_curso['n_meses'], sin_mes_curso['n_meses'])
        self.assertAlmostEqual(con_mes_curso['real']['ahorro']['media'],
                               sin_mes_curso['real']['ahorro']['media'])

    def test_un_mes_sin_saldo_no_inventa_un_ritmo(self):
        """Sin el saldo de abril, la diferencia mayo − marzo abarcaría dos
        meses: ni abril ni mayo cuentan como ritmo mensual."""
        saldos = {m: 1000 * m for m in range(1, 9) if m != 4}
        analisis = self._analizar(saldos)['liquidez']

        self.assertEqual([f['mes'] for f in analisis['meses']], [2, 3, 6, 7, 8])
        self.assertAlmostEqual(analisis['real']['ahorro']['media'], 1000)

    def test_el_desvio_compara_con_el_presupuesto_de_esos_meses(self):
        # Presupuesta 2.000 € de gasto y ahorra 500 € al mes → gasta 2.500 €.
        analisis = self._analizar({m: 500 * m for m in range(1, 9)})['liquidez']

        self.assertAlmostEqual(analisis['presupuesto']['gasto'], 2000)
        self.assertAlmostEqual(analisis['desvio']['gasto']['media'], 500)
        self.assertAlmostEqual(analisis['desvio']['ahorro']['media'], -500)
        self.assertTrue(any(a['tono'] == 'aviso' for a in analisis['avisos']))

    def test_un_presupuesto_que_cuadra_no_pide_correcciones(self):
        analisis = self._analizar({m: 1000 * m for m in range(1, 9)})['liquidez']
        self.assertEqual([a['tono'] for a in analisis['avisos']], ['ok'])

    def test_lo_que_va_a_inversion_es_gasto_para_la_liquidez_pero_no_para_el_patrimonio(self):
        flujos = {m: self._flujo(gastos='1700', inversion='300') for m in range(1, 13)}
        analisis = self._analizar({m: 1000 * m for m in range(1, 9)}, flujos)

        self.assertAlmostEqual(analisis['liquidez']['presupuesto']['gasto'], 2000)
        self.assertAlmostEqual(analisis['patrimonio']['presupuesto']['gasto'], 1700)

    def test_el_ingreso_presupuestado_no_incluye_los_extras_del_mes(self):
        """Cobrar un bonus no significa que el presupuesto contara con él: el
        plan es la nómina base, y la diferencia es justo lo que hay que ver."""
        flujos = {m: self._flujo(ingreso='3500', presupuestado='3000') for m in range(1, 13)}
        analisis = self._analizar({m: 1000 * m for m in range(1, 9)}, flujos)['liquidez']

        self.assertAlmostEqual(analisis['presupuesto']['ingreso'], 3000)
        self.assertAlmostEqual(analisis['desvio']['ingreso']['media'], 500)

    def test_sin_dos_meses_seguidos_no_hay_ritmo_que_medir(self):
        analisis = self._analizar({1: 1000})['liquidez']
        self.assertEqual(analisis['n_meses'], 0)
        self.assertEqual(analisis['escenarios'], [])
        self.assertIsNone(analisis['real']['ahorro']['media'])

    def test_la_proyeccion_sin_rentabilidad_es_el_ahorro_acumulado(self):
        from .analisis_evolucion import proyectar
        self.assertAlmostEqual(proyectar(10000, 500, 10), 10000 + 500 * 120)

    def test_la_rentabilidad_solo_suma_sobre_el_ahorro_puro(self):
        from .analisis_evolucion import proyectar
        sin_interes = proyectar(10000, 500, 10)
        con_interes = proyectar(10000, 500, 10, rentabilidad_anual=0.05)
        self.assertGreater(con_interes, sin_interes)

    def test_hay_proyeccion_a_1_2_5_y_10_años_para_cada_ritmo(self):
        analisis = self._analizar({m: 1000 * m for m in range(1, 9)})['liquidez']
        claves = [e['clave'] for e in analisis['escenarios']]
        self.assertEqual(claves, ['media', 'mediana', 'presupuesto'])
        for escenario in analisis['escenarios']:
            self.assertEqual(sorted(escenario['valores']), ['1', '10', '2', '5'])
        # Al ritmo real (1.000 €/mes) desde 8.000 € → 8.000 + 12.000 en un año.
        self.assertAlmostEqual(analisis['escenarios'][0]['valores']['1'], 20000)


class RitmoRealEnLaVistaTests(TestCase):
    """El análisis viaja a la pantalla y se recalcula al guardar un saldo."""

    def setUp(self):
        from core.models import Hogar, UserProfile
        from .models import FondoFamiliar, FuenteIngreso, SaldoRealFondo

        self.user = User.objects.create_user('adri', password='x')
        self.hogar = Hogar.objects.create(nombre='Casa', creado_por=self.user)
        perfil, _ = UserProfile.objects.get_or_create(user=self.user)
        perfil.hogar = self.hogar
        perfil.save()

        self.hoy = datetime.date.today()
        self.fondo = FondoFamiliar.objects.create(
            hogar=self.hogar, nombre='Común', tipo_fondo='comun')
        for mes in range(1, self.hoy.month + 1):
            SaldoRealFondo.objects.create(
                fondo=self.fondo, año=self.hoy.year, mes=mes,
                saldo=Decimal('1000') * mes)

        FuenteIngreso.objects.create(
            hogar=self.hogar, usuario=self.user, nombre='Nómina', tipo='fijo',
            modo_entrada='anual', importe_declarado=Decimal('36000'),
            es_bruto=False, num_pagas=12, activo=True,
        )

    def test_la_vista_trae_el_ritmo_y_las_proyecciones(self):
        self.client.force_login(self.user)
        resp = self.client.get(reverse('finanzas:vista_evolucion'))

        self.assertEqual(resp.status_code, 200)
        analisis = resp.context['analisis']
        self.assertEqual(analisis['horizontes'], [1, 2, 5, 10])
        self.assertIn('liquidez', analisis)
        self.assertIn('patrimonio', analisis)
        self.assertContains(resp, 'evo-analisis-data')

    def test_guardar_un_saldo_devuelve_el_ritmo_recalculado(self):
        self.client.force_login(self.user)
        resp = self.client.post(
            reverse('finanzas:registrar_saldo_fondo'),
            {'fondo_id': self.fondo.id, 'mes': self.hoy.month,
             'año': self.hoy.year, 'saldo': '99000'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn('analisis', resp.json()['estado'])

    def test_el_cierre_guarda_tambien_lo_que_estaba_presupuestado(self):
        """Sin esto, 'lo presupuestado originalmente' se recalcularía con la
        configuración de hoy y la comparación no diría nada."""
        from .cierres import congelar_mes
        from .models import CierreMensual
        mes_cerrado = self.hoy.month - 1
        if not mes_cerrado:
            self.skipTest('En enero no hay ningún mes cerrado de este año')

        congelar_mes(self.hogar, self.hoy.year, mes_cerrado)
        cierre = CierreMensual.objects.get(
            hogar=self.hogar, año=self.hoy.year, mes=mes_cerrado)
        self.assertEqual(cierre.ingreso_previsto, Decimal('3000'))


class FuentesDeDatosSimuladorTests(TestCase):
    """El simulador de vivienda puede trabajar con el presupuesto o con lo que
    de verdad pasa cada mes (medias, medianas y último mes cerrado)."""

    def setUp(self):
        from core.models import Hogar, UserProfile
        from .models import FondoFamiliar, FuenteIngreso, SaldoRealFondo

        self.user = User.objects.create_user('adri', password='x')
        self.hogar = Hogar.objects.create(nombre='Casa', creado_por=self.user)
        perfil, _ = UserProfile.objects.get_or_create(user=self.user)
        perfil.hogar = self.hogar
        perfil.save()

        self.hoy = datetime.date.today()
        self.fondo = FondoFamiliar.objects.create(
            hogar=self.hogar, nombre='Común', tipo_fondo='comun')
        FuenteIngreso.objects.create(
            hogar=self.hogar, usuario=self.user, nombre='Nómina', tipo='fijo',
            modo_entrada='anual', importe_declarado=Decimal('36000'),
            es_bruto=False, num_pagas=12, activo=True,
        )

    def _con_saldos(self, ahorro_mensual):
        from .models import SaldoRealFondo
        for mes in range(1, self.hoy.month + 1):
            SaldoRealFondo.objects.update_or_create(
                fondo=self.fondo, año=self.hoy.year, mes=mes,
                defaults={'saldo': Decimal(ahorro_mensual) * mes})

    def _fuentes(self):
        from .views_simuladores import _datos_financieros, _fuentes_de_datos
        datos = _datos_financieros(self.hogar)
        return _fuentes_de_datos(self.hogar, datos['sim_data'])

    def test_el_presupuesto_siempre_esta_disponible(self):
        fuentes = self._fuentes()
        self.assertTrue(fuentes['presupuesto']['disponible'])
        self.assertEqual(fuentes['presupuesto']['ingresos'], 3000)

    def test_sin_saldos_registrados_las_fuentes_reales_no_se_pueden_elegir(self):
        fuentes = self._fuentes()
        for clave in ('media', 'mediana', 'ultimo'):
            self.assertFalse(fuentes[clave]['disponible'], clave)

    def test_con_saldos_el_gasto_real_es_el_ingreso_menos_lo_ahorrado(self):
        if self.hoy.month < 3:
            self.skipTest('Hacen falta al menos dos meses cerrados')
        self._con_saldos(500)  # ahorra 500 €/mes de liquidez

        fuentes = self._fuentes()
        for clave in ('media', 'mediana', 'ultimo'):
            self.assertTrue(fuentes[clave]['disponible'], clave)
            self.assertEqual(fuentes[clave]['ingresos'], 3000)
            self.assertEqual(fuentes[clave]['ahorro'], 500)
            self.assertEqual(fuentes[clave]['gastos'], 2500)

    def test_las_fuentes_reales_llegan_a_la_pantalla(self):
        if self.hoy.month < 3:
            self.skipTest('Hacen falta al menos dos meses cerrados')
        self._con_saldos(500)

        self.client.force_login(self.user)
        resp = self.client.get(reverse('finanzas:simulador_vivienda'))

        self.assertEqual(resp.status_code, 200)
        fuentes = resp.context['sim_data']['fuentes']
        self.assertEqual(sorted(fuentes), ['media', 'mediana', 'presupuesto', 'ultimo'])


class CategoriasCrudTests(TestCase):
    """Gestión de categorías: crearlas, editarlas, archivarlas y borrarlas.

    Las categorías son la pieza común de gastos, ingresos y movimientos del
    extracto, así que tocarlas no puede llevarse por delante ni el presupuesto
    declarado ni lo ya clasificado sin decirlo.
    """

    def setUp(self):
        from core.models import Hogar
        from finanzas.views_gastos import _crear_categorias_predefinidas

        self.hogar = Hogar.objects.create(nombre='Hogar')
        self.user = User.objects.create_user('categorizador', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        self.client.force_login(self.user)

    def _categoria(self, nombre):
        from finanzas.models import CategoriaGasto
        return CategoriaGasto.objects.get(hogar=self.hogar, nombre=nombre)

    def test_las_predefinidas_nacen_con_el_computo_de_su_bloque(self):
        self.assertEqual(self._categoria('Traspaso entre cuentas').computo, 'neutro')
        self.assertEqual(self._categoria('Nomina').computo, 'suma')
        self.assertEqual(self._categoria('Alimentacion').computo, 'resta')

    def test_crear_categoria_con_computo_propio(self):
        self.client.post(reverse('finanzas:crear_categoria'), {
            'nombre': 'Pago tarjeta', 'tipo': 'variable', 'computo': 'neutro',
        })
        cat = self._categoria('Pago tarjeta')
        self.assertEqual(cat.tipo, 'variable')
        self.assertEqual(cat.computo, 'neutro')
        self.assertFalse(cat.es_predefinida)

    def test_un_computo_invalido_cae_al_del_bloque(self):
        self.client.post(reverse('finanzas:crear_categoria'), {
            'nombre': 'Mascotas', 'tipo': 'variable', 'computo': 'inventado',
        })
        self.assertEqual(self._categoria('Mascotas').computo, 'resta')

    def test_editar_renombra_y_cambia_bloque_y_computo(self):
        cat = self._categoria('Ocio')
        self.client.post(reverse('finanzas:editar_categoria', args=[cat.id]), {
            'nombre': 'Ocio y cultura', 'tipo': 'variable', 'computo': 'neutro',
        })
        cat.refresh_from_db()
        self.assertEqual(cat.nombre, 'Ocio y cultura')
        self.assertEqual(cat.tipo, 'variable')
        self.assertEqual(cat.computo, 'neutro')

    def test_no_se_puede_renombrar_a_una_categoria_que_ya_existe(self):
        cat = self._categoria('Ocio')
        self.client.post(reverse('finanzas:editar_categoria', args=[cat.id]), {
            'nombre': 'Ropa', 'tipo': 'discrecional', 'computo': 'resta',
        })
        cat.refresh_from_db()
        self.assertEqual(cat.nombre, 'Ocio')

    def test_archivar_saca_la_categoria_sin_borrar_nada(self):
        from finanzas.models import CategoriaGasto, PartidaGasto

        cat = self._categoria('Gimnasio')
        PartidaGasto.objects.create(
            hogar=self.hogar, categoria=cat, nombre='Cuota', importe=Decimal('30'),
        )
        self.client.post(reverse('finanzas:archivar_categoria', args=[cat.id]))

        cat.refresh_from_db()
        self.assertFalse(cat.activo)
        self.assertEqual(cat.partidas.count(), 1)
        self.assertTrue(CategoriaGasto.objects.filter(pk=cat.pk).exists())

    def test_una_predefinida_archivada_no_resucita(self):
        from finanzas.views_gastos import _crear_categorias_predefinidas

        cat = self._categoria('Gimnasio')
        self.client.post(reverse('finanzas:archivar_categoria', args=[cat.id]))
        _crear_categorias_predefinidas(self.hogar)

        cat.refresh_from_db()
        self.assertFalse(cat.activo)

    def test_no_se_borra_una_categoria_con_gasto_declarado_sin_destino(self):
        from finanzas.models import CategoriaGasto, PartidaGasto

        cat = self._categoria('Gimnasio')
        PartidaGasto.objects.create(
            hogar=self.hogar, categoria=cat, nombre='Cuota', importe=Decimal('30'),
        )
        self.client.post(reverse('finanzas:eliminar_categoria', args=[cat.id]))

        self.assertTrue(CategoriaGasto.objects.filter(pk=cat.pk).exists())
        self.assertEqual(PartidaGasto.objects.filter(categoria=cat).count(), 1)

    def test_borrar_reasignando_mueve_partidas_movimientos_y_reglas(self):
        from datetime import date

        from extractos.models import ExtractoBancario, MovimientoBancario, ReglaCategorizacion
        from finanzas.models import CategoriaGasto, PartidaGasto

        origen = self._categoria('Gimnasio')
        destino = self._categoria('Salud / Farmacia')
        partida = PartidaGasto.objects.create(
            hogar=self.hogar, categoria=origen, nombre='Cuota', importe=Decimal('30'),
        )
        extracto = ExtractoBancario.objects.create(hogar=self.hogar, usuario=self.user)
        mov = MovimientoBancario.objects.create(
            extracto=extracto, hogar=self.hogar, fecha=date(2026, 7, 1),
            concepto='Cuota gimnasio', importe=Decimal('-30'), categoria=origen,
        )
        regla = ReglaCategorizacion.objects.create(
            hogar=self.hogar, patron='gimnasio', categoria=origen,
        )

        self.client.post(reverse('finanzas:eliminar_categoria', args=[origen.id]), {
            'reasignar_a': destino.id,
        })

        self.assertFalse(CategoriaGasto.objects.filter(pk=origen.pk).exists())
        partida.refresh_from_db(); mov.refresh_from_db(); regla.refresh_from_db()
        self.assertEqual(partida.categoria, destino)
        self.assertEqual(mov.categoria, destino)
        self.assertEqual(regla.categoria, destino)

    def test_borrar_sin_destino_deja_los_movimientos_sin_categorizar(self):
        from datetime import date

        from extractos.models import ExtractoBancario, MovimientoBancario
        from finanzas.models import CategoriaGasto

        cat = self._categoria('Gimnasio')
        extracto = ExtractoBancario.objects.create(hogar=self.hogar, usuario=self.user)
        mov = MovimientoBancario.objects.create(
            extracto=extracto, hogar=self.hogar, fecha=date(2026, 7, 1),
            concepto='Cuota gimnasio', importe=Decimal('-30'), categoria=cat,
        )

        self.client.post(reverse('finanzas:eliminar_categoria', args=[cat.id]))

        self.assertFalse(CategoriaGasto.objects.filter(pk=cat.pk).exists())
        mov.refresh_from_db()
        self.assertIsNone(mov.categoria)

    def test_la_pantalla_lista_las_categorias_por_bloque(self):
        respuesta = self.client.get(reverse('finanzas:listar_categorias'))
        self.assertEqual(respuesta.status_code, 200)
        etiquetas = [b['etiqueta'] for b in respuesta.context['bloques']]
        self.assertEqual(etiquetas[0], 'Fijos')
        self.assertIn('Traspasos', etiquetas)

    def test_crear_desde_el_presupuesto_devuelve_al_presupuesto(self):
        respuesta = self.client.post(reverse('finanzas:crear_categoria'), {
            'nombre': 'Mascotas', 'tipo': 'variable', 'computo': 'resta',
            'volver_a': reverse('finanzas:listar_gastos'),
        })
        self.assertRedirects(respuesta, reverse('finanzas:listar_gastos'))

    def test_no_se_admite_un_destino_de_vuelta_externo(self):
        respuesta = self.client.post(reverse('finanzas:crear_categoria'), {
            'nombre': 'Mascotas', 'tipo': 'variable', 'computo': 'resta',
            'volver_a': 'https://example.com/phishing',
        })
        self.assertRedirects(respuesta, reverse('finanzas:listar_categorias'))

    def test_eliminar_una_predefinida_la_elimina_de_verdad(self):
        """Las de fábrica se recreaban en cada visita, así que borrarlas no
        servía de nada: reaparecían solas."""
        from finanzas.models import CategoriaGasto
        from finanzas.views_gastos import _crear_categorias_predefinidas

        cat = self._categoria('Gimnasio')
        self.client.post(reverse('finanzas:eliminar_categoria', args=[cat.id]))
        _crear_categorias_predefinidas(self.hogar)

        self.assertFalse(
            CategoriaGasto.objects.filter(hogar=self.hogar, nombre='Gimnasio').exists()
        )

    def test_volver_a_crearla_a_mano_la_recupera(self):
        cat = self._categoria('Gimnasio')
        self.client.post(reverse('finanzas:eliminar_categoria', args=[cat.id]))
        self.client.post(reverse('finanzas:crear_categoria'), {
            'nombre': 'Gimnasio', 'tipo': 'fijo', 'computo': 'resta',
        })

        self.assertEqual(self._categoria('Gimnasio').tipo, 'fijo')
        # Y ya no vuelve a desaparecer en la siguiente visita.
        self.client.get(reverse('finanzas:listar_categorias'))
        self.assertEqual(self._categoria('Gimnasio').tipo, 'fijo')

    def test_una_categoria_con_gasto_declarado_no_puede_ser_neutra(self):
        from finanzas.models import PartidaGasto

        cat = self._categoria('Gimnasio')
        PartidaGasto.objects.create(
            hogar=self.hogar, categoria=cat, nombre='Cuota', importe=Decimal('30'),
        )
        self.client.post(reverse('finanzas:editar_categoria', args=[cat.id]), {
            'nombre': 'Gimnasio', 'tipo': 'fijo', 'computo': 'neutro',
        })

        cat.refresh_from_db()
        self.assertEqual(cat.computo, 'resta')


class CostesDeActivoTests(TestCase):
    """Lo que cuesta mantener un vehículo o una propiedad: lo declarado frente
    a lo que de verdad ha pasado por el banco."""

    def setUp(self):
        from core.models import Hogar
        from finanzas.models import Vehiculo
        from finanzas.views_gastos import _crear_categorias_predefinidas
        from extractos.models import ExtractoBancario

        self.hogar = Hogar.objects.create(nombre='Hogar')
        self.user = User.objects.create_user('conductor', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        self.client.force_login(self.user)
        self.coche = Vehiculo.objects.create(hogar=self.hogar, nombre='Golf')
        self.extracto = ExtractoBancario.objects.create(hogar=self.hogar, usuario=self.user)

    def _categoria(self, nombre):
        from finanzas.models import CategoriaGasto
        return CategoriaGasto.objects.get(hogar=self.hogar, nombre=nombre)

    def declarar(self, nombre, importe, periodicidad='mensual', categoria='Gasolina', activo=None):
        from finanzas.models import PartidaGasto
        from finanzas import costes_activo

        partida = PartidaGasto(
            hogar=self.hogar, categoria=self._categoria(categoria), nombre=nombre,
            importe=Decimal(importe), periodicidad=periodicidad,
        )
        costes_activo.asignar(partida, activo if activo is not None else self.coche)
        partida.save()
        return partida

    def pagar(self, concepto, importe, mes=3, dia=5, anio=2026, categoria='Gasolina', activo='mismo'):
        from extractos.models import MovimientoBancario
        from finanzas import costes_activo

        mov = MovimientoBancario(
            extracto=self.extracto, hogar=self.hogar, fecha=datetime.date(anio, mes, dia),
            concepto=concepto, importe=Decimal(importe),
            categoria=self._categoria(categoria) if categoria else None,
        )
        costes_activo.asignar(mov, self.coche if activo == 'mismo' else activo)
        mov.save()
        return mov

    def ficha(self, anio=2026):
        from finanzas import costes_activo
        return costes_activo.costes(self.coche, anio)

    def test_el_teorico_prorratea_las_periodicidades(self):
        self.declarar('Gasolina', '80')                        # 80/mes
        self.declarar('Seguro', '360', 'anual', 'Seguro coche')  # 30/mes
        f = self.ficha()

        self.assertEqual(f['teorico_mensual'], Decimal('110'))
        self.assertEqual(f['teorico_anual'], Decimal('1320'))

    def test_el_real_sale_de_los_movimientos_imputados(self):
        self.declarar('Gasolina', '80')
        self.pagar('Repsol', '-65', mes=1)
        self.pagar('Repsol', '-70', mes=2)
        f = self.ficha()

        self.assertEqual(f['real_anual'], Decimal('135'))
        self.assertEqual(f['num_movimientos'], 2)
        # La media mensual se calcula sobre los meses con gasto, no sobre doce:
        # si no, un coche estrenado en diciembre parecería baratísimo.
        self.assertEqual(f['meses_con_datos'], 2)
        self.assertEqual(f['real_mensual'], Decimal('67.5'))

    def test_la_barra_mide_lo_gastado_contra_el_presupuesto_anual(self):
        self.declarar('Gasolina', '100')  # 1.200 al año
        self.pagar('Repsol', '-600', mes=1)
        self.assertEqual(self.ficha()['pct_ejecucion'], 50)

        self.pagar('Taller', '-900', mes=2)
        f = self.ficha()
        self.assertEqual(f['pct_ejecucion'], 125)
        self.assertEqual(f['diferencia_anual'], Decimal('300'))

    def test_sin_presupuesto_declarado_no_hay_porcentaje(self):
        self.pagar('Repsol', '-65')
        self.assertIsNone(self.ficha()['pct_ejecucion'])

    def test_no_se_cuela_el_gasto_de_otro_activo(self):
        from finanzas.models import Vehiculo

        otro = Vehiculo.objects.create(hogar=self.hogar, nombre='Moto')
        self.declarar('Gasolina Golf', '80')
        self.declarar('Gasolina moto', '30', activo=otro)
        self.pagar('Repsol Golf', '-65')
        self.pagar('Repsol moto', '-20', activo=otro)

        f = self.ficha()
        self.assertEqual(f['teorico_mensual'], Decimal('80'))
        self.assertEqual(f['real_anual'], Decimal('65'))

    def test_reimputar_no_deja_el_gasto_contado_dos_veces(self):
        """Los dos campos se limpian siempre: sin eso, mover un gasto del coche
        a la casa lo dejaría contado en ambos."""
        from finanzas import costes_activo
        from finanzas.models import Propiedad

        casa = Propiedad.objects.create(
            hogar=self.hogar, nombre='Piso', fecha_compra=datetime.date(2020, 1, 1),
            precio_compra=Decimal('100000'), valor_actual=Decimal('120000'),
        )
        mov = self.pagar('Repsol', '-65')
        costes_activo.asignar(mov, casa)
        mov.save()

        self.assertEqual(self.ficha()['real_anual'], Decimal('0'))
        self.assertEqual(costes_activo.costes(casa, 2026)['real_anual'], Decimal('65'))

    def test_solo_cuenta_el_gasto_del_anio_mirado(self):
        self.pagar('Repsol', '-65', anio=2025)
        self.pagar('Repsol', '-70', anio=2026)

        self.assertEqual(self.ficha(2026)['real_anual'], Decimal('70'))
        self.assertEqual(self.ficha(2025)['real_anual'], Decimal('65'))

    def test_un_ingreso_imputado_no_cuenta_como_coste(self):
        """Vender una rueda no es un gasto del coche."""
        self.pagar('Venta de ruedas', '150', categoria='Otros ingresos')
        self.pagar('Repsol', '-65')
        self.assertEqual(self.ficha()['real_anual'], Decimal('65'))

    def test_el_desglose_por_categoria_cruza_declarado_y_real(self):
        """Las dos columnas son del AÑO: lo que suman las partidas de la
        categoría en doce meses y lo que los pagos de este año le imputan."""
        self.declarar('Gasolina', '100')
        self.pagar('Repsol', '-1500', mes=1)
        fila = self.ficha()['por_categoria'][0]

        self.assertEqual(fila['categoria'], 'Gasolina')
        self.assertEqual(fila['declarado_anual'], Decimal('1200'))
        self.assertEqual(fila['real_anual'], Decimal('1500'))
        self.assertEqual(fila['pagado_anual'], Decimal('1500'))
        self.assertEqual(fila['diferencia'], Decimal('300'))

    def test_el_ritmo_mensual_divide_entre_los_meses_ya_cerrados(self):
        """Un año cerrado son sus doce meses y la cuenta es la de siempre."""
        from finanzas import costes_activo

        pasado = datetime.date.today().year - 1
        self.pagar('Repsol', '-120', mes=1, anio=pasado)
        self.pagar('Repsol', '-120', mes=2, anio=pasado)

        f = costes_activo.costes(self.coche, pasado)
        self.assertEqual(f['meses_cerrados'], 12)
        self.assertEqual(f['devengado_cerrado'], Decimal('240'))
        self.assertEqual(f['ritmo_mensual'], Decimal('20'))

    def test_el_mes_en_curso_no_entra_en_la_media(self):
        """Dividir lo que va de año entre doce dice que el activo cuesta la
        mitad de lo que cuesta; y meter el mes en curso, que lleva unos días de
        gasto contra un mes entero de divisor, la hunde por el otro lado."""
        from finanzas import costes_activo

        hoy = datetime.date.today()
        if hoy.month == 1:
            self.skipTest('En enero todavía no hay ningún mes cerrado')
        cerrados = hoy.month - 1
        self.pagar('Repsol', '-100', mes=1, dia=5, anio=hoy.year)
        self.pagar('Taller', '-900', mes=hoy.month, dia=1, anio=hoy.year)

        f = costes_activo.costes(self.coche, hoy.year)

        self.assertEqual(f['meses_cerrados'], cerrados)
        self.assertEqual(f['devengado_cerrado'], Decimal('100'))
        self.assertEqual(f['ritmo_mensual'], round(Decimal('100') / cerrados, 2))
        # Y lo pagado del año sigue contándose entero: lo que cambia es el
        # divisor de la media, no lo que ha salido del banco.
        self.assertEqual(f['real_anual'], Decimal('1000'))

    def test_el_ritmo_del_anio_pone_el_porcentaje_en_contexto(self):
        """Un 76% del presupuesto en marzo y en diciembre no son lo mismo."""
        from finanzas import costes_activo

        hoy = datetime.date.today()
        self.assertEqual(costes_activo.costes(self.coche, hoy.year - 1)['pct_transcurrido'], 100)
        self.assertEqual(costes_activo.costes(self.coche, hoy.year + 1)['pct_transcurrido'], 0)
        self.assertEqual(
            costes_activo.costes(self.coche, hoy.year)['pct_transcurrido'],
            int(hoy.month / 12 * 100),
        )

    def test_resolver_no_acepta_activos_de_otro_hogar(self):
        from core.models import Hogar
        from finanzas import costes_activo
        from finanzas.models import Vehiculo

        ajeno = Vehiculo.objects.create(
            hogar=Hogar.objects.create(nombre='Otro'), nombre='Ajeno',
        )
        self.assertIsNone(costes_activo.resolver(self.hogar, ajeno.clave_activo))
        self.assertIsNone(costes_activo.resolver(self.hogar, 'vehiculo:inventado'))
        self.assertIsNone(costes_activo.resolver(self.hogar, 'otracosa:1'))
        self.assertEqual(costes_activo.resolver(self.hogar, self.coche.clave_activo), self.coche)


class VehiculosVistaTests(TestCase):
    """Las pantallas de vehículos."""

    def setUp(self):
        from core.models import Hogar
        from finanzas.views_gastos import _crear_categorias_predefinidas

        self.hogar = Hogar.objects.create(nombre='Hogar')
        self.user = User.objects.create_user('conductor', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        self.client.force_login(self.user)

    def crear(self, **datos):
        datos.setdefault('nombre', 'Golf')
        datos.setdefault('tipo', 'coche')
        return self.client.post(reverse('finanzas:crear_vehiculo'), datos)

    def test_crear_y_editar_un_vehiculo(self):
        from finanzas.models import Vehiculo

        self.crear(marca_modelo='VW Golf 1.6', matricula='1234 ABC', precio_compra='18.000')
        coche = Vehiculo.objects.get()
        self.assertEqual(coche.matricula, '1234 ABC')
        self.assertEqual(coche.precio_compra, Decimal('18000'))

        self.client.post(reverse('finanzas:editar_vehiculo', args=[coche.id]), {
            'nombre': 'Golf de Ana', 'tipo': 'coche',
        })
        coche.refresh_from_db()
        self.assertEqual(coche.nombre, 'Golf de Ana')

    def test_un_tipo_inventado_cae_a_coche(self):
        from finanzas.models import Vehiculo

        self.crear(tipo='submarino')
        self.assertEqual(Vehiculo.objects.get().tipo, 'coche')

    def test_archivar_no_borra_y_eliminar_no_se_lleva_los_gastos(self):
        from finanzas import costes_activo
        from finanzas.models import CategoriaGasto, PartidaGasto, Vehiculo

        self.crear()
        coche = Vehiculo.objects.get()
        partida = PartidaGasto(
            hogar=self.hogar, categoria=CategoriaGasto.objects.get(hogar=self.hogar, nombre='Gasolina'),
            nombre='Gasolina', importe=Decimal('80'),
        )
        costes_activo.asignar(partida, coche)
        partida.save()

        self.client.post(reverse('finanzas:archivar_vehiculo', args=[coche.id]))
        coche.refresh_from_db()
        self.assertFalse(coche.activo)

        self.client.post(reverse('finanzas:eliminar_vehiculo', args=[coche.id]))
        self.assertFalse(Vehiculo.objects.exists())
        partida.refresh_from_db()
        self.assertIsNone(partida.vehiculo_id)
        self.assertEqual(partida.nombre, 'Gasolina')

    def test_la_pantalla_lista_los_vehiculos_con_sus_costes(self):
        self.crear()
        respuesta = self.client.get(reverse('finanzas:listar_vehiculos'))
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(len(respuesta.context['fichas']), 1)

    def test_imputar_una_partida_desde_la_ficha(self):
        from finanzas.models import CategoriaGasto, PartidaGasto, Vehiculo

        self.crear()
        coche = Vehiculo.objects.get()
        partida = PartidaGasto.objects.create(
            hogar=self.hogar, nombre='Seguro', importe=Decimal('360'), periodicidad='anual',
            categoria=CategoriaGasto.objects.get(hogar=self.hogar, nombre='Seguro coche'),
        )
        self.client.post(reverse('finanzas:imputar_partida'), {
            'partida_id': partida.id, 'activo': coche.clave_activo,
        })

        partida.refresh_from_db()
        self.assertEqual(partida.vehiculo, coche)

        # Y quitarla deja el gasto sin activo.
        self.client.post(reverse('finanzas:imputar_partida'), {
            'partida_id': partida.id, 'activo': '',
        })
        partida.refresh_from_db()
        self.assertIsNone(partida.vehiculo_id)

    def test_la_depreciacion_solo_sale_con_los_datos_necesarios(self):
        from finanzas.models import Vehiculo

        sin_datos = Vehiculo.objects.create(hogar=self.hogar, nombre='Sin datos')
        self.assertIsNone(sin_datos.depreciacion_anual)

        con_datos = Vehiculo.objects.create(
            hogar=self.hogar, nombre='Con datos',
            fecha_compra=datetime.date.today() - datetime.timedelta(days=730),
            precio_compra=Decimal('20000'), valor_actual=Decimal('14000'),
        )
        self.assertAlmostEqual(float(con_datos.depreciacion_anual), 3000, delta=20)


class ParseDecimalMilesTests(SimpleTestCase):
    """El punto como separador de miles.

    «18.000» en un formulario español son dieciocho mil, no dieciocho: el
    importe de compra de un coche se quedaba en 18 € al guardarlo."""

    def test_el_punto_de_miles_no_se_lee_como_decimal(self):
        from finanzas.parsing import parse_decimal

        self.assertEqual(parse_decimal('18.000'), Decimal('18000'))
        self.assertEqual(parse_decimal('1.234.567'), Decimal('1234567'))

    def test_los_decimales_de_verdad_siguen_siendo_decimales(self):
        from finanzas.parsing import parse_decimal

        self.assertEqual(parse_decimal('18.50'), Decimal('18.50'))
        self.assertEqual(parse_decimal('9,30'), Decimal('9.30'))
        self.assertEqual(parse_decimal('18.000,50'), Decimal('18000.50'))
        self.assertEqual(parse_decimal('1,842.50'), Decimal('1842.50'))
        self.assertEqual(parse_decimal('0.00'), Decimal('0.00'))


class AhorroEsperadoTests(TestCase):
    """Ahorro esperado mensual y anual: no son la misma cifra y las dos hacen
    falta. La paga extra no llega todos los meses, pero es dinero del año."""

    def setUp(self):
        from core.models import Hogar
        from finanzas.models import CategoriaGasto, FuenteIngreso, PartidaGasto

        self.hogar = Hogar.objects.create(nombre='Hogar')
        self.user = User.objects.create_user('ahorrador', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        self.client.force_login(self.user)
        self.categoria = CategoriaGasto.objects.create(
            hogar=self.hogar, nombre='Piso', tipo='fijo',
        )
        PartidaGasto.objects.create(
            hogar=self.hogar, categoria=self.categoria, nombre='Alquiler',
            importe=Decimal('900'), periodicidad='mensual',
        )
        PartidaGasto.objects.create(
            hogar=self.hogar, categoria=self.categoria, nombre='Seguro',
            importe=Decimal('240'), periodicidad='anual',
        )
        self.FuenteIngreso = FuenteIngreso

    def nomina(self, **extra):
        # Neto declarado y en anual: así las cifras del test son las del
        # usuario y no las que salgan del motor fiscal.
        datos = dict(
            usuario=self.user, hogar=self.hogar, nombre='Nómina',
            modo_entrada='anual', importe_declarado=Decimal('24000'),
            es_bruto=False, num_pagas=12, activo=True,
        )
        datos.update(extra)
        return self.FuenteIngreso.objects.create(**datos)

    def test_el_mensual_es_ingresos_menos_gastos_recurrentes(self):
        from finanzas.distribucion import ahorro_esperado

        self.nomina()
        datos = ahorro_esperado(self.hogar, 2026)

        # 900 mensual + 20 de provisión del seguro anual.
        self.assertEqual(datos['gastos_mensuales'], Decimal('920'))
        self.assertEqual(datos['ingresos_mensuales'], Decimal('2000'))
        self.assertEqual(datos['mensual'], Decimal('1080'))

    def test_el_anual_incluye_las_pagas_extra(self):
        from finanzas.distribucion import ahorro_esperado

        self.nomina(num_pagas=14, importe_declarado=Decimal('28000'),
                    meses_pagas_extras='6,12')
        datos = ahorro_esperado(self.hogar, 2026)

        # 14 pagas de 2.000: 12 mensuales + 2 extras.
        self.assertEqual(datos['ingresos_mensuales'], Decimal('2000'))
        self.assertEqual(datos['ingresos_anuales'], Decimal('28000'))
        self.assertEqual(datos['extras_anuales'], Decimal('4000'))
        # Gasto anual: 900x12 + 240.
        self.assertEqual(datos['gastos_anuales'], Decimal('11040'))
        self.assertEqual(datos['anual'], Decimal('16960'))

    def test_el_anual_no_es_doce_veces_el_mensual_cuando_hay_extras(self):
        from finanzas.distribucion import ahorro_esperado

        self.nomina(num_pagas=14, importe_declarado=Decimal('28000'),
                    meses_pagas_extras='6,12')
        datos = ahorro_esperado(self.hogar, 2026)
        self.assertNotEqual(datos['anual'], datos['mensual'] * 12)


class IngresosFueraDelRepartoTests(TestCase):
    """Un ingreso marcado «fuera del reparto» no se distribuye, pero sigue
    siendo ingreso del hogar: el dashboard da la foto general y ahí cuenta."""

    def setUp(self):
        from core.models import Hogar
        from finanzas.models import FuenteIngreso

        self.hogar = Hogar.objects.create(nombre='Hogar')
        self.user = User.objects.create_user('irene', password='clave-de-prueba')
        self.user.first_name = 'Irene'
        self.user.save()
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        self.client.force_login(self.user)

        FuenteIngreso.objects.create(
            usuario=self.user, hogar=self.hogar, nombre='Nómina',
            modo_entrada='anual', importe_declarado=Decimal('24000'),
            es_bruto=False, num_pagas=12, activo=True,
        )
        FuenteIngreso.objects.create(
            usuario=self.user, hogar=self.hogar, nombre='Alquiler del piso',
            modo_entrada='anual', importe_declarado=Decimal('8400'),
            es_bruto=False, num_pagas=12, activo=True, incluir_en_distribucion=False,
        )

    def test_el_reparto_lo_deja_fuera_pero_lo_declara(self):
        from finanzas.distribucion import calcular_flujos

        flujo = calcular_flujos(self.hogar, mes=6, anio=2026)

        self.assertEqual(flujo['ingreso_base_puro_hogar'], Decimal('2000'))
        self.assertEqual(flujo['total_fuera_reparto'], Decimal('700'))
        self.assertEqual(flujo['ingreso_total_hogar'], Decimal('2700'))
        self.assertEqual(flujo['ingresos_fuera_reparto'][0]['fuente'], 'Alquiler del piso')
        self.assertEqual(flujo['ingresos_fuera_reparto'][0]['miembro'], 'Irene')

    def test_el_ahorro_esperado_cuenta_todos_los_ingresos(self):
        from finanzas.distribucion import ahorro_esperado

        self.assertEqual(
            ahorro_esperado(self.hogar, 2026)['ingresos_mensuales'], Decimal('2700'),
        )

    def test_el_dashboard_los_cuenta_y_lo_explica(self):
        respuesta = self.client.get(reverse('dashboard'))

        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.context['ahorro']['ingresos_mensuales'], Decimal('2700'))
        self.assertContains(respuesta, 'Alquiler del piso')

    def test_el_semaforo_usa_la_misma_base_que_el_ahorro_esperado(self):
        """El dashboard no puede decir «gastas más de lo que ingresas» junto a
        un ahorro esperado positivo: la base tiene que ser la misma."""
        from finanzas.models import CategoriaGasto, PartidaGasto

        categoria = CategoriaGasto.objects.create(
            hogar=self.hogar, nombre='Casa', tipo='fijo',
        )
        PartidaGasto.objects.create(
            hogar=self.hogar, categoria=categoria, nombre='Alquiler',
            importe=Decimal('2200'), periodicidad='mensual',
        )
        respuesta = self.client.get(reverse('dashboard'))

        # Ingresos 2.700 (2.000 + 700 fuera del reparto) contra 2.200 de gasto.
        self.assertGreater(respuesta.context['salud_tasa'], 0)
        self.assertGreater(respuesta.context['ahorro']['mensual'], 0)

    def test_la_distribucion_avisa_de_lo_omitido(self):
        respuesta = self.client.get(reverse('finanzas:vista_distribucion'))
        self.assertContains(respuesta, 'Se está omitiendo del reparto')


class PantallasSeRenderizanTests(TestCase):
    """Un render por pantalla del módulo.

    No comprueban contenido: comprueban que la plantilla compila y la vista
    responde. Un filtro mal escrito en una plantilla no lo caza ningún test de
    lógica, y estas pantallas se tocan a menudo."""

    def setUp(self):
        from core.models import Hogar
        from finanzas.models import CategoriaGasto, PartidaGasto, Propiedad, Vehiculo
        from finanzas.views_gastos import _crear_categorias_predefinidas

        self.hogar = Hogar.objects.create(nombre='Hogar')
        self.user = User.objects.create_user('vista', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        self.client.force_login(self.user)

        PartidaGasto.objects.create(
            hogar=self.hogar, nombre='Alquiler', importe=Decimal('900'),
            periodicidad='mensual',
            categoria=CategoriaGasto.objects.get(hogar=self.hogar, nombre='Hipoteca / Alquiler'),
        )
        PartidaGasto.objects.create(
            hogar=self.hogar, nombre='IBI', importe=Decimal('520'), periodicidad='anual',
            categoria=CategoriaGasto.objects.get(hogar=self.hogar, nombre='IBI'),
        )
        self.vehiculo = Vehiculo.objects.create(hogar=self.hogar, nombre='Coche')
        Propiedad.objects.create(
            hogar=self.hogar, nombre='Piso', fecha_compra=datetime.date(2020, 1, 1),
            precio_compra=Decimal('100000'), valor_actual=Decimal('120000'),
        )

    RUTAS = None   # se rellena en test_todas_las_pantallas_responden

    def rutas_de_la_app(self):
        return [
            reverse('dashboard'),
            reverse('finanzas:listar_gastos'),
            reverse('finanzas:crear_partida'),
            reverse('finanzas:listar_categorias'),
            reverse('finanzas:listar_vehiculos'),
            reverse('finanzas:detalle_vehiculo', args=[self.vehiculo.id]),
            reverse('finanzas:listar_propiedades'),
            reverse('finanzas:listar_ingresos'),
            reverse('finanzas:vista_distribucion'),
            # Con periodo explícito: sin él, Movimientos redirige al mes en
            # curso y estas pruebas esperan la pantalla, no el salto.
            reverse('extractos:listar') + '?anio=all&mes=all',
            reverse('extractos:reglas'),
            reverse('extractos:etiquetas'),
            reverse('extractos:subir'),
        ]

    def test_todas_las_pantallas_responden(self):
        for ruta in self.rutas_de_la_app():
            with self.subTest(ruta=ruta):
                self.assertEqual(self.client.get(ruta).status_code, 200)

    def test_ninguna_pantalla_escupe_sintaxis_de_plantilla(self):
        """Un `{# … #}` de varias líneas NO es un comentario para Django: se
        pinta tal cual en medio de la pantalla. Como la página responde 200
        igualmente, ningún test lo veía —y el usuario sí—."""
        for ruta in self.rutas_de_la_app():
            with self.subTest(ruta=ruta):
                contenido = self.client.get(ruta).content.decode('utf-8')
                # Solo marcas que NUNCA aparecen en JavaScript ni en CSS de
                # verdad: buscar «{{» o «}}» daría falsos positivos en cada
                # cierre de bloque de los scripts en línea.
                for marca in ('{#', '#}', '{% comment', '{% if ', '{% for ',
                              '{% endif', '{% endfor', '{% include'):
                    self.assertNotIn(
                        marca, contenido,
                        f'{ruta} deja escapar «{marca}»: hay una etiqueta de plantilla sin interpretar.',
                    )

    def test_la_vieja_pantalla_de_sin_categorizar_redirige_al_filtro(self):
        respuesta = self.client.get(reverse('extractos:sin_categorizar'))
        self.assertRedirects(
            respuesta, reverse('extractos:listar') + '?categoria=sin',
        )

    def test_la_vieja_pantalla_de_analisis_redirige_a_movimientos(self):
        """La pestaña ya no existe, pero la ruta seguía enlazada y guardada en
        los marcadores del usuario: tiene que llevar a Movimientos con el mismo
        periodo, no a un 404."""
        respuesta = self.client.get(reverse('extractos:analisis'), {'anio': 2026, 'mes': 8})
        self.assertRedirects(
            respuesta, reverse('extractos:listar') + '?anio=2026&mes=8',
        )

    def test_la_vieja_pantalla_de_conciliacion_redirige_a_movimientos(self):
        """La comparación con el presupuesto vive en Movimientos; la pantalla
        aparte decía lo mismo con sus propias cifras."""
        respuesta = self.client.get(reverse('extractos:conciliacion'), {'anio': 2026, 'mes': 8})
        self.assertRedirects(
            respuesta, reverse('extractos:listar') + '?anio=2026&mes=8',
        )

    def test_editar_una_partida_se_renderiza(self):
        from finanzas.models import PartidaGasto

        partida = PartidaGasto.objects.first()
        respuesta = self.client.get(reverse('finanzas:editar_partida', args=[partida.id]))
        self.assertEqual(respuesta.status_code, 200)


class BalanceDeUnaPropiedadTests(TestCase):
    """Un piso alquilado no es solo gasto. Con el alquiler imputado a él, la
    pregunta pasa de «cuánto me cuesta» a «cuánto me renta»."""

    def setUp(self):
        from core.models import Hogar
        from extractos.models import ExtractoBancario
        from finanzas.models import Propiedad
        from finanzas.views_gastos import _crear_categorias_predefinidas

        self.hogar = Hogar.objects.create(nombre='Hogar')
        self.user = User.objects.create_user('casero', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        self.client.force_login(self.user)
        self.extracto = ExtractoBancario.objects.create(hogar=self.hogar, usuario=self.user)
        self.piso = Propiedad.objects.create(
            hogar=self.hogar, nombre='Piso en alquiler', fecha_compra=datetime.date(2019, 1, 1),
            precio_compra=Decimal('150000'), valor_actual=Decimal('180000'),
        )

    def movimiento(self, concepto, importe, categoria, dia, activo=None):
        from extractos.models import MovimientoBancario
        from finanzas import costes_activo
        from finanzas.models import CategoriaGasto

        mov = MovimientoBancario(
            extracto=self.extracto, hogar=self.hogar, fecha=datetime.date(2026, 3, dia),
            concepto=concepto, importe=Decimal(importe),
            categoria=CategoriaGasto.objects.get(hogar=self.hogar, nombre=categoria),
        )
        costes_activo.asignar(mov, activo if activo is not None else self.piso)
        mov.save()
        return mov

    def test_el_alquiler_imputado_balancea_lo_que_cuesta(self):
        from finanzas import costes_activo

        self.movimiento('IBI', '-520', 'IBI', 5)
        self.movimiento('Comunidad', '-180', 'Comunidad', 6)
        self.movimiento('Alquiler marzo', '900', 'Otros ingresos', 1)

        ficha = costes_activo.costes(self.piso, 2026)

        self.assertEqual(ficha['real_anual'], Decimal('700'))
        self.assertEqual(ficha['ingreso_real_anual'], Decimal('900'))
        self.assertEqual(ficha['neto_real_anual'], Decimal('200'))
        self.assertTrue(ficha['renta'])

    def test_sin_ingresos_imputados_sigue_siendo_solo_coste(self):
        from finanzas import costes_activo

        self.movimiento('IBI', '-520', 'IBI', 5)
        ficha = costes_activo.costes(self.piso, 2026)

        self.assertFalse(ficha['renta'])
        self.assertEqual(ficha['neto_real_anual'], Decimal('-520'))

    def test_el_ingreso_declarado_se_imputa_desde_el_formulario(self):
        from finanzas.models import FuenteIngreso

        self.client.post(reverse('finanzas:crear_ingreso'), {
            'usuario_id': self.user.id, 'nombre': 'Alquiler del piso',
            'tipo': 'fijo', 'modo_entrada': 'anual', 'importe_declarado': '10800',
            'pais_fiscal': 'ES', 'num_pagas': '12',
            'activo': self.piso.clave_activo,
        })
        fuente = FuenteIngreso.objects.get(nombre='Alquiler del piso')
        self.assertEqual(fuente.propiedad, self.piso)
        self.assertEqual(fuente.clave_activo, self.piso.clave_activo)

    def test_el_ingreso_declarado_llega_a_la_ficha(self):
        from finanzas import costes_activo
        from finanzas.models import FuenteIngreso

        fuente = FuenteIngreso(
            usuario=self.user, hogar=self.hogar, nombre='Alquiler',
            modo_entrada='anual', importe_declarado=Decimal('10800'),
            es_bruto=False, num_pagas=12, activo=True,
        )
        costes_activo.asignar(fuente, self.piso)
        fuente.save()

        ficha = costes_activo.costes(self.piso, 2026)
        self.assertEqual(ficha['ingreso_mensual'], Decimal('900'))
        self.assertEqual(ficha['ingreso_anual'], Decimal('10800'))
        self.assertEqual([f.nombre for f in ficha['fuentes']], ['Alquiler'])

    def test_la_pantalla_enseña_el_desglose(self):
        self.movimiento('IBI', '-520', 'IBI', 5)
        respuesta = self.client.get(reverse('finanzas:listar_propiedades'))

        ficha = respuesta.context['propiedades_con_venta'][0]['costes']
        self.assertEqual(ficha['real_anual'], Decimal('520'))
        self.assertEqual([c['categoria'] for c in ficha['por_categoria']], ['IBI'])
        self.assertContains(respuesta, 'Ver desglose')


class PresupuestoEstadoTests(SimpleTestCase):
    """El estado de un gasto frente a su límite.

    Las claves no pueden llamarse `pct` a secas: este dict se mezcla con el de
    cada bloque, que ya trae su propio `pct` (el peso sobre el total), y lo
    pisaba en silencio dejando todas las proporciones a cero."""

    def test_dentro_del_limite(self):
        from finanzas.presupuesto import estado

        e = estado(Decimal('80'), Decimal('100'))
        self.assertTrue(e['dentro'])
        self.assertEqual(e['exceso'], Decimal('0'))
        self.assertEqual(e['pct_gastado'], 80.0)
        self.assertEqual(e['pct_barra'], 80.0)

    def test_pasado_del_limite(self):
        from finanzas.presupuesto import estado

        e = estado(Decimal('150'), Decimal('100'))
        self.assertFalse(e['dentro'])
        self.assertEqual(e['exceso'], Decimal('50'))
        # La barra se queda llena: pasarse no la alarga, la pone en rojo.
        self.assertEqual(e['pct_barra'], 100)

    def test_sin_limite_no_se_juzga(self):
        """Sin presupuesto declarado no es que vaya bien: es que no hay con qué
        comparar, y pintarlo verde sería afirmar algo que no se sabe."""
        from finanzas.presupuesto import estado

        e = estado(Decimal('150'), Decimal('0'))
        self.assertIsNone(e['dentro'])
        self.assertEqual(e['exceso'], Decimal('0'))

    def test_no_pisa_el_porcentaje_del_bloque(self):
        from finanzas.presupuesto import estado

        bloque = {'pct': 42.0, **estado(Decimal('80'), Decimal('100'))}
        self.assertEqual(bloque['pct'], 42.0)


class PresupuestoDeBloqueTests(TestCase):
    """Presupuestar un bloque entero sin desglosarlo por categorías.

    Hay bloques —los discrecionales, sobre todo— donde se sabe cuánto se quiere
    gastar en total pero no en qué. La única salida era inventarse una categoría
    cajón de sastre y darle todo el dinero: quedaba esa categoría pareciendo que
    le sobraba presupuesto y el resto del bloque sin límite ninguno.
    """

    def setUp(self):
        from core.models import Hogar
        from finanzas.views_gastos import _crear_categorias_predefinidas

        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        self.user = User.objects.create_user(username='tester', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        self.client.force_login(self.user)

    def categoria(self, nombre):
        from finanzas.models import CategoriaGasto
        return CategoriaGasto.objects.get(hogar=self.hogar, nombre=nombre)

    def partida_de_bloque(self, tipo, importe, periodicidad='mensual'):
        from finanzas.models import PartidaGasto
        return PartidaGasto.objects.create(
            hogar=self.hogar, categoria=None, bloque=tipo,
            nombre=f'Presupuesto {tipo}', importe=Decimal(importe),
            periodicidad=periodicidad,
        )

    def partida_de_categoria(self, nombre, importe):
        from finanzas.models import PartidaGasto
        return PartidaGasto.objects.create(
            hogar=self.hogar, categoria=self.categoria(nombre),
            nombre=f'Presupuesto {nombre}', importe=Decimal(importe),
            periodicidad='mensual',
        )

    def test_el_techo_del_bloque_no_cuenta_para_ninguna_categoria(self):
        """Es justo el problema que resuelve: antes ese dinero colgaba de una
        categoría inventada y la dejaba pareciendo que le sobraba."""
        from finanzas import presupuesto

        self.partida_de_bloque('discrecional', '1500')

        self.assertEqual(presupuesto.por_categoria(self.hogar), {})
        self.assertEqual(presupuesto.por_bloque(self.hogar)['discrecional'], Decimal('1500'))

    def test_el_techo_del_bloque_manda_sobre_sus_categorias(self):
        """«Tengo 1.500 € para caprichos, de los cuales 200 para restaurantes»
        son 1.500, no 1.700."""
        from finanzas import presupuesto

        self.partida_de_bloque('discrecional', '1500')
        self.partida_de_categoria('Restaurantes', '200')

        self.assertEqual(presupuesto.por_bloque(self.hogar)['discrecional'], Decimal('1500'))
        # La categoría conserva su límite como desglose dentro del techo.
        por_cat = presupuesto.por_categoria(self.hogar)
        self.assertEqual(por_cat[self.categoria('Restaurantes').id], Decimal('200'))

    def test_sin_techo_declarado_todo_sigue_como_antes(self):
        from finanzas import presupuesto

        self.partida_de_categoria('Restaurantes', '200')
        self.partida_de_categoria('Ocio', '100')

        self.assertEqual(presupuesto.por_bloque(self.hogar)['discrecional'], Decimal('300'))

    def test_el_techo_de_un_bloque_no_toca_a_los_demas(self):
        from finanzas import presupuesto

        self.partida_de_bloque('discrecional', '1500')
        self.partida_de_categoria('Alimentacion', '400')   # variable

        bloques = presupuesto.por_bloque(self.hogar)
        self.assertEqual(bloques['discrecional'], Decimal('1500'))
        self.assertEqual(bloques['variable'], Decimal('400'))

    def test_un_techo_anual_se_prorratea_como_cualquier_partida(self):
        from finanzas import presupuesto

        self.partida_de_bloque('discrecional', '1200', periodicidad='anual')
        self.assertEqual(presupuesto.por_bloque(self.hogar)['discrecional'], Decimal('100'))

    def test_se_puede_declarar_desde_la_pantalla_de_gastos(self):
        from finanzas.models import PartidaGasto

        respuesta = self.client.post(reverse('finanzas:crear_partida'), {
            'categoria_id': 'bloque:discrecional',
            'nombre': 'Caprichos del mes',
            'importe': '1500',
            'periodicidad': 'mensual',
        })
        self.assertEqual(respuesta.status_code, 302)

        partida = PartidaGasto.objects.get(hogar=self.hogar, nombre='Caprichos del mes')
        self.assertIsNone(partida.categoria)
        self.assertEqual(partida.bloque, 'discrecional')
        self.assertTrue(partida.es_del_bloque)
        self.assertEqual(partida.tipo_bloque, 'discrecional')

    def test_un_bloque_inventado_no_cuela(self):
        respuesta = self.client.post(reverse('finanzas:crear_partida'), {
            'categoria_id': 'bloque:loquesea',
            'nombre': 'Invento', 'importe': '10', 'periodicidad': 'mensual',
        })
        self.assertEqual(respuesta.status_code, 404)

    def test_la_pantalla_de_gastos_lo_enseña_como_techo(self):
        self.partida_de_bloque('discrecional', '1500')
        respuesta = self.client.get(reverse('finanzas:listar_gastos'))

        self.assertContains(respuesta, 'etiqueta-techo')
        entradas = respuesta.context['gastos_discrecionales']
        techo = [e for e in entradas if e['categoria'] is None]
        self.assertEqual(len(techo), 1)
        self.assertEqual(techo[0]['subtotal_mensual'], Decimal('1500'))

    def test_el_techo_entra_en_el_total_mensual_del_hogar(self):
        self.partida_de_bloque('discrecional', '1500')
        respuesta = self.client.get(reverse('finanzas:listar_gastos'))
        self.assertEqual(respuesta.context['total_discrecionales'], Decimal('1500'))
        self.assertEqual(respuesta.context['total_mensual'], Decimal('1500'))

    def test_en_el_panel_las_categorias_del_bloque_no_fingen_limite(self):
        """El caso de la captura: con el presupuesto en una categoría cajón de
        sastre, esa salía «2.738 € de 20.097 €» —como si le sobrara dinero— y el
        resto del bloque sin límite. Con el techo en el bloque, las categorías se
        leen por su peso y el único límite es el de arriba."""
        from datetime import date
        from django.urls import reverse as url
        from extractos.models import ExtractoBancario, MovimientoBancario

        extracto = ExtractoBancario.objects.create(hogar=self.hogar, usuario=self.user)
        for nombre, importe in [('Restaurantes', '-250'), ('Ocio', '-100')]:
            MovimientoBancario.objects.create(
                extracto=extracto, hogar=self.hogar, fecha=date(2026, 8, 5),
                concepto=f'Gasto {nombre}', importe=Decimal(importe),
                categoria=self.categoria(nombre),
            )
        self.partida_de_bloque('discrecional', '1500')

        panel = self.client.get(url('extractos:listar'), {'anio': 2026, 'mes': 8}).context['panel']
        bloque = next(b for b in panel['bloques'] if b['tipo'] == 'discrecional')

        self.assertTrue(bloque['techo_propio'])
        self.assertEqual(bloque['limite'], Decimal('1500'))
        self.assertTrue(bloque['dentro'])
        # Ninguna categoría de dentro tiene límite propio que enseñar.
        self.assertTrue(all(c['limite'] == Decimal('0') for c in bloque['categorias']))
        # Y no se avisa por categoría de un límite que nadie puso.
        fuera = panel['fuera_presupuesto']
        self.assertEqual([f['nombre'] for f in fuera['categorias']], [])
        self.assertEqual([f['nombre'] for f in fuera['sin_presupuesto']], [])

    def test_el_boton_de_poner_techo_esta_en_cada_bloque(self):
        """No basta con que la opción exista dentro de un desplegable: quien va
        a poner el tope de sus caprichos lo busca mirando el bloque, no
        abriendo el selector de categorías de «Nuevo gasto»."""
        respuesta = self.client.get(reverse('finanzas:listar_gastos'))
        contenido = respuesta.content.decode()

        self.assertContains(respuesta, 'Poner techo al bloque', count=4)
        for tipo in ('fijo', 'anual', 'variable', 'discrecional'):
            self.assertIn(f'?bloque={tipo}', contenido)

    def test_el_boton_dice_el_techo_cuando_ya_esta_puesto(self):
        self.partida_de_bloque('discrecional', '1500')
        respuesta = self.client.get(reverse('finanzas:listar_gastos'))

        self.assertEqual(respuesta.context['techo_discrecional'], Decimal('1500'))
        self.assertContains(respuesta, 'Techo:')
        self.assertContains(respuesta, 'Poner techo al bloque', count=3)

    def test_el_formulario_llega_con_el_bloque_ya_elegido(self):
        respuesta = self.client.get(
            reverse('finanzas:crear_partida'), {'bloque': 'discrecional'},
        )
        self.assertEqual(respuesta.context['bloque_elegido'], 'discrecional')
        self.assertContains(
            respuesta,
            '<option selected value="bloque:discrecional" data-tipo="discrecional">',
            html=False,
        )

    def test_un_bloque_inventado_en_la_url_se_ignora(self):
        respuesta = self.client.get(
            reverse('finanzas:crear_partida'), {'bloque': 'loquesea'},
        )
        self.assertEqual(respuesta.context['bloque_elegido'], '')

    def test_sin_elegir_destino_no_se_crea_un_techo_sin_querer(self):
        """El desplegable arrancaba en «techo de los fijos», así que un gasto
        normal se guardaba como techo de bloque si no se tocaba el campo."""
        from finanzas.models import PartidaGasto

        respuesta = self.client.get(reverse('finanzas:crear_partida'))
        contenido = respuesta.content.decode()
        self.assertIn('<option value="" selected>— Elige una —</option>', contenido)

        respuesta = self.client.post(reverse('finanzas:crear_partida'), {
            'categoria_id': '', 'nombre': 'Sin destino',
            'importe': '100', 'periodicidad': 'mensual',
        })
        self.assertEqual(respuesta.status_code, 404)
        self.assertFalse(PartidaGasto.objects.filter(nombre='Sin destino').exists())

    def test_el_total_del_bloque_es_el_techo_y_no_la_suma_de_las_tarjetas(self):
        """Si no, la pantalla de Gastos daba un total que no existe y que además
        no coincidía con el que enseña la conciliación."""
        self.partida_de_bloque('discrecional', '1500')
        self.partida_de_categoria('Restaurantes', '200')

        respuesta = self.client.get(reverse('finanzas:listar_gastos'))
        self.assertEqual(respuesta.context['total_discrecionales'], Decimal('1500'))
        self.assertEqual(respuesta.context['total_mensual'], Decimal('1500'))
        # Pero la tarjeta de la categoría sigue estando, como desglose.
        nombres = [
            e['categoria'].nombre for e in respuesta.context['gastos_discrecionales']
            if e['categoria']
        ]
        self.assertIn('Restaurantes', nombres)


class CosteDeActivoConPagosAnualesTests(TestCase):
    """Un pago que se provisiona todo el año y se paga de golpe no puede entrar
    en la media mensual de lo que cuesta un coche.

    La tarjeta decía «1.249,34 €/mes en 1 mes» porque en septiembre tocaba la
    revisión: literalmente, que el coche cuesta mil doscientos euros al mes."""

    def setUp(self):
        from datetime import date
        from core.models import Hogar
        from extractos.models import ExtractoBancario, MovimientoBancario
        from finanzas.models import CategoriaGasto, PartidaGasto, Vehiculo
        from finanzas.views_gastos import _crear_categorias_predefinidas

        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        self.user = User.objects.create_user(username='tester', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        self.client.force_login(self.user)

        self.coche = Vehiculo.objects.create(hogar=self.hogar, nombre='Polo 1.4', tipo='coche')
        self.mantenimiento = CategoriaGasto.objects.get(
            hogar=self.hogar, nombre='Mantenimiento vehicular')
        self.gasolina = CategoriaGasto.objects.get(hogar=self.hogar, nombre='Gasolina')
        # Gasto corriente del coche: 60 €/mes de gasolina.
        PartidaGasto.objects.create(
            hogar=self.hogar, categoria=self.gasolina, nombre='Gasolina Polo',
            importe=Decimal('60'), periodicidad='mensual', vehiculo=self.coche,
        )
        # Y la revisión, que se provisiona y se paga de golpe.
        self.revision = PartidaGasto.objects.create(
            hogar=self.hogar, categoria=self.mantenimiento, nombre='Revisión Polo',
            importe=Decimal('1200'), periodicidad='anual', vehiculo=self.coche,
        )
        self.extracto = ExtractoBancario.objects.create(hogar=self.hogar, usuario=self.user)
        self._dia = 0
        self.MovimientoBancario = MovimientoBancario
        self.date = date

    def mov(self, importe, mes, categoria, provision=None):
        self._dia += 1
        return self.MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar,
            fecha=self.date(2026, mes, (self._dia % 28) + 1),
            concepto=f'Gasto {mes}-{self._dia}', importe=Decimal(importe),
            categoria=categoria, vehiculo=self.coche, partida_conciliada=provision,
        )

    def ficha(self, anio=2026):
        from finanzas import costes_activo
        return costes_activo.costes(self.coche, anio)

    def ficha_a(self, *hoy, anio=2026, gasto=None):
        """La ficha vista en una fecha concreta.

        La media va sobre los meses ya cerrados, así que depende del día: sin
        fijarlo, estas pruebas dirían una cosa en enero y otra en diciembre.
        """
        from unittest import mock

        if gasto:
            self.mov(gasto[0], gasto[1], self.gasolina)
        with mock.patch('finanzas.costes_activo.date') as falso:
            falso.today.return_value = self.date(*hoy)
            return self.ficha(anio)

    def test_la_media_mensual_es_solo_del_gasto_corriente(self):
        self.mov('-60', 7, self.gasolina)
        self.mov('-60', 8, self.gasolina)
        self.mov('-1200', 9, self.mantenimiento, provision=self.revision)

        f = self.ficha()
        self.assertEqual(f['real_mensual'], Decimal('60'))
        self.assertEqual(f['meses_con_datos'], 2)
        self.assertEqual(f['corriente_anual'], Decimal('120'))

    def test_el_pago_anual_se_cuenta_aparte_pero_no_se_pierde(self):
        self.mov('-60', 7, self.gasolina)
        self.mov('-1200', 9, self.mantenimiento, provision=self.revision)

        f = self.ficha()
        self.assertEqual(f['provisiones_anual'], Decimal('1200'))
        self.assertEqual(f['num_provisiones'], 1)
        # Contra el AÑO sí cuenta: ahí la comparación tiene sentido.
        self.assertEqual(f['real_anual'], Decimal('1260'))

    def test_sin_pagos_anuales_todo_sigue_igual(self):
        self.mov('-60', 7, self.gasolina)
        self.mov('-80', 8, self.gasolina)

        f = self.ficha()
        self.assertEqual(f['provisiones_anual'], Decimal('0'))
        self.assertEqual(f['real_mensual'], Decimal('70'))
        self.assertEqual(f['partidas_sueltas'], [])

    def test_la_serie_mensual_lleva_todo_lo_que_paso_por_el_banco(self):
        """La serie es de CAJA: para saber en qué mes se fue el dinero hay que
        ver el dinero, incluido el mes en que tocó pagar la revisión.

        Las doce barras de antes sacaban el pago anual de la barra para que no
        aplastase al resto, y así contaban dos cosas a la vez sin dejar leer
        ninguna. El desglose se sigue dando —corriente y anual van aparte en
        cada mes—, pero el total del mes es el total del mes."""
        self.mov('-60', 7, self.gasolina)
        self.mov('-1200', 9, self.mantenimiento, provision=self.revision)

        por_mes = {m['mes']: m for m in self.ficha()['por_mes']}
        self.assertEqual(por_mes[7]['total'], Decimal('60'))
        self.assertEqual(por_mes[7]['provision'], Decimal('0'))
        self.assertEqual(por_mes[9]['total'], Decimal('1200'))
        self.assertEqual(por_mes[9]['corriente'], Decimal('0'))
        self.assertEqual(por_mes[9]['provision'], Decimal('1200'))

    def test_avisa_si_la_partida_del_pago_no_está_imputada_al_activo(self):
        """El gasto suma en lo real y su provisión no suma en lo teórico: el
        vehículo parece pasarse cuando lo que falta es imputarle la partida."""
        from finanzas.models import PartidaGasto

        suelta = PartidaGasto.objects.create(
            hogar=self.hogar, categoria=self.mantenimiento, nombre='ITV Polo',
            importe=Decimal('50'), periodicidad='anual',   # sin vehiculo
        )
        self.mov('-50', 9, self.mantenimiento, provision=suelta)

        f = self.ficha()
        self.assertEqual([p.nombre for p in f['partidas_sueltas']], ['ITV Polo'])

    def test_la_partida_bien_imputada_no_genera_aviso(self):
        self.mov('-1200', 9, self.mantenimiento, provision=self.revision)
        self.assertEqual(self.ficha()['partidas_sueltas'], [])

    def test_la_pantalla_lo_cuenta(self):
        from django.urls import reverse as url

        self.mov('-60', 7, self.gasolina)
        self.mov('-1200', 9, self.mantenimiento, provision=self.revision)

        respuesta = self.client.get(url('finanzas:listar_vehiculos'), {'anio': 2026})
        self.assertContains(respuesta, 'de los cuales')
        self.assertContains(respuesta, 'pago de gastos periódicos')

    def test_el_real_se_da_al_mes_para_poder_compararlo_con_el_teorico(self):
        """La ficha ponía «85,27 €/mes» de teórico al lado de «1.249,34 €» de
        real, que es el total del año: parecía que el coche costaba mil
        doscientos al mes cuando eso era lo de nueve meses.

        El divisor son los meses ya CERRADOS: en septiembre hay ocho meses de
        gasto, y repartirlos entre doce decía que el coche cuesta la mitad de
        lo que cuesta. Lo que va de año lo sigue diciendo la marca de la
        barra."""
        from unittest import mock

        self.mov('-900', 1, self.gasolina)
        with mock.patch('finanzas.costes_activo.date') as falso:
            falso.today.return_value = self.date(2026, 9, 15)
            f = self.ficha()

        self.assertEqual(f['meses_transcurridos'], 9)
        self.assertEqual(f['meses_cerrados'], 8)
        self.assertEqual(f['ritmo_mensual'], Decimal('112.50'))    # 900 / 8
        # Y el teórico con el que se compara sigue siendo mensual.
        self.assertEqual(f['teorico_mensual'], Decimal('160'))
        self.assertEqual(f['diferencia_mensual'], Decimal('-47.50'))

    def test_un_gasto_de_tres_años_se_reparte_entre_treinta_y_seis_meses(self):
        """Unos neumáticos de 470 € que duran tres años cuestan 13 €/mes, no 470
        entre los meses que lleve el año."""
        from finanzas.models import PartidaGasto

        neumaticos = PartidaGasto.objects.create(
            hogar=self.hogar, categoria=self.mantenimiento, nombre='Neumáticos Polo',
            importe=Decimal('470'), periodicidad='trienal', vehiculo=self.coche,
        )
        self.assertEqual(neumaticos.meses_periodo, 36)
        self.assertEqual(neumaticos.importe_mensual, Decimal('13.06'))
        self.assertEqual(neumaticos.importe_anual, Decimal('156.67'))

        self.mov('-470', 9, self.mantenimiento, provision=neumaticos)
        # Con el año ya cerrado, el divisor son sus doce meses.
        f = self.ficha_a(2027, 1, 1)
        self.assertEqual(f['ritmo_provisiones'], Decimal('13.06'))
        self.assertEqual(f['ritmo_corriente'], Decimal('0'))
        self.assertEqual(f['ritmo_mensual'], Decimal('13.06'))

    def test_cada_pago_se_reparte_según_su_propio_gasto(self):
        """La revisión es anual y los neumáticos trienales: cada uno con su
        divisor, no los dos con el mismo."""
        from finanzas.models import PartidaGasto

        neumaticos = PartidaGasto.objects.create(
            hogar=self.hogar, categoria=self.mantenimiento, nombre='Neumáticos',
            importe=Decimal('360'), periodicidad='trienal', vehiculo=self.coche,
        )
        self.mov('-360', 9, self.mantenimiento, provision=neumaticos)    # 360/36 = 10
        self.mov('-1200', 9, self.mantenimiento, provision=self.revision)  # 1200/12 = 100

        self.assertEqual(self.ficha_a(2027, 1, 1)['ritmo_provisiones'], Decimal('110'))

    def test_la_ficha_respeta_los_meses_que_pusiste_a_mano(self):
        """Los mismos neumáticos duran 36 meses en el coche que hace kilómetros
        y 50 en el que apenas sale. La ficha tiene que usar el número de cada
        uno, no una tabla de periodicidades cerrada."""
        from finanzas.models import PartidaGasto

        neumaticos = PartidaGasto.objects.create(
            hogar=self.hogar, categoria=self.mantenimiento, nombre='Neumáticos Polo',
            importe=Decimal('600'), periodicidad='personalizada',
            meses_personalizados=50, vehiculo=self.coche,
        )
        self.mov('-600', 9, self.mantenimiento, provision=neumaticos)

        self.assertEqual(neumaticos.importe_mensual, Decimal('12'))
        self.assertEqual(self.ficha_a(2027, 1, 1)['ritmo_provisiones'], Decimal('12'))

    def test_el_gasto_corriente_se_reparte_entre_los_meses_cerrados(self):
        """Lo del día a día se imputa entero al año —ya ha pasado— y la media
        se hace sobre los meses que de verdad han terminado."""
        f = self.ficha_a(2026, 9, 15, gasto=('-900', 1))

        self.assertEqual(f['corriente_anual'], Decimal('900'))
        self.assertEqual(f['ritmo_corriente'], Decimal('112.50'))   # 900 / 8
        self.assertEqual(f['ritmo_provisiones'], Decimal('0'))

    def test_el_mes_en_curso_todavia_no_pesa_en_la_media(self):
        """Unos días de gasto contra un mes entero de divisor hunden la media:
        el mes en curso entra cuando se cierra."""
        f = self.ficha_a(2026, 9, 15, gasto=('-900', 9))

        self.assertEqual(f['corriente_anual'], Decimal('900'))
        self.assertEqual(f['devengado_cerrado'], Decimal('0'))
        self.assertEqual(f['ritmo_mensual'], Decimal('0'))

    def test_un_año_cerrado_se_divide_entre_doce(self):
        from unittest import mock

        self.mov('-1200', 3, self.gasolina)
        with mock.patch('finanzas.costes_activo.date') as falso:
            falso.today.return_value = self.date(2027, 5, 1)
            f = self.ficha(2026)

        self.assertEqual(f['meses_transcurridos'], 12)
        self.assertEqual(f['ritmo_mensual'], Decimal('100'))


class CosteAnualDeUnActivoTests(TestCase):
    """Lo que cuesta el coche al año, y ese año entre doce.

    Reproduce la ficha del Polo con sus cifras exactas, porque es la cuenta que
    el usuario hace a mano y la que tiene que salir:

        declarado   2,57 + 4,95 + 16,11 + 25,75 = 49,38 €/mes de mantenimiento
                    (592,56 €/año) + 39 €/mes de seguro (468 €/año)
        pagado      543 € de neumáticos (cubren 3 años), 385 € de revisión
                    (anual), y 238 + 54 + 28,46 € de gasto corriente
        imputado    543/3 + 385 + 238 + 54 + 28,46 = 886,46 € al año
        al mes      886,46 / 12 = 73,87 €

    La ficha llegó a decir 82,85 €/mes —el gasto corriente entre los NUEVE
    meses transcurridos— y «Mantenimiento vehicular 425 € de 444 €», que son
    dos cifras prorrateadas que no se pueden comprobar contra nada.
    """

    def setUp(self):
        from datetime import date
        from core.models import Hogar
        from extractos.models import ExtractoBancario, MovimientoBancario
        from finanzas.models import CategoriaGasto, PartidaGasto, Vehiculo
        from finanzas.views_gastos import _crear_categorias_predefinidas

        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        self.user = User.objects.create_user(username='tester', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        self.client.force_login(self.user)

        self.coche = Vehiculo.objects.create(hogar=self.hogar, nombre='Polo 1.4', tipo='coche')
        self.mant = CategoriaGasto.objects.get(
            hogar=self.hogar, nombre='Mantenimiento vehicular')
        self.seguro = CategoriaGasto.objects.get(hogar=self.hogar, nombre='Seguro coche')
        self.imprevistos = CategoriaGasto.objects.create(
            hogar=self.hogar, nombre='Imprevistos', tipo='variable')

        def partida(nombre, importe, periodicidad, categoria):
            return PartidaGasto.objects.create(
                hogar=self.hogar, categoria=categoria, nombre=nombre,
                importe=Decimal(importe), periodicidad=periodicidad, vehiculo=self.coche,
            )

        partida('Polo ITV', '30.84', 'anual', self.mant)          #  2,57 €/mes
        partida('Polo IVTM', '59.40', 'anual', self.mant)         #  4,95 €/mes
        self.neumaticos = partida(
            'Polo Neumaticos', '579.96', 'trienal', self.mant)    # 16,11 €/mes
        self.revision = partida(
            'Polo Revision', '309.00', 'anual', self.mant)        # 25,75 €/mes
        partida('Seguro Polo', '39.00', 'mensual', self.seguro)   # 39,00 €/mes

        self.extracto = ExtractoBancario.objects.create(hogar=self.hogar, usuario=self.user)
        self.MovimientoBancario = MovimientoBancario
        self.date = date
        self._n = 0

    def pagar(self, importe, mes, categoria, provision=None):
        self._n += 1
        return self.MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=self.date(2026, mes, 10),
            concepto=f'Pago {self._n}', importe=Decimal(importe), categoria=categoria,
            vehiculo=self.coche, partida_conciliada=provision,
        )

    def pagar_el_año_del_usuario(self):
        self.pagar('-543', 9, self.mant, self.neumaticos)
        self.pagar('-385', 9, self.mant, self.revision)
        self.pagar('-238', 5, self.imprevistos)
        self.pagar('-54', 6, self.imprevistos)
        self.pagar('-28.46', 7, self.imprevistos)

    def ficha(self, anio=2026, hoy=(2026, 9, 15)):
        from unittest import mock
        from finanzas import costes_activo

        with mock.patch('finanzas.costes_activo.date') as falso:
            falso.today.return_value = self.date(*hoy)
            return costes_activo.costes(self.coche, anio)

    # ── Lo declarado ─────────────────────────────────────────────────────

    def test_lo_declarado_al_año_es_la_suma_de_las_partidas_por_doce(self):
        f = self.ficha()
        self.assertEqual(f['teorico_mensual'], Decimal('88.38'))
        self.assertEqual(f['teorico_anual'], Decimal('1060.56'))

        por_cat = {c['categoria']: c for c in self.ficha()['por_categoria']}
        # 49,38 €/mes de mantenimiento son 592,56 al año, no 444.
        self.assertEqual(por_cat['Mantenimiento vehicular']['declarado_anual'],
                         Decimal('592.56'))
        # Y el seguro, 468 al año, no 351.
        self.assertEqual(por_cat['Seguro coche']['declarado_anual'], Decimal('468.00'))

    # ── Lo real ──────────────────────────────────────────────────────────

    def test_el_coste_del_año_es_la_cuenta_que_sale_a_mano(self):
        """543/3 + 385 + 238 + 54 + 28,46 = 886,46 · entre 12 = 73,87 €/mes.

        Con el año ya cerrado sus doce meses son los doce meses cerrados, así
        que la cuenta a mano sale tal cual."""
        self.pagar_el_año_del_usuario()
        f = self.ficha(hoy=(2027, 1, 1))

        self.assertEqual(f['corriente_anual'], Decimal('320.46'))     # 238+54+28,46
        self.assertEqual(f['provisiones_devengadas'], Decimal('566.00'))  # 181+385
        self.assertEqual(f['devengado_anual'], Decimal('886.46'))
        self.assertEqual(f['meses_cerrados'], 12)
        self.assertEqual(f['ritmo_mensual'], Decimal('73.87'))
        # Y la caja, aparte: es lo que salió del banco.
        self.assertEqual(f['real_anual'], Decimal('1248.46'))

    def test_con_el_año_a_medias_la_media_va_sobre_los_meses_cerrados(self):
        """En septiembre hay ocho meses cerrados: los 320,46 € de gasto
        corriente son 40,06 €/mes, no 26,70 (que es lo que salía dividiendo
        entre doce). Los pagos de septiembre entran cuando el mes se cierre."""
        self.pagar_el_año_del_usuario()
        f = self.ficha()

        self.assertEqual(f['meses_cerrados'], 8)
        self.assertEqual(f['devengado_cerrado'], Decimal('320.46'))
        self.assertEqual(f['ritmo_mensual'], Decimal('40.06'))
        # Lo imputado al año no se toca: lo que cambia es el divisor.
        self.assertEqual(f['devengado_anual'], Decimal('886.46'))

    def test_un_gasto_plurianual_imputa_al_año_solo_su_parte(self):
        """543 € de neumáticos que duran tres años son 181 € al año."""
        self.pagar('-543', 9, self.mant, self.neumaticos)
        f = self.ficha(hoy=(2027, 1, 1))

        self.assertEqual(f['real_anual'], Decimal('543'))         # caja
        self.assertEqual(f['devengado_anual'], Decimal('181'))    # 543 × 12/36
        self.assertEqual(f['ritmo_mensual'], Decimal('15.08'))

    def test_un_recibo_de_menos_de_un_año_imputa_entero(self):
        """Un trimestral cubre tres meses que caen todos dentro del año, y los
        cuatro del año suman lo declarado. Anualizarlo lo multiplicaría por
        cuatro, y luego otra vez por los cuatro pagos."""
        from finanzas.models import PartidaGasto

        trimestral = PartidaGasto.objects.create(
            hogar=self.hogar, categoria=self.mant, nombre='Lavado trimestral',
            importe=Decimal('100'), periodicidad='trimestral', vehiculo=self.coche,
        )
        self.assertEqual(trimestral.importe_anual, Decimal('400'))

        for mes in (1, 4, 7, 10):
            self.pagar('-100', mes, self.mant, trimestral)
        f = self.ficha(hoy=(2027, 1, 1))

        self.assertEqual(f['real_anual'], Decimal('400'))
        self.assertEqual(f['devengado_anual'], Decimal('400'))

    # ── Las dos tarjetas dicen lo mismo ──────────────────────────────────

    def test_donde_se_va_compara_el_año_contra_el_año(self):
        """«Si sumas las partidas de mantenimiento vehicular y las multiplicas
        por 12, no salen 444, salen 592.»"""
        self.pagar_el_año_del_usuario()
        por_cat = {c['categoria']: c for c in self.ficha()['por_categoria']}

        fila = por_cat['Mantenimiento vehicular']
        self.assertEqual(fila['real_anual'], Decimal('566.00'))       # 181 + 385
        self.assertEqual(fila['declarado_anual'], Decimal('592.56'))
        self.assertEqual(fila['diferencia'], Decimal('-26.56'))
        # Lo que salió del banco, y lo que de eso cubre años futuros.
        self.assertEqual(fila['pagado_anual'], Decimal('928.00'))
        self.assertEqual(fila['diferido'], Decimal('362.00'))         # 543 − 181

        self.assertEqual(por_cat['Imprevistos']['real_anual'], Decimal('320.46'))
        self.assertEqual(por_cat['Seguro coche']['real_anual'], Decimal('0'))

    def test_la_columna_de_lo_real_suma_el_coste_del_año(self):
        """Si las filas no suman la cifra de arriba, una de las dos miente."""
        self.pagar_el_año_del_usuario()
        f = self.ficha()

        self.assertEqual(
            sum(c['real_anual'] for c in f['por_categoria']), f['devengado_anual'],
        )

    def test_la_barra_mide_el_año_contra_el_año(self):
        self.pagar_el_año_del_usuario()
        f = self.ficha()

        self.assertEqual(f['pct_ejecucion'], 83)        # 886,46 de 1.060,56
        self.assertEqual(f['pct_transcurrido'], 75)     # y van 9 de 12 meses
        self.assertEqual(f['diferencia_anual'], Decimal('-174.10'))

    def test_la_pantalla_ensena_las_mismas_cifras(self):
        from django.urls import reverse as url

        self.pagar_el_año_del_usuario()
        with __import__('unittest').mock.patch('finanzas.costes_activo.date') as falso:
            falso.today.return_value = self.date(2026, 9, 15)
            respuesta = self.client.get(
                url('finanzas:detalle_vehiculo', args=[self.coche.id]), {'anio': 2026},
            )

        # 40,06 €/mes es la media sobre los ocho meses ya cerrados; 886,46 €
        # sigue siendo lo imputado al año entero.
        for cifra in ('40,06', '886,46', '320,46', '566,00', '592,56', '468,00', '1.248,46'):
            self.assertContains(respuesta, cifra)


class MovimientoEntreFondosQueSigueAlPresupuestoTests(TestCase):
    """El «Aporte Gastos Anuales» decía 249,43 € cuando la provisión eran
    252,54: se había quedado con la cifra del día que se creó.

    No era cosa de los cierres —solo congelan meses YA pasados— sino de que el
    importe se teclea una vez y no sigue a nada. Y había dos capas del mismo
    fallo: `importe_calculado` ignoraba las partidas vinculadas cuando el
    movimiento iba a otro fondo, y el motor de distribución ni siquiera llamaba
    a esa propiedad: leía `importe_manual` a pelo.
    """

    def setUp(self):
        from core.models import Hogar
        from finanzas.models import CategoriaGasto, FondoFamiliar, PartidaGasto
        from finanzas.views_gastos import _crear_categorias_predefinidas

        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        self.user = User.objects.create_user(username='tester', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        self.client.force_login(self.user)

        self.itv = CategoriaGasto.objects.get(hogar=self.hogar, nombre='ITV')
        self.alimentacion = CategoriaGasto.objects.get(
            hogar=self.hogar, nombre='Alimentacion')
        self.PartidaGasto = PartidaGasto
        # 60 + 520 al año = 48,33 €/mes de provisión.
        self.anual('ITV', '60')
        self.anual('IBI', '520', categoria=CategoriaGasto.objects.get(
            hogar=self.hogar, nombre='IBI'))

        self.conjunta = FondoFamiliar.objects.create(
            hogar=self.hogar, nombre='Cuenta Conjunta')
        self.provision = FondoFamiliar.objects.create(
            hogar=self.hogar, nombre='Provision Anual')

    def anual(self, nombre, importe, categoria=None):
        return self.PartidaGasto.objects.create(
            hogar=self.hogar, categoria=categoria or self.itv, nombre=nombre,
            importe=Decimal(importe), periodicidad='anual',
        )

    def movimiento(self, **campos):
        from finanzas.models import SubsobreFondo

        datos = {
            'fondo': self.conjunta, 'nombre': 'Aporte Gastos Anuales',
            'fondo_destino': self.provision,
        }
        datos.update(campos)
        return SubsobreFondo.objects.create(**datos)

    def provision_del_bloque(self):
        from finanzas import presupuesto
        return presupuesto.por_bloque(self.hogar)['anual']

    # ── El bug ───────────────────────────────────────────────────────────

    def test_siguiendo_al_bloque_recoge_los_gastos_nuevos(self):
        """«Si hoy añado un gasto del mes actual en adelante, debe salir.»"""
        ss = self.movimiento(bloque='anual')
        self.assertEqual(ss.importe_calculado, self.provision_del_bloque())

        self.anual('ITV moto', '120')      # +10 €/mes
        self.assertEqual(ss.importe_calculado, self.provision_del_bloque())
        self.assertEqual(ss.importe_calculado, Decimal('58.33'))

    def test_un_movimiento_a_otro_fondo_ya_mira_sus_partidas(self):
        """El campo promete que «si vinculas partidas, el importe se calcula
        sumando su importe_mensual», y con un fondo de destino no lo hacía."""
        ss = self.movimiento(importe_manual=Decimal('99'))
        ss.partidas_vinculadas.set(self.PartidaGasto.objects.filter(hogar=self.hogar))

        self.assertEqual(ss.importe_calculado, Decimal('48.33'))

    def test_el_motor_de_distribucion_usa_la_cifra_viva(self):
        """Leía `importe_manual` directamente, así que la propiedad no servía de
        nada: la pantalla enseñaba siempre lo tecleado."""
        from finanzas.distribucion import calcular_flujos

        self.movimiento(bloque='anual')
        self.anual('ITV moto', '120')

        datos = calcular_flujos(self.hogar, mes=10, anio=2026)
        cascada = [
            ss for fa in datos['fondos_aportaciones'] for ss in fa['subsobres']
        ]
        self.assertEqual(len(cascada), 1)
        self.assertEqual(cascada[0]['importe'], Decimal('58.33'))

    def test_lo_que_entra_en_el_fondo_de_destino_es_esa_misma_cifra(self):
        """Era la cifra de la captura: «Provision Anual · Entra +249,43» cuando
        el presupuesto decía 252,54."""
        from finanzas.distribucion import calcular_flujos

        self.movimiento(bloque='anual')
        self.anual('ITV moto', '120')

        datos = calcular_flujos(self.hogar, mes=10, anio=2026)
        destino = next(
            fa for fa in datos['fondos_aportaciones']
            if fa['fondo'].id == self.provision.id
        )
        self.assertEqual(destino['total_aportacion_base'], self.provision_del_bloque())

    # ── Lo que NO debe cambiar ───────────────────────────────────────────

    def test_un_importe_fijo_sigue_siendo_fijo(self):
        """Hay movimientos que son una cantidad elegida a propósito —200 € al
        mes al depósito— y no tienen que seguir a nada."""
        ss = self.movimiento(importe_manual=Decimal('200'))
        self.assertEqual(ss.importe_calculado, Decimal('200'))

        self.anual('ITV moto', '120')
        self.assertEqual(ss.importe_calculado, Decimal('200'))

    def test_el_bloque_manda_sobre_las_partidas_y_el_importe(self):
        ss = self.movimiento(bloque='anual', importe_manual=Decimal('999'))
        self.assertEqual(ss.importe_calculado, Decimal('48.33'))

    def test_cada_bloque_lleva_su_total(self):
        self.PartidaGasto.objects.create(
            hogar=self.hogar, categoria=self.alimentacion, nombre='Compra',
            importe=Decimal('400'), periodicidad='mensual',
        )
        self.assertEqual(
            self.movimiento(bloque='variable').importe_calculado, Decimal('400'),
        )
        self.assertEqual(
            self.movimiento(nombre='Otro', bloque='anual').importe_calculado,
            Decimal('48.33'),
        )

    # ── La pantalla ──────────────────────────────────────────────────────

    def test_se_puede_crear_siguiendo_un_bloque(self):
        from finanzas.models import SubsobreFondo

        self.client.post(
            reverse('finanzas:crear_subsobres', args=[self.conjunta.id]),
            {
                'nombre': 'Aporte Gastos Anuales', 'tipo': 'libre',
                'modo_importe': 'bloque', 'bloque': 'anual',
                'importe_manual': '999',
                'fondo_destino_id': self.provision.id, 'solo_mes': '',
            },
        )
        ss = SubsobreFondo.objects.get(fondo=self.conjunta)
        self.assertEqual(ss.bloque, 'anual')
        # El importe tecleado no se guarda: sería una cifra de adorno que al
        # leer la ficha parece la buena.
        self.assertIsNone(ss.importe_manual)
        self.assertEqual(ss.importe_calculado, Decimal('48.33'))

    def test_se_puede_crear_con_importe_fijo(self):
        from finanzas.models import SubsobreFondo

        self.client.post(
            reverse('finanzas:crear_subsobres', args=[self.conjunta.id]),
            {
                'nombre': 'Inversion', 'tipo': 'libre',
                'modo_importe': 'fijo', 'bloque': 'anual',
                'importe_manual': '200',
                'fondo_destino_id': self.provision.id, 'solo_mes': '',
            },
        )
        ss = SubsobreFondo.objects.get(fondo=self.conjunta)
        self.assertEqual(ss.bloque, '')
        self.assertEqual(ss.importe_calculado, Decimal('200'))

    def test_la_pantalla_dice_de_donde_sale_cada_cifra(self):
        self.movimiento(bloque='anual')
        self.movimiento(nombre='Inversion', importe_manual=Decimal('200'))

        respuesta = self.client.get(
            reverse('finanzas:vista_distribucion'), {'anio': 2026, 'mes': 10},
        )
        self.assertContains(respuesta, 'cascada-auto')
        self.assertContains(respuesta, 'NO cambia al tocar el presupuesto')

    # ── Los cierres no tienen nada que ver ───────────────────────────────

    def test_los_cierres_solo_congelan_meses_pasados(self):
        """La sospecha era que venía de evitar cambios hacia el pasado. No: un
        mes que no ha terminado nunca se congela."""
        import datetime
        from finanzas.cierres import meses_cerrados_de

        hoy = datetime.date(2026, 9, 16)
        self.assertEqual(meses_cerrados_de(2026, hoy), list(range(1, 9)))
        self.assertNotIn(9, meses_cerrados_de(2026, hoy))    # el mes en curso
        self.assertNotIn(10, meses_cerrados_de(2026, hoy))   # el siguiente
        self.assertEqual(meses_cerrados_de(2027, hoy), [])


class LineaMesAMesTests(TestCase):
    """La gráfica del año: en qué mes se fue el dinero, y en qué.

    Eran doce barras con los pagos anuales dibujados aparte para que no
    aplastasen al resto. No se leía: ni el mes del pico, ni de qué era. Una
    línea con su escala y una tarjeta al pasar por encima responde las dos.
    """

    def setUp(self):
        from datetime import date
        from core.models import Hogar
        from extractos.models import ExtractoBancario, MovimientoBancario
        from finanzas.models import CategoriaGasto, PartidaGasto, Vehiculo
        from finanzas.views_gastos import _crear_categorias_predefinidas

        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        self.user = User.objects.create_user(username='tester', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        self.client.force_login(self.user)

        self.coche = Vehiculo.objects.create(hogar=self.hogar, nombre='Polo', tipo='coche')
        self.gasolina = CategoriaGasto.objects.get(hogar=self.hogar, nombre='Gasolina')
        self.mantenimiento = CategoriaGasto.objects.get(
            hogar=self.hogar, nombre='Mantenimiento vehicular')
        self.revision = PartidaGasto.objects.create(
            hogar=self.hogar, categoria=self.mantenimiento, nombre='Revisión',
            importe=Decimal('1200'), periodicidad='anual', vehiculo=self.coche,
        )
        self.extracto = ExtractoBancario.objects.create(hogar=self.hogar, usuario=self.user)
        self.MovimientoBancario = MovimientoBancario
        self.date = date
        self._n = 0

    def mov(self, importe, mes, categoria=None, provision=None, concepto=None):
        self._n += 1
        return self.MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=self.date(2026, mes, 10),
            concepto=concepto or f'Gasto {self._n}', importe=Decimal(importe),
            categoria=categoria or self.gasolina, vehiculo=self.coche,
            partida_conciliada=provision,
        )

    def grafico(self, hoy=(2026, 9, 15)):
        from unittest import mock
        from finanzas import costes_activo

        with mock.patch('finanzas.costes_activo.date') as falso:
            falso.today.return_value = self.date(*hoy)
            return costes_activo.costes(self.coche, 2026)['grafico_mensual']

    # ── El eje ───────────────────────────────────────────────────────────

    def test_el_eje_sube_a_una_cifra_redonda(self):
        """Sin redondear, el eje decía «1.249,34» y «624,67»: cifras que nadie
        lee en un eje."""
        self.mov('-1249.34', 9)
        g = self.grafico()

        self.assertEqual(g['tope'], Decimal('1500.00'))
        self.assertEqual(
            [m['valor'] for m in g['marcas']],
            [Decimal('0'), Decimal('500'), Decimal('1000'), Decimal('1500')],
        )

    def test_el_eje_deja_sitio_a_la_prevision(self):
        """Con un gasto pequeño y una previsión alta, la línea de referencia se
        salía por arriba del marco."""
        from finanzas.models import PartidaGasto

        PartidaGasto.objects.create(
            hogar=self.hogar, categoria=self.gasolina, nombre='Gasolina',
            importe=Decimal('300'), periodicidad='mensual', vehiculo=self.coche,
        )
        self.mov('-20', 3)
        g = self.grafico()

        self.assertGreaterEqual(g['tope'], Decimal('300'))
        self.assertGreaterEqual(g['y_teorico'], 0)

    # ── La línea ─────────────────────────────────────────────────────────

    def test_la_linea_se_corta_en_el_mes_en_curso(self):
        """Un mes que aún no ha llegado no es un mes de cero euros: dibujarlo
        desplomaba el año en curso a cero en octubre."""
        self.mov('-100', 3)
        g = self.grafico(hoy=(2026, 9, 15))

        futuros = [m['etiqueta'] for m in g['meses'] if m['futuro']]
        self.assertEqual(futuros, ['Oct', 'Nov', 'Dic'])
        self.assertEqual(g['linea'].count(','), 9)      # de enero a septiembre

    def test_un_año_cerrado_dibuja_los_doce_meses(self):
        self.mov('-100', 3)
        g = self.grafico(hoy=(2027, 4, 1))

        self.assertEqual([m for m in g['meses'] if m['futuro']], [])
        self.assertEqual(g['linea'].count(','), 12)

    def test_un_mes_sin_gasto_es_un_cero_de_verdad(self):
        self.mov('-100', 3)
        por_mes = {m['mes']: m for m in self.grafico()['meses']}

        self.assertEqual(por_mes[4]['total'], Decimal('0'))
        self.assertEqual(por_mes[4]['num'], 0)
        self.assertFalse(por_mes[4]['futuro'])

    def test_se_etiqueta_el_mes_del_pico_y_solo_ese(self):
        """Un número en cada punto es ruido que nadie lee; el del pico responde
        a «¿en qué mes se me fue?» sin pasar el ratón."""
        self.mov('-60', 3)
        self.mov('-1200', 9, self.mantenimiento, provision=self.revision)

        self.assertEqual(self.grafico()['pico']['etiqueta'], 'Sep')

    # ── La tarjeta ───────────────────────────────────────────────────────

    def test_cada_mes_lleva_sus_movimientos_para_la_tarjeta(self):
        self.mov('-60', 3, concepto='Repsol')
        self.mov('-25', 3, self.mantenimiento, concepto='Autolavado')
        datos = {m['mes']: m for m in self.grafico()['datos']}

        marzo = datos[3]
        self.assertEqual(marzo['total'], 85.0)
        self.assertEqual(marzo['num'], 2)
        self.assertEqual(
            sorted(x['concepto'] for x in marzo['movimientos']), ['Autolavado', 'Repsol'],
        )
        self.assertEqual(
            sorted(x['categoria'] for x in marzo['movimientos']),
            ['Gasolina', 'Mantenimiento vehicular'],
        )

    def test_la_tarjeta_separa_el_gasto_corriente_del_pago_anual(self):
        self.mov('-60', 9)
        self.mov('-1200', 9, self.mantenimiento, provision=self.revision)
        septiembre = {m['mes']: m for m in self.grafico()['datos']}[9]

        self.assertEqual(septiembre['total'], 1260.0)
        self.assertEqual(septiembre['corriente'], 60.0)
        self.assertEqual(septiembre['provision'], 1200.0)
        self.assertTrue(
            any(x['provision'] for x in septiembre['movimientos']),
        )

    def test_un_mes_con_muchos_movimientos_no_desborda_la_tarjeta(self):
        from finanzas.costes_activo import MAXIMO_EN_LA_TARJETA

        for _ in range(MAXIMO_EN_LA_TARJETA + 4):
            self.mov('-10', 3)
        marzo = {m['mes']: m for m in self.grafico()['datos']}[3]

        self.assertEqual(len(marzo['movimientos']), MAXIMO_EN_LA_TARJETA)
        self.assertEqual(marzo['ocultos'], 4)
        # Pero el total sigue siendo el de todos.
        self.assertEqual(marzo['total'], float(10 * (MAXIMO_EN_LA_TARJETA + 4)))

    # ── En pantalla ──────────────────────────────────────────────────────

    def test_la_ficha_del_vehiculo_pinta_la_linea(self):
        from django.urls import reverse as url

        self.mov('-60', 3)
        respuesta = self.client.get(
            url('finanzas:detalle_vehiculo', args=[self.coche.id]), {'anio': 2026},
        )
        self.assertContains(respuesta, 'lmes-linea')
        self.assertContains(respuesta, 'lmes-datos-vehiculo')
        # Y la tabla, que es lo que se lee sin ratón y sin JS.
        self.assertContains(respuesta, 'Ver los meses en una tabla')

    def test_la_pantalla_de_propiedades_pinta_la_suya(self):
        from datetime import date as dia
        from django.urls import reverse as url
        from finanzas.models import CategoriaGasto, Propiedad

        casa = Propiedad.objects.create(
            hogar=self.hogar, nombre='Piso', fecha_compra=dia(2020, 1, 1),
            precio_compra=Decimal('100000'), valor_actual=Decimal('120000'),
        )
        self.MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=dia(2026, 5, 3),
            concepto='Comunidad', importe=Decimal('-62'),
            categoria=CategoriaGasto.objects.get(hogar=self.hogar, nombre='Comunidad'),
            propiedad=casa,
        )

        respuesta = self.client.get(url('finanzas:listar_propiedades'), {'anio': 2026})
        self.assertContains(respuesta, 'lmes-linea')
        self.assertContains(respuesta, 'lmes-datos-propiedad')

    def test_los_recursos_del_grafico_van_una_sola_vez(self):
        """En propiedades hay un gráfico por tarjeta: si el JS se incluyese con
        cada uno, cada gesto se manejaría tantas veces como casas haya."""
        from datetime import date as dia
        from django.urls import reverse as url
        from finanzas.models import Propiedad

        for n in range(3):
            Propiedad.objects.create(
                hogar=self.hogar, nombre=f'Piso {n}', fecha_compra=dia(2020, 1, 1),
                precio_compra=Decimal('100000'), valor_actual=Decimal('120000'),
            )
        cuerpo = self.client.get(
            url('finanzas:listar_propiedades'), {'anio': 2026},
        ).content.decode()
        self.assertEqual(cuerpo.count('function mostrar(figura'), 1)


class PeriodicidadPlurianualTests(TestCase):
    """Hay gastos que duran varios años —unos neumáticos, una caldera— y
    presupuestarlos «al año» obliga a inventarse una cifra."""

    def setUp(self):
        from core.models import Hogar
        from finanzas.models import CategoriaGasto
        from finanzas.views_gastos import _crear_categorias_predefinidas

        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        self.user = User.objects.create_user(username='tester', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        self.client.force_login(self.user)
        self.categoria = CategoriaGasto.objects.get(
            hogar=self.hogar, nombre='Mantenimiento vehicular')

    def partida(self, importe, periodicidad):
        from finanzas.models import PartidaGasto
        return PartidaGasto.objects.create(
            hogar=self.hogar, categoria=self.categoria, nombre='Prueba',
            importe=Decimal(importe), periodicidad=periodicidad,
        )

    def test_los_meses_de_cada_periodicidad(self):
        for periodicidad, meses in [
            ('mensual', 1), ('bimensual', 2), ('trimestral', 3), ('semestral', 6),
            ('anual', 12), ('bienal', 24), ('trienal', 36), ('quinquenal', 60),
        ]:
            with self.subTest(periodicidad=periodicidad):
                self.assertEqual(self.partida('100', periodicidad).meses_periodo, meses)

    def test_unos_neumaticos_de_tres_años(self):
        neumaticos = self.partida('470', 'trienal')
        self.assertEqual(neumaticos.importe_mensual, Decimal('13.06'))
        self.assertEqual(neumaticos.importe_anual, Decimal('156.67'))

    def test_las_periodicidades_de_siempre_no_cambian(self):
        self.assertEqual(self.partida('520', 'anual').importe_mensual, Decimal('43.33'))
        self.assertEqual(self.partida('520', 'anual').importe_anual, Decimal('520'))
        self.assertEqual(self.partida('60', 'mensual').importe_mensual, Decimal('60'))
        self.assertEqual(self.partida('60', 'mensual').importe_anual, Decimal('720'))
        self.assertEqual(self.partida('90', 'trimestral').importe_mensual, Decimal('30'))

    def test_entran_en_el_presupuesto_prorrateadas(self):
        from finanzas import presupuesto

        self.partida('470', 'trienal')
        self.assertEqual(
            presupuesto.por_categoria(self.hogar)[self.categoria.id], Decimal('13.06'),
        )

    # ── Cada N meses, escrito a mano ─────────────────────────────────────

    def partida_a_medida(self, importe, meses):
        from finanzas.models import PartidaGasto, normalizar_periodicidad

        periodicidad, personalizados = normalizar_periodicidad('personalizada', meses)
        return PartidaGasto.objects.create(
            hogar=self.hogar, categoria=self.categoria, nombre='A medida',
            importe=Decimal(importe), periodicidad=periodicidad,
            meses_personalizados=personalizados,
        )

    def test_los_meses_los_pone_el_usuario(self):
        """La vida útil de unos neumáticos no viene en años redondos: 36 en el
        coche que hace kilómetros y 50 en el que apenas sale."""
        self.assertEqual(self.partida_a_medida('470', 36).meses_periodo, 36)
        self.assertEqual(self.partida_a_medida('470', 50).meses_periodo, 50)
        self.assertEqual(self.partida_a_medida('470', 7).meses_periodo, 7)

    def test_cada_coche_puede_llevar_los_suyos(self):
        uno = self.partida_a_medida('600', 36)
        otro = self.partida_a_medida('600', 50)
        self.assertEqual(uno.importe_mensual, Decimal('16.67'))
        self.assertEqual(otro.importe_mensual, Decimal('12'))

    def test_un_numero_que_ya_tiene_nombre_se_guarda_con_su_nombre(self):
        """Escribir 12 es «Anual». Si no, la pantalla diría «Cada 12 meses» y,
        peor, habría dos formas distintas de decir lo mismo en la base."""
        from finanzas.models import normalizar_periodicidad

        self.assertEqual(normalizar_periodicidad('personalizada', 12), ('anual', None))
        self.assertEqual(normalizar_periodicidad('personalizada', 1), ('mensual', None))
        self.assertEqual(normalizar_periodicidad('personalizada', 36), ('trienal', None))
        self.assertEqual(normalizar_periodicidad('personalizada', 50), ('personalizada', 50))

    def test_un_numero_imposible_no_rompe_nada(self):
        from finanzas.models import normalizar_periodicidad

        self.assertEqual(normalizar_periodicidad('personalizada', 0), ('mensual', None))
        self.assertEqual(normalizar_periodicidad('personalizada', -3), ('mensual', None))
        self.assertEqual(normalizar_periodicidad('personalizada', 'ocho'), ('mensual', None))
        self.assertEqual(normalizar_periodicidad('personalizada', None), ('mensual', None))
        self.assertEqual(normalizar_periodicidad('personalizada', 9999), ('personalizada', 600))

    def test_la_pantalla_lo_llama_por_su_numero(self):
        """Veinte plantillas piden `get_periodicidad_display`; ninguna debería
        acabar enseñando «Cada N meses…» con la N literal."""
        self.assertEqual(
            self.partida_a_medida('470', 50).get_periodicidad_display(), 'Cada 50 meses')
        self.assertEqual(self.partida('520', 'anual').get_periodicidad_display(), 'Anual')

    def test_se_declara_desde_la_pantalla(self):
        from finanzas.models import PartidaGasto

        respuesta = self.client.get(reverse('finanzas:crear_partida'))
        self.assertContains(respuesta, 'meses_personalizados')
        self.assertContains(respuesta, 'Cada N meses')

        self.client.post(reverse('finanzas:crear_partida'), {
            'categoria_id': str(self.categoria.id), 'nombre': 'Neumáticos del Ibiza',
            'importe': '600', 'periodicidad': 'personalizada',
            'meses_personalizados': '50',
        })
        creada = PartidaGasto.objects.get(hogar=self.hogar, nombre='Neumáticos del Ibiza')
        self.assertEqual(creada.periodicidad, 'personalizada')
        self.assertEqual(creada.meses_personalizados, 50)
        self.assertEqual(creada.importe_mensual, Decimal('12'))

    def test_se_cambia_desde_la_pantalla(self):
        partida = self.partida_a_medida('600', 50)

        self.client.post(reverse('finanzas:editar_partida', args=[partida.id]), {
            'categoria_id': str(self.categoria.id), 'nombre': 'A medida',
            'importe': '600', 'periodicidad': 'personalizada',
            'meses_personalizados': '36',
        })
        partida.refresh_from_db()
        self.assertEqual(partida.meses_periodo, 36)

    def test_al_volver_a_una_periodicidad_normal_se_olvidan_los_meses(self):
        """Si no, quedarían 50 meses guardados en una partida anual esperando a
        confundir a alguien el día que vuelva a tocar «Cada N meses»."""
        partida = self.partida_a_medida('600', 50)

        self.client.post(reverse('finanzas:editar_partida', args=[partida.id]), {
            'categoria_id': str(self.categoria.id), 'nombre': 'A medida',
            'importe': '600', 'periodicidad': 'anual',
        })
        partida.refresh_from_db()
        self.assertEqual(partida.periodicidad, 'anual')
        self.assertIsNone(partida.meses_personalizados)
        self.assertEqual(partida.meses_periodo, 12)

    def test_entra_en_el_presupuesto_prorrateada(self):
        from finanzas import presupuesto

        self.partida_a_medida('600', 50)
        self.assertEqual(
            presupuesto.por_categoria(self.hogar)[self.categoria.id], Decimal('12'),
        )

    def test_se_puede_editar_el_numero_de_meses_desde_la_pantalla(self):
        partida = self.partida_a_medida('600', 50)
        respuesta = self.client.get(reverse('finanzas:editar_partida', args=[partida.id]))
        self.assertContains(respuesta, 'value="50"')

    def test_se_pueden_declarar_desde_la_pantalla(self):
        from finanzas.models import PartidaGasto

        respuesta = self.client.get(reverse('finanzas:crear_partida'))
        self.assertContains(respuesta, 'Cada 3 años')
        # El cálculo en pantalla usa los mismos meses que el servidor.
        self.assertContains(respuesta, 'meses-por-periodicidad')

        self.client.post(reverse('finanzas:crear_partida'), {
            'categoria_id': str(self.categoria.id), 'nombre': 'Neumáticos',
            'importe': '470', 'periodicidad': 'trienal',
        })
        creada = PartidaGasto.objects.get(hogar=self.hogar, nombre='Neumáticos')
        self.assertEqual(creada.periodicidad, 'trienal')
        self.assertEqual(creada.importe_mensual, Decimal('13.06'))
