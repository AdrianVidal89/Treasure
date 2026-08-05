"""Tests de la importación y categorización de extractos bancarios.

Los ficheros de `tests_fixtures/` son recortes anonimizados de extractos reales
(CaixaBank en CSV y en .xls antiguo, Revolut en CSV), porque los fallos que
motivaron estos cambios solo aparecen con las rarezas de los formatos de verdad:
columnas de descripción partidas en dos, acentos, operaciones pendientes y
razones sociales pegadas al nombre del comercio.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase
from django.urls import reverse

from core.models import Hogar
from finanzas.models import CategoriaGasto, PartidaGasto
from finanzas.parsing import es_excel, leer_tabla
from finanzas.views_gastos import _crear_categorias_predefinidas

from .categorizacion import categorizar_por_codigo
from .models import ExtractoBancario, MovimientoBancario, ReglaCategorizacion
from .normalizacion import (
    contiene_patron, es_traspaso_interno, normalizar_comercio, normalizar_texto,
)
from .parser import analizar_extracto
from .views import _importar_analizados, _panel_context

FIXTURES = Path(__file__).resolve().parent / 'tests_fixtures'


def leer_fixture(nombre):
    ruta = FIXTURES / nombre
    if ruta.suffix == '.xls':
        class _Archivo:
            name = nombre

            def read(self):
                return ruta.read_bytes()

        return leer_tabla(_Archivo())
    return ruta.read_text(encoding='utf-8')


class NormalizacionTests(TestCase):

    def test_quita_acentos_para_comparar(self):
        self.assertEqual(normalizar_texto('Cafetería'), 'cafeteria')
        self.assertEqual(normalizar_texto('Baobab Cafè'), 'baobab cafe')
        self.assertEqual(normalizar_texto('el jamón'), 'el jamon')

    def test_limite_de_palabra_evita_falsos_positivos(self):
        self.assertTrue(contiene_patron(normalizar_texto('Bar Flamenco'), 'bar'))
        self.assertFalse(contiene_patron(normalizar_texto('Barcelona Store'), 'bar'))
        self.assertFalse(contiene_patron(normalizar_texto('recibí el pago'), 'ibi'))

    def test_comercio_ignora_ruido_de_la_operacion(self):
        self.assertEqual(normalizar_comercio('WWW.AMAZON'), 'amazon')
        self.assertEqual(normalizar_comercio('Pepe Mobile, S.L.U.'), 'pepe mobile')
        self.assertEqual(normalizar_comercio('Bip Drive, S.a.'), 'bip drive')
        self.assertEqual(
            normalizar_comercio('EL CORTE INGLES · Fecha de operación: 28-05-2026'),
            'el corte ingles',
        )

    def test_mismo_comercio_pese_a_referencias_distintas(self):
        # Es lo que permite agrupar los recibos mes a mes en un solo grupo.
        primero = normalizar_comercio('RECIBO UNICO MYBOX · CUOTA AGRUPADA MYBOX 01-07-2026')
        segundo = normalizar_comercio('RECIBO UNICO MYBOX · CUOTA AGRUPADA MYBOX 01-06-2026')
        self.assertEqual(primero, segundo)

    def test_traspaso_solo_si_menciona_a_un_miembro(self):
        nombres = {'nombre apellido', 'nombre', 'apellido'}
        self.assertTrue(es_traspaso_interno('Transferencia a NOMBRE APELLIDO', nombres))
        self.assertFalse(es_traspaso_interno('Transferencia a Pepe Mobile, S.L.U.', nombres))


class CategorizacionPorCodigoTests(TestCase):

    def test_gana_el_patron_mas_especifico(self):
        # 'repsol' a secas es Gasolina, pero la comercializadora es la factura
        # del gas: antes ganaba el primero del diccionario y salía Gasolina.
        self.assertEqual(categorizar_por_codigo('Repsol'), 'Gasolina')
        self.assertEqual(
            categorizar_por_codigo('Repsol, S.L.U.-REPSOL COMERCIALIZADORA DE ELECTRICIDAD Y GAS SLU'),
            'Gas',
        )
        self.assertEqual(categorizar_por_codigo('Movistar'), 'Internet / Telefono')
        self.assertEqual(categorizar_por_codigo('Movistar Plus'), 'Ocio')

    def test_acierta_pese_a_los_acentos(self):
        self.assertEqual(
            categorizar_por_codigo('Cafetería del Hospital Universitario Virgen del Rocío'),
            'Restaurantes',
        )
        self.assertEqual(categorizar_por_codigo('Baobab Cafè Restaurant'), 'Restaurantes')
        self.assertEqual(categorizar_por_codigo('el jamón'), 'Alimentacion')

    def test_categorias_nuevas(self):
        self.assertEqual(categorizar_por_codigo('Farmacia Ronda'), 'Salud / Farmacia')
        self.assertEqual(categorizar_por_codigo('Fcia Amanda Tesoro'), 'Salud / Farmacia')
        self.assertEqual(categorizar_por_codigo('Leroy Merlin'), 'Hogar / Bricolaje')
        self.assertEqual(categorizar_por_codigo('Anthropic'), 'Tecnologia / Software')
        self.assertEqual(categorizar_por_codigo('EMBARGOS'), 'Impuestos y comisiones')


class ParserTests(TestCase):

    def test_usa_la_columna_de_descripcion_extra(self):
        # Sin esto el concepto era solo «ALJARAFESA EMP» y no había manera de
        # saber que era el recibo del agua.
        r = analizar_extracto(leer_fixture('caixabank.csv'))
        self.assertEqual(r['mapa']['concepto'], 2)
        self.assertEqual(r['mapa']['concepto_extra'], 3)
        conceptos = [m['concepto'] for m in r['movimientos']]
        self.assertIn('ALJARAFESA EMP · Recibo de agua', conceptos)

    def test_omite_las_operaciones_no_firmes(self):
        # Las pendientes vuelven en el extracto siguiente ya consolidadas y con
        # otro saldo, así que importarlas genera duplicados.
        r = analizar_extracto(leer_fixture('revolut.csv'))
        self.assertEqual(len(r['filas_omitidas']), 1)
        self.assertIn('PENDIENTE', r['filas_omitidas'][0]['motivo'])
        self.assertNotIn('DIA', [m['concepto'] for m in r['movimientos']])

    def test_binario_ilegible_no_revienta(self):
        # Un .xls que no se pudo convertir llegaba aquí como texto basura y
        # lanzaba csv.Error sin capturar (error 500 en la pantalla de revisión).
        r = analizar_extracto('col1\x00\x01\n"sin cerrar\ncomilla\x00')
        self.assertEqual(r['movimientos'], [])
        self.assertTrue(r['errores_generales'])

    def test_lee_xls_antiguo(self):
        self.assertTrue(es_excel('Movimientos.xls'))
        r = analizar_extracto(leer_fixture('caixabank.xls'))
        self.assertFalse(r['errores_generales'])
        self.assertEqual(len(r['movimientos']), 3)
        self.assertIn(
            'ALJARAFESA EMP · Recibo de agua',
            [m['concepto'] for m in r['movimientos']],
        )


class ImportacionTests(TestCase):

    def setUp(self):
        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        self.user = User.objects.create_user(
            username='tester', password='clave-de-prueba',
            first_name='Nombre', last_name='Apellido',
        )
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)

    def importar(self, *fixtures):
        analizados = [
            {'nombre': nombre, 'resultado': analizar_extracto(leer_fixture(nombre))}
            for nombre in fixtures
        ]
        return _importar_analizados(self.hogar, self.user, 'Banco', None, analizados)

    def test_caixabank_pasa_de_cero_a_la_mayoria_categorizada(self):
        self.importar('caixabank.csv')
        gastos = MovimientoBancario.objects.filter(hogar=self.hogar, importe__lt=0)
        con_categoria = gastos.exclude(categoria__isnull=True)
        self.assertEqual(gastos.count(), 6)
        # Antes de estos cambios el acierto en este extracto era exactamente 0.
        self.assertGreaterEqual(con_categoria.count(), 4)

        agua = gastos.get(concepto__startswith='ALJARAFESA')
        self.assertEqual(agua.categoria.nombre, 'Agua')
        self.assertEqual(agua.estado_categorizacion, 'por_codigo')

    def test_guarda_comercio_y_concepto_original(self):
        self.importar('caixabank.csv')
        mov = MovimientoBancario.objects.get(hogar=self.hogar, concepto__startswith='WWW.AMAZON')
        self.assertEqual(mov.comercio, 'amazon')
        self.assertTrue(mov.concepto_raw)

    def test_marca_los_traspasos_entre_cuentas_propias(self):
        self.importar('revolut.csv')
        traspaso = MovimientoBancario.objects.get(
            hogar=self.hogar, concepto__contains='NOMBRE APELLIDO',
        )
        self.assertTrue(traspaso.es_traspaso)
        # Van a su propia categoría (y por tanto a su propio bloque), para que
        # el negativo de una cuenta y el positivo de la otra se compensen sin
        # mezclarse con el gasto ni con el ingreso.
        self.assertEqual(traspaso.categoria.nombre, 'Traspaso entre cuentas')
        self.assertEqual(traspaso.categoria.tipo, 'traspaso')
        # Y el resto de gastos sí se categorizan con normalidad.
        gas = MovimientoBancario.objects.get(hogar=self.hogar, concepto__contains='COMERCIALIZADORA')
        self.assertEqual(gas.categoria.nombre, 'Gas')

    def test_reimportar_no_duplica(self):
        primera = self.importar('revolut.csv')
        segunda = self.importar('revolut.csv')
        self.assertEqual(segunda['total_creados'], 0)
        self.assertEqual(segunda['total_duplicados'], primera['total_creados'])

    def test_el_mismo_extracto_en_csv_y_en_xls_no_duplica(self):
        # El Excel entrega los importes como número (-1700) y el CSV como texto
        # formateado (-1,700.00); si el hash no los normaliza, el mismo apunte
        # entra dos veces.
        self.importar('caixabank.csv')
        segunda = self.importar('caixabank.xls')
        self.assertEqual(segunda['total_creados'], 0)
        self.assertEqual(segunda['total_duplicados'], 3)

    def test_editar_el_concepto_recalcula_el_hash(self):
        # El hash solo se calculaba cuando estaba vacío, así que tras editar
        # quedaba apuntando a datos que ya no existían.
        self.importar('caixabank.csv')
        mov = MovimientoBancario.objects.filter(hogar=self.hogar).first()
        anterior = mov.hash_dedupe
        mov.concepto = 'Otro concepto distinto'
        mov.save()
        mov.refresh_from_db()
        self.assertNotEqual(mov.hash_dedupe, anterior)
        self.assertEqual(
            mov.hash_dedupe,
            MovimientoBancario.calcular_hash(mov.fecha, mov.concepto, mov.importe, mov.saldo),
        )


class BloquesPresupuestoTests(TestCase):
    """El `tipo` de la categoría es la agrupación superior con la que se compara
    lo real contra lo presupuestado."""

    def setUp(self):
        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        _crear_categorias_predefinidas(self.hogar)

    def _tipo(self, nombre):
        return CategoriaGasto.objects.get(hogar=self.hogar, nombre=nombre).tipo

    def test_los_cuatro_bloques_de_gasto(self):
        self.assertEqual(self._tipo('Hipoteca / Alquiler'), 'fijo')
        self.assertEqual(self._tipo('IBI'), 'anual')
        self.assertEqual(self._tipo('Alimentacion'), 'variable')
        self.assertEqual(self._tipo('Ocio'), 'discrecional')

    def test_lo_prescindible_es_discrecional(self):
        for nombre in ('Ocio', 'Restaurantes', 'Ropa', 'Suscripciones', 'Tecnologia / Software'):
            self.assertEqual(self._tipo(nombre), 'discrecional', nombre)

    def test_ingresos_y_traspasos_tienen_su_propio_bloque(self):
        for nombre in ('Nomina', 'Intereses', 'Devoluciones', 'Otros ingresos'):
            self.assertEqual(self._tipo(nombre), 'ingreso', nombre)
        self.assertEqual(self._tipo('Traspaso entre cuentas'), 'traspaso')

    def test_recoloca_una_categoria_con_el_tipo_antiguo(self):
        # Simula un hogar creado antes de que existiera el bloque Discrecional.
        CategoriaGasto.objects.filter(hogar=self.hogar, nombre='Ocio').update(tipo='variable')
        _crear_categorias_predefinidas(self.hogar)
        self.assertEqual(self._tipo('Ocio'), 'discrecional')

    def test_no_pisa_una_categoria_del_usuario(self):
        CategoriaGasto.objects.filter(hogar=self.hogar, nombre='Ocio').update(
            tipo='variable', es_predefinida=False,
        )
        _crear_categorias_predefinidas(self.hogar)
        self.assertEqual(self._tipo('Ocio'), 'variable')

    def test_el_presupuesto_no_ofrece_categorias_de_ingreso(self):
        user = User.objects.create_user('presu', password='clave-de-prueba')
        perfil = user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        self.client.force_login(user)

        respuesta = self.client.get(reverse('finanzas:crear_partida'))
        nombres = {c.nombre for c in respuesta.context['categorias']}
        self.assertIn('Alimentacion', nombres)
        self.assertIn('Ocio', nombres)
        self.assertNotIn('Nomina', nombres)
        self.assertNotIn('Traspaso entre cuentas', nombres)


class IngresosTests(TestCase):

    def setUp(self):
        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        self.user = User.objects.create_user(
            username='tester', password='clave-de-prueba',
            first_name='Nombre', last_name='Apellido',
        )
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)

    def importar(self, *fixtures):
        analizados = [
            {'nombre': nombre, 'resultado': analizar_extracto(leer_fixture(nombre))}
            for nombre in fixtures
        ]
        return _importar_analizados(self.hogar, self.user, 'Banco', None, analizados)

    def test_la_nomina_se_categoriza_como_ingreso(self):
        self.importar('caixabank.csv')
        nomina = MovimientoBancario.objects.get(hogar=self.hogar, concepto__startswith='NOMINA')
        self.assertEqual(nomina.categoria.nombre, 'Nomina')
        self.assertEqual(nomina.categoria.tipo, 'ingreso')

    def test_los_positivos_usan_su_propio_diccionario(self):
        # 'Repsol' en negativo es Gasolina; el mismo texto en positivo no puede
        # serlo, porque es dinero que entra.
        self.assertEqual(categorizar_por_codigo('Repsol'), 'Gasolina')
        self.assertIsNone(categorizar_por_codigo('Repsol', es_ingreso=True))
        self.assertEqual(categorizar_por_codigo('NOMINA TRF', es_ingreso=True), 'Nomina')
        self.assertEqual(
            categorizar_por_codigo('Interes neto pagado', es_ingreso=True), 'Intereses',
        )

    def test_un_abono_de_un_comercio_conocido_es_devolucion(self):
        # Steam aparece antes como gasto, así que el abono posterior no es un
        # ingreso nuevo: es dinero que vuelve.
        extracto = ExtractoBancario.objects.create(hogar=self.hogar, usuario=self.user)
        MovimientoBancario.objects.create(
            extracto=extracto, hogar=self.hogar, fecha=date(2026, 7, 10),
            concepto='Steam', importe=Decimal('-4.99'),
        )
        analizados = [{
            'nombre': 'abono.csv',
            'resultado': {
                'movimientos': [{
                    'fecha': date(2026, 7, 17), 'concepto': 'Steam',
                    'concepto_raw': 'Steam', 'importe': Decimal('4.99'), 'saldo': None,
                }],
                'filas_error': [], 'filas_omitidas': [],
            },
        }]
        _importar_analizados(self.hogar, self.user, 'Banco', None, analizados)

        abono = MovimientoBancario.objects.get(hogar=self.hogar, importe=Decimal('4.99'))
        self.assertEqual(abono.categoria.nombre, 'Devoluciones')

    def test_reconoce_la_devolucion_aunque_venga_en_el_mismo_extracto(self):
        # La compra y su reembolso suelen aparecer en el mismo archivo, así que
        # no basta con mirar lo ya importado.
        analizados = [{
            'nombre': 'compra_y_abono.csv',
            'resultado': {
                'movimientos': [
                    {'fecha': date(2026, 7, 10), 'concepto': 'Steam',
                     'concepto_raw': 'Steam', 'importe': Decimal('-4.99'), 'saldo': None},
                    {'fecha': date(2026, 7, 17), 'concepto': 'Steam',
                     'concepto_raw': 'Steam', 'importe': Decimal('4.99'), 'saldo': None},
                ],
                'filas_error': [], 'filas_omitidas': [],
            },
        }]
        _importar_analizados(self.hogar, self.user, 'Banco', None, analizados)

        self.assertEqual(
            MovimientoBancario.objects.get(hogar=self.hogar, importe=Decimal('4.99')).categoria.nombre,
            'Devoluciones',
        )
        self.assertEqual(
            MovimientoBancario.objects.get(hogar=self.hogar, importe=Decimal('-4.99')).categoria.nombre,
            'Ocio',
        )

    def test_los_traspasos_se_compensan_y_no_tocan_ingreso_ni_gasto(self):
        self.importar('revolut.csv')
        # El otro lado del traspaso, como si se cargara la cuenta de la pareja.
        extracto = ExtractoBancario.objects.create(hogar=self.hogar, usuario=self.user)
        MovimientoBancario.objects.create(
            extracto=extracto, hogar=self.hogar, fecha=date(2026, 7, 4),
            concepto='Transferencia de NOMBRE APELLIDO', importe=Decimal('80.00'),
            es_traspaso=True,
        )

        peticion = RequestFactory().get('/')
        todos = list(MovimientoBancario.objects.filter(hogar=self.hogar).select_related('categoria'))
        panel = _panel_context(self.hogar, todos, peticion)

        self.assertEqual(panel['kpi_traspaso_neto'], Decimal('0.00'))
        self.assertTrue(panel['traspasos_cuadran'])
        # Ni el +80 ni el −80 aparecen en los KPIs de ingreso/gasto.
        self.assertNotIn(Decimal('80.00'), [panel['kpi_ingresos']])
        gastos_sin_traspaso = sum(
            (m.importe for m in todos if m.importe < 0 and not m.es_traspaso), Decimal('0'),
        )
        self.assertEqual(panel['kpi_gastos'], gastos_sin_traspaso)

    def test_el_panel_desglosa_por_bloque_del_presupuesto(self):
        self.importar('revolut.csv')
        peticion = RequestFactory().get('/')
        todos = list(MovimientoBancario.objects.filter(hogar=self.hogar).select_related('categoria'))
        panel = _panel_context(self.hogar, todos, peticion)

        etiquetas = [b['etiqueta'] for b in panel['bloques']]
        self.assertIn('Variables', etiquetas)
        self.assertIn('Discrecionales', etiquetas)
        # Los porcentajes reparten el 100 % del gasto observado.
        self.assertAlmostEqual(sum(b['pct'] for b in panel['bloques']), 100, delta=0.5)

    def test_los_ingresos_sin_clasificar_salen_en_sin_categorizar(self):
        extracto = ExtractoBancario.objects.create(hogar=self.hogar, usuario=self.user)
        MovimientoBancario.objects.create(
            extracto=extracto, hogar=self.hogar, fecha=date(2026, 7, 3),
            concepto='Ingreso raro de alguien', importe=Decimal('250.00'),
        )
        self.client.force_login(self.user)
        respuesta = self.client.get(reverse('extractos:sin_categorizar'))
        grupos = {g['comercio']: g for g in respuesta.context['grupos']}
        self.assertIn('ingreso raro de alguien', grupos)
        self.assertTrue(grupos['ingreso raro de alguien']['es_ingreso'])


class ConciliacionTests(TestCase):

    def setUp(self):
        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        self.user = User.objects.create_user(username='tester', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        self.extracto = ExtractoBancario.objects.create(hogar=self.hogar, usuario=self.user)
        self.client.force_login(self.user)

    def gasto(self, nombre_cat, importe, dia):
        cat = CategoriaGasto.objects.get(hogar=self.hogar, nombre=nombre_cat)
        MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(2026, 7, dia),
            concepto=f'{nombre_cat} {dia}', importe=Decimal(importe), categoria=cat,
        )

    def presupuestar(self, nombre_cat, importe):
        cat = CategoriaGasto.objects.get(hogar=self.hogar, nombre=nombre_cat)
        PartidaGasto.objects.create(
            hogar=self.hogar, categoria=cat, nombre=f'Presupuesto {nombre_cat}',
            importe=Decimal(importe), periodicidad='mensual',
        )

    def test_agrupa_las_categorias_en_los_bloques_del_presupuesto(self):
        self.presupuestar('Alimentacion', '400')
        self.presupuestar('Ocio', '100')
        self.presupuestar('Restaurantes', '150')
        self.gasto('Alimentacion', '-380', 5)
        self.gasto('Ocio', '-130', 6)
        self.gasto('Restaurantes', '-170', 7)

        respuesta = self.client.get(reverse('extractos:conciliacion'))
        bloques = {b['etiqueta']: b for b in respuesta.context['bloques']}

        self.assertEqual(bloques['Variables']['declarado'], Decimal('400'))
        self.assertEqual(bloques['Variables']['observado'], Decimal('380'))
        # Ocio y Restaurantes suman en el mismo bloque sin perder su categoría.
        self.assertEqual(bloques['Discrecionales']['declarado'], Decimal('250'))
        self.assertEqual(bloques['Discrecionales']['observado'], Decimal('300'))
        self.assertEqual(bloques['Discrecionales']['diferencia'], Decimal('50'))
        self.assertEqual(
            {f['categoria'] for f in bloques['Discrecionales']['filas']},
            {'Ocio', 'Restaurantes'},
        )

    def test_los_bloques_van_en_el_orden_del_presupuesto(self):
        self.presupuestar('Hipoteca / Alquiler', '900')
        self.presupuestar('IBI', '120')
        self.presupuestar('Alimentacion', '400')
        self.presupuestar('Ocio', '100')
        for nombre in ('Hipoteca / Alquiler', 'IBI', 'Alimentacion', 'Ocio'):
            self.gasto(nombre, '-50', 5)

        respuesta = self.client.get(reverse('extractos:conciliacion'))
        self.assertEqual(
            [b['etiqueta'] for b in respuesta.context['bloques']],
            ['Fijos', 'Fijos anuales', 'Variables', 'Discrecionales'],
        )

    def test_los_traspasos_no_entran_en_la_comparacion(self):
        self.presupuestar('Alimentacion', '400')
        self.gasto('Alimentacion', '-380', 5)
        cat_traspaso = CategoriaGasto.objects.get(hogar=self.hogar, nombre='Traspaso entre cuentas')
        MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(2026, 7, 9),
            concepto='Transferencia a mi otra cuenta', importe=Decimal('-2000'),
            categoria=cat_traspaso, es_traspaso=True,
        )

        respuesta = self.client.get(reverse('extractos:conciliacion'))
        self.assertEqual(respuesta.context['total_observado'], Decimal('380'))


class DuplicadosTests(TestCase):

    def setUp(self):
        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        self.user = User.objects.create_user(username='tester', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        self.client.force_login(self.user)

    def _subir(self, *fixtures):
        sesion = self.client.session
        sesion['extractos_pendientes'] = [
            {'nombre': f, 'texto': leer_fixture(f)} for f in fixtures
        ]
        sesion['extractos_pendientes_meta'] = {'nombre_banco': 'Banco', 'cuenta_id': None}
        sesion.save()
        return self.client.get(reverse('extractos:revisar'))

    def test_la_revision_avisa_de_lo_que_ya_esta_importado(self):
        primera = self._subir('caixabank.csv')
        self.assertEqual(primera.context['total_duplicados'], 0)
        self.assertEqual(primera.context['total_nuevos'], primera.context['total_ok'])

        analizados = [{
            'nombre': 'caixabank.csv',
            'resultado': analizar_extracto(leer_fixture('caixabank.csv')),
        }]
        _importar_analizados(self.hogar, self.user, 'Banco', None, analizados)

        segunda = self._subir('caixabank.csv')
        self.assertEqual(segunda.context['total_nuevos'], 0)
        self.assertEqual(segunda.context['total_duplicados'], segunda.context['total_ok'])
        self.assertTrue(all(m['ya_existe'] for a in segunda.context['archivos'] for m in a['preview']))

    def test_el_mismo_archivo_dos_veces_en_el_lote_solo_cuenta_una(self):
        respuesta = self._subir('caixabank.csv', 'caixabank.csv')
        total = respuesta.context['total_ok']
        self.assertEqual(respuesta.context['total_nuevos'], total / 2)
        self.assertEqual(respuesta.context['total_duplicados'], total / 2)


class AprendizajeTests(TestCase):

    def setUp(self):
        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        self.user = User.objects.create_user(username='tester', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        self.ocio = CategoriaGasto.objects.get(hogar=self.hogar, nombre='Ocio')
        self.extracto = ExtractoBancario.objects.create(
            hogar=self.hogar, usuario=self.user, nombre_banco='Banco',
        )
        self.client.force_login(self.user)
        self._dia = 0

    def crear_movimiento(self, concepto, importe='-10.00'):
        # Cada movimiento va en un día distinto: la deduplicación por hash
        # rechazaría dos apuntes idénticos, que es justo lo que debe hacer.
        self._dia += 1
        return MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar,
            fecha=date(2026, 7, self._dia),
            concepto=concepto, importe=Decimal(importe),
        )

    def test_aprender_aplica_a_los_similares_y_recuerda(self):
        for _ in range(3):
            self.crear_movimiento('Malacabeza')
        self.crear_movimiento('Otro sitio cualquiera')

        respuesta = self.client.post(reverse('extractos:aprender_regla'), {
            'patron': 'malacabeza', 'categoria_id': self.ocio.id,
        })
        self.assertEqual(respuesta.status_code, 302)

        self.assertEqual(
            MovimientoBancario.objects.filter(hogar=self.hogar, categoria=self.ocio).count(), 3,
        )
        self.assertTrue(
            ReglaCategorizacion.objects.filter(
                hogar=self.hogar, patron='malacabeza', categoria=self.ocio, origen='manual',
            ).exists()
        )
        # El que no encaja se queda como estaba.
        self.assertIsNone(
            MovimientoBancario.objects.get(concepto='Otro sitio cualquiera').categoria,
        )

    def test_la_regla_aprendida_se_aplica_en_la_siguiente_importacion(self):
        self.client.post(reverse('extractos:aprender_regla'), {
            'patron': 'malacabeza', 'categoria_id': self.ocio.id,
        })
        analizados = [{
            'nombre': 'nuevo.csv',
            'resultado': {
                'movimientos': [{
                    'fecha': '2026-08-01', 'concepto': 'MALACABEZA SEVILLA',
                    'concepto_raw': 'MALACABEZA SEVILLA',
                    'importe': Decimal('-22.00'), 'saldo': None,
                }],
                'filas_error': [], 'filas_omitidas': [],
            },
        }]
        _importar_analizados(self.hogar, self.user, 'Banco', None, analizados)

        mov = MovimientoBancario.objects.get(concepto='MALACABEZA SEVILLA')
        self.assertEqual(mov.categoria, self.ocio)
        self.assertEqual(mov.estado_categorizacion, 'por_regla')

    def test_aprender_no_pisa_lo_ya_categorizado(self):
        ya = self.crear_movimiento('Malacabeza')
        alimentacion = CategoriaGasto.objects.get(hogar=self.hogar, nombre='Alimentacion')
        ya.categoria = alimentacion
        ya.estado_categorizacion = 'manual'
        ya.save()
        self.crear_movimiento('Malacabeza Centro')

        self.client.post(reverse('extractos:aprender_regla'), {
            'patron': 'malacabeza', 'categoria_id': self.ocio.id,
        })

        ya.refresh_from_db()
        self.assertEqual(ya.categoria, alimentacion)
        self.assertEqual(
            MovimientoBancario.objects.get(concepto='Malacabeza Centro').categoria, self.ocio,
        )

    def test_editar_categoria_sugiere_aplicar_a_los_similares(self):
        primero = self.crear_movimiento('Malacabeza')
        self.crear_movimiento('Malacabeza')
        self.crear_movimiento('Malacabeza')

        respuesta = self.client.post(
            reverse('extractos:actualizar_movimiento', args=[primero.id]),
            {'categoria_id': self.ocio.id},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        datos = respuesta.json()
        self.assertTrue(datos['ok'])
        self.assertEqual(datos['sugerencia']['patron'], 'malacabeza')
        self.assertEqual(datos['sugerencia']['n_similares'], 2)
        # Ofrecer no es aplicar: sin confirmación no se crea ninguna regla.
        self.assertFalse(ReglaCategorizacion.objects.filter(hogar=self.hogar).exists())

    def test_pantalla_sin_categorizar_agrupa_por_comercio(self):
        for _ in range(3):
            self.crear_movimiento('Malacabeza')
        self.crear_movimiento('Otro sitio cualquiera')

        respuesta = self.client.get(reverse('extractos:sin_categorizar'))
        self.assertEqual(respuesta.status_code, 200)
        grupos = {g['comercio']: g for g in respuesta.context['grupos']}
        self.assertEqual(grupos['malacabeza']['num'], 3)
        self.assertEqual(grupos['malacabeza']['total'], Decimal('-30.00'))

    def test_desactivar_una_regla_la_deja_de_aplicar(self):
        self.client.post(reverse('extractos:aprender_regla'), {
            'patron': 'malacabeza', 'categoria_id': self.ocio.id,
        })
        regla = ReglaCategorizacion.objects.get(hogar=self.hogar, patron='malacabeza')
        self.client.post(reverse('extractos:reglas'), {
            'accion': 'alternar', 'regla_id': regla.id,
        })
        regla.refresh_from_db()
        self.assertFalse(regla.activo)
