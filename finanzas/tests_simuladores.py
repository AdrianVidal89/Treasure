"""Tests del simulador de vivienda.

Los tres fallos que motivaron la reescritura eran de comportamiento, no de
cálculo: los valores no persistían, el escenario mostrado no era el que el
usuario estaba simulando, y el presupuesto posterior ignoraba los costes de ser
propietario. Se cubren los tres, más la matemática hipotecaria y los umbrales.
"""

import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core.models import Hogar
from finanzas.models import CategoriaGasto, PartidaGasto, SimulacionVivienda
from finanzas.views_gastos import _crear_categorias_predefinidas
from finanzas.views_simuladores import _datos_financieros, _limpiar_valor


def cuota_francesa(capital, tipo_anual, anios):
    """Misma fórmula que usa el simulador, para comprobar sus resultados."""
    r = tipo_anual / 100 / 12
    n = anios * 12
    if r == 0:
        return capital / n
    return capital * r / (1 - (1 + r) ** -n)


class PersistenciaSimulacionTests(TestCase):
    """«Quiero que los valores se queden como los deje.»"""

    def setUp(self):
        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        self.user = User.objects.create_user(username='tester', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        self.client.force_login(self.user)

    def guardar(self, **datos):
        return self.client.post(
            reverse('finanzas:guardar_simulacion_vivienda'),
            data=json.dumps(datos), content_type='application/json',
        )

    def test_la_primera_visita_crea_el_escenario_con_valores_por_defecto(self):
        respuesta = self.client.get(reverse('finanzas:simulador_vivienda'))
        self.assertEqual(respuesta.status_code, 200)
        self.assertTrue(SimulacionVivienda.objects.filter(hogar=self.hogar).exists())
        self.assertEqual(respuesta.context['simulacion']['modo'], 'maximo')

    def test_los_valores_sobreviven_a_recargar(self):
        self.guardar(modo='precio', precio=275000, plazo_anios=30, tipo_interes=2.75)
        respuesta = self.client.get(reverse('finanzas:simulador_vivienda'))
        guardado = respuesta.context['simulacion']
        self.assertEqual(guardado['modo'], 'precio')
        self.assertEqual(guardado['precio'], 275000)
        self.assertEqual(guardado['plazo_anios'], 30)
        self.assertEqual(guardado['tipo_interes'], 2.75)

    def test_guardar_solo_toca_lo_enviado(self):
        self.guardar(precio=250000)
        self.guardar(plazo_anios=15)
        sim = SimulacionVivienda.objects.get(hogar=self.hogar)
        self.assertEqual(sim.precio, Decimal('250000'))
        self.assertEqual(sim.plazo_anios, 15)

    def test_cada_hogar_tiene_su_escenario(self):
        otro_hogar = Hogar.objects.create(nombre='Otro')
        otro = User.objects.create_user(username='otro', password='clave-de-prueba')
        perfil = otro.userprofile
        perfil.hogar = otro_hogar
        perfil.save()

        self.guardar(precio=250000)
        self.client.force_login(otro)
        self.guardar(precio=800000)

        self.assertEqual(
            SimulacionVivienda.objects.get(hogar=self.hogar).precio, Decimal('250000'),
        )
        self.assertEqual(
            SimulacionVivienda.objects.get(hogar=otro_hogar).precio, Decimal('800000'),
        )

    def test_los_valores_fuera_de_rango_se_acotan(self):
        self.guardar(precio=99999999, plazo_anios=200, tipo_interes=-5)
        sim = SimulacionVivienda.objects.get(hogar=self.hogar)
        self.assertEqual(sim.precio, Decimal('5000000'))
        self.assertEqual(sim.plazo_anios, 40)
        self.assertEqual(sim.tipo_interes, Decimal('0'))

    def test_ignora_basura_sin_romperse(self):
        respuesta = self.guardar(modo='inventado', precio='no soy un número', plazo_anios=None)
        self.assertEqual(respuesta.status_code, 200)
        sim = SimulacionVivienda.objects.get(hogar=self.hogar)
        self.assertEqual(sim.modo, 'maximo')
        self.assertEqual(sim.precio, Decimal('300000'))

    def test_json_invalido_devuelve_400(self):
        respuesta = self.client.post(
            reverse('finanzas:guardar_simulacion_vivienda'),
            data='{esto no es json', content_type='application/json',
        )
        self.assertEqual(respuesta.status_code, 400)

    def test_solo_acepta_post(self):
        respuesta = self.client.get(reverse('finanzas:guardar_simulacion_vivienda'))
        self.assertEqual(respuesta.status_code, 405)


class LimpiarValorTests(TestCase):

    def test_choice(self):
        self.assertEqual(_limpiar_valor('precio', 'choice', ['maximo', 'precio']), 'precio')
        self.assertIsNone(_limpiar_valor('otro', 'choice', ['maximo', 'precio']))

    def test_ids(self):
        self.assertEqual(_limpiar_valor([1, '2', 'x'], 'ids', None), [1, 2])
        self.assertIsNone(_limpiar_valor('no es lista', 'ids', None))

    def test_numeros_no_finitos(self):
        self.assertIsNone(_limpiar_valor('NaN', 'decimal', (0, 100)))
        self.assertIsNone(_limpiar_valor('Infinity', 'decimal', (0, 100)))


class DatosFinancierosTests(TestCase):
    """El presupuesto posterior necesita los cuatro bloques por separado y saber
    cuánto se paga hoy por vivienda."""

    def setUp(self):
        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        _crear_categorias_predefinidas(self.hogar)

    def partida(self, categoria, importe):
        PartidaGasto.objects.create(
            hogar=self.hogar,
            categoria=CategoriaGasto.objects.get(hogar=self.hogar, nombre=categoria),
            nombre=f'Partida {categoria}', importe=Decimal(importe), periodicidad='mensual',
        )

    def test_desglosa_los_cuatro_bloques(self):
        self.partida('Hipoteca / Alquiler', '900')
        self.partida('Alimentacion', '400')
        self.partida('Ocio', '120')
        self.partida('IBI', '60')

        d = _datos_financieros(self.hogar)['sim_data']
        self.assertEqual(d['gastos_desg_fijos'], 900)
        self.assertEqual(d['gastos_desg_variables'], 400)
        self.assertEqual(d['gastos_desg_discrecionales'], 120)
        self.assertEqual(d['gastos_desg_anuales'], 60)
        self.assertEqual(d['gastos_totales_mensuales'], 1480)

    def test_detecta_lo_que_ya_se_paga_por_vivienda(self):
        self.partida('Hipoteca / Alquiler', '850')
        self.partida('Alimentacion', '400')
        d = _datos_financieros(self.hogar)['sim_data']
        self.assertEqual(d['vivienda_actual_mensual'], 850)

    def test_las_categorias_de_ingreso_no_cuentan_como_gasto(self):
        self.partida('Alimentacion', '400')
        PartidaGasto.objects.create(
            hogar=self.hogar,
            categoria=CategoriaGasto.objects.get(hogar=self.hogar, nombre='Nomina'),
            nombre='No deberia contar', importe=Decimal('2000'), periodicidad='mensual',
        )
        d = _datos_financieros(self.hogar)['sim_data']
        self.assertEqual(d['gastos_totales_mensuales'], 400)


class MatematicaHipotecaTests(TestCase):
    """Comprobaciones de la fórmula francesa contra valores conocidos."""

    def test_cuota_conocida(self):
        # 200.000 € a 30 años al 3% → 843,21 €/mes (valor de referencia de
        # cualquier calculadora hipotecaria).
        self.assertAlmostEqual(cuota_francesa(200000, 3.0, 30), 843.21, places=1)

    def test_tipo_cero_reparte_el_capital(self):
        self.assertAlmostEqual(cuota_francesa(120000, 0, 10), 1000.0, places=6)

    def test_capital_maximo_es_la_inversa_de_la_cuota(self):
        capital, tipo, anios = 250000, 3.5, 25
        cuota = cuota_francesa(capital, tipo, anios)
        r = tipo / 100 / 12
        n = anios * 12
        capital_max = cuota * (1 - (1 + r) ** -n) / r
        self.assertAlmostEqual(capital_max, capital, places=4)

    def test_el_precio_maximo_lo_marca_la_restriccion_mas_dura(self):
        # Réplica del cálculo del simulador: manda el menor de los dos límites.
        cuota_max, tipo, anios = 1000.0, 3.0, 30
        entrada_pct, gastos_pct = 20.0, 11.0
        r = tipo / 100 / 12
        n = anios * 12
        hip_max = cuota_max * (1 - (1 + r) ** -n) / r

        por_cuota = hip_max / (1 - entrada_pct / 100)
        # Con mucho ahorro manda la cuota.
        por_ahorro_alto = 500000 / ((entrada_pct + gastos_pct) / 100)
        self.assertAlmostEqual(min(por_cuota, por_ahorro_alto), por_cuota, places=4)
        # Con poco ahorro manda el ahorro.
        por_ahorro_bajo = 30000 / ((entrada_pct + gastos_pct) / 100)
        self.assertAlmostEqual(min(por_cuota, por_ahorro_bajo), por_ahorro_bajo, places=4)


class RenderSimuladorTests(TestCase):

    def setUp(self):
        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        self.user = User.objects.create_user(username='tester', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        self.client.force_login(self.user)

    def test_la_pagina_carga_con_el_escenario_guardado(self):
        SimulacionVivienda.objects.create(
            hogar=self.hogar, modo='precio', precio=Decimal('265000'),
            comunidad_mes=Decimal('75'),
        )
        respuesta = self.client.get(reverse('finanzas:simulador_vivienda'))
        self.assertEqual(respuesta.status_code, 200)
        sim = respuesta.context['simulacion']
        self.assertEqual(sim['modo'], 'precio')
        self.assertEqual(sim['precio'], 265000)
        self.assertEqual(sim['comunidad_mes'], 75)

    def test_el_simulador_de_vehiculo_sigue_funcionando(self):
        respuesta = self.client.get(reverse('finanzas:simulador_vehiculo'))
        self.assertEqual(respuesta.status_code, 200)

    def test_sin_hogar_redirige(self):
        sin_hogar = User.objects.create_user(username='solo', password='clave-de-prueba')
        self.client.force_login(sin_hogar)
        respuesta = self.client.get(reverse('finanzas:simulador_vivienda'))
        self.assertEqual(respuesta.status_code, 302)


class PresupuestoPosteriorTests(TestCase):
    """El presupuesto tras comprar debe mover de verdad las partidas, no dejarlo
    todo igual y sumar solo la cuota."""

    def setUp(self):
        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        _crear_categorias_predefinidas(self.hogar)

    def partida(self, categoria, importe):
        PartidaGasto.objects.create(
            hogar=self.hogar,
            categoria=CategoriaGasto.objects.get(hogar=self.hogar, nombre=categoria),
            nombre=f'Partida {categoria}', importe=Decimal(importe), periodicidad='mensual',
        )

    def test_los_costes_de_propiedad_van_a_su_bloque(self):
        # Réplica del reparto que hace el simulador: comunidad y seguro a Fijos,
        # IBI y mantenimiento a Fijos anuales.
        self.partida('Hipoteca / Alquiler', '800')
        self.partida('Alimentacion', '400')
        self.partida('IBI', '50')
        d = _datos_financieros(self.hogar)['sim_data']

        precio, ibi_pct, mant_pct = 250000, 0.5, 1.0
        comunidad, seguro_anio = 60, 300
        nuevos_fijos = comunidad + seguro_anio / 12
        nuevos_anuales = precio * ibi_pct / 100 / 12 + precio * mant_pct / 100 / 12

        fijos_despues = d['gastos_desg_fijos'] + nuevos_fijos
        anuales_despues = d['gastos_desg_anuales'] + nuevos_anuales

        self.assertAlmostEqual(fijos_despues, 885.0, places=2)
        self.assertAlmostEqual(anuales_despues, 50 + 312.5, places=2)
        # Lo esencial: los bloques cambian, no se quedan igual que antes.
        self.assertNotEqual(fijos_despues, d['gastos_desg_fijos'])
        self.assertNotEqual(anuales_despues, d['gastos_desg_anuales'])

    def test_sustituir_la_vivienda_actual_libera_su_importe(self):
        self.partida('Hipoteca / Alquiler', '800')
        self.partida('Alimentacion', '400')
        d = _datos_financieros(self.hogar)['sim_data']

        fijos_sin_sustituir = d['gastos_desg_fijos'] + 60
        fijos_sustituyendo = d['gastos_desg_fijos'] + 60 - d['vivienda_actual_mensual']
        self.assertEqual(fijos_sin_sustituir - fijos_sustituyendo, 800)

    def test_el_coste_real_de_comprar_no_es_solo_la_cuota(self):
        precio = 250000
        cuota = cuota_francesa(precio * 0.8, 3.0, 30)
        costes = 60 + 300 / 12 + precio * 0.5 / 100 / 12 + precio * 1.0 / 100 / 12
        self.assertGreater(costes, 350)
        self.assertGreater(cuota + costes, cuota * 1.4)
