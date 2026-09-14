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

from .analisis import analizar_mes
from .categorizacion import categorizar_por_codigo
from .models import Etiqueta, ExtractoBancario, MovimientoBancario, ReglaCategorizacion
from .normalizacion import (
    contiene_patron, es_traspaso_interno, normalizar_comercio, normalizar_texto,
)
from .parser import analizar_extracto
from .views import _importar_analizados, _marcar_duplicados, _panel_context

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

    def _analizado(self, nombre, filas):
        """Un extracto sintético: (dia, concepto, importe, saldo)."""
        return {
            'nombre': nombre,
            'resultado': {
                'movimientos': [{
                    'fecha': f'2026-08-{dia:02d}', 'concepto': concepto,
                    'concepto_raw': concepto, 'importe': Decimal(importe),
                    'saldo': Decimal(saldo) if saldo is not None else None,
                } for dia, concepto, importe, saldo in filas],
                'filas_error': [], 'filas_omitidas': [],
            },
        }

    def _revisar_analizado(self, analizado):
        """Pasa un extracto sintético por el marcado de duplicados de la revisión."""
        _marcar_duplicados(self.hogar, [analizado])
        return analizado['resultado']['movimientos']

    def test_reimportar_el_mes_a_medias_solo_trae_lo_nuevo(self):
        """El caso real: se importa agosto a mitad de mes para ver cómo va y se
        vuelve a importar más adelante con el mes más completo. Lo ya guardado
        se marca como duplicado y solo entran los apuntes nuevos."""
        primeros = [
            (1, 'Supermercado Dia', '-42.10', '1000.00'),
            (3, 'Gasolinera Repsol', '-60.00', '940.00'),
        ]
        _importar_analizados(
            self.hogar, self.user, 'Banco', None, [self._analizado('agosto.csv', primeros)],
        )
        self.assertEqual(MovimientoBancario.objects.filter(hogar=self.hogar).count(), 2)

        mes_completo = primeros + [
            (12, 'Farmacia Ronda', '-14.00', '926.00'),
            (20, 'Restaurante La Plaza', '-35.50', '890.50'),
        ]
        segundo = self._analizado('agosto.csv', mes_completo)

        # Lo que ve el usuario en la pantalla de revisión antes de confirmar.
        movs = self._revisar_analizado(segundo)
        self.assertEqual([m['ya_existe'] for m in movs], [True, True, False, False])

        totales = _importar_analizados(
            self.hogar, self.user, 'Banco', None, [segundo],
        )
        self.assertEqual(totales['total_creados'], 2)
        self.assertEqual(totales['total_duplicados'], 2)
        self.assertEqual(MovimientoBancario.objects.filter(hogar=self.hogar).count(), 4)

    def test_el_tercer_intento_del_mismo_mes_no_duplica_nada(self):
        """Reimportar el mes ya completo no crea nada: ni un extracto vacío."""
        filas = [
            (1, 'Supermercado Dia', '-42.10', '1000.00'),
            (3, 'Gasolinera Repsol', '-60.00', '940.00'),
        ]
        for _ in range(3):
            _importar_analizados(
                self.hogar, self.user, 'Banco', None, [self._analizado('agosto.csv', filas)],
            )

        self.assertEqual(MovimientoBancario.objects.filter(hogar=self.hogar).count(), 2)
        self.assertEqual(ExtractoBancario.objects.filter(hogar=self.hogar).count(), 1)

    def test_la_revision_no_deja_confirmar_cuando_todo_esta_repetido(self):
        """Si no queda nada nuevo, la pantalla lo dice y el botón va deshabilitado:
        el usuario no puede confirmar una importación que no importaría nada."""
        primera = self._subir('caixabank.csv')
        analizados = [{
            'nombre': 'caixabank.csv',
            'resultado': analizar_extracto(leer_fixture('caixabank.csv')),
        }]
        _importar_analizados(self.hogar, self.user, 'Banco', None, analizados)

        segunda = self._subir('caixabank.csv')
        self.assertEqual(segunda.context['total_nuevos'], 0)
        contenido = segunda.content.decode('utf-8')
        self.assertIn('no se volverán a importar', contenido)
        self.assertIn('No queda nada nuevo que importar', contenido)
        self.assertIn('Ya los tienes', contenido)
        self.assertIn('Confirmar importación (0)', contenido)
        self.assertIn('Ya tienes importados todos estos movimientos', contenido)
        self.assertNotEqual(primera.context['total_nuevos'], 0)


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

    def test_la_sugerencia_cuenta_tambien_los_ya_clasificados_y_acota_el_mes(self):
        """Corregir un comercio suele querer decir corregir también lo que se
        clasificó mal antes, no solo lo que quedó en blanco."""
        alimentacion = CategoriaGasto.objects.get(hogar=self.hogar, nombre='Alimentacion')
        objetivo = self.crear_movimiento('Malacabeza')
        mal = self.crear_movimiento('Malacabeza')
        mal.categoria = alimentacion
        mal.estado_categorizacion = 'manual'
        mal.save()
        # Uno del mes anterior: cuenta en el total pero no en el mes revisado.
        MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(2026, 6, 15),
            concepto='Malacabeza', importe=Decimal('-18.00'),
        )

        respuesta = self.client.post(
            reverse('extractos:actualizar_movimiento', args=[objetivo.id]),
            {'categoria_id': self.ocio.id},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        sugerencia = respuesta.json()['sugerencia']

        self.assertEqual(sugerencia['n_similares'], 2)
        self.assertEqual(sugerencia['n_mes'], 1)
        self.assertEqual(sugerencia['n_ya_clasificados'], 1)
        self.assertEqual(sugerencia['etiqueta_mes'], 'Julio 2026')
        self.assertEqual(sugerencia['categoria'], 'Ocio')

    def test_no_se_sugiere_nada_cuando_no_queda_ningun_similar_por_cambiar(self):
        solo = self.crear_movimiento('Malacabeza')
        respuesta = self.client.post(
            reverse('extractos:actualizar_movimiento', args=[solo.id]),
            {'categoria_id': self.ocio.id},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertIsNone(respuesta.json()['sugerencia'])

    def test_aplicar_solo_al_mes_no_toca_el_resto_ni_crea_regla(self):
        julio = self.crear_movimiento('Malacabeza')
        junio = MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(2026, 6, 15),
            concepto='Malacabeza', importe=Decimal('-18.00'),
        )

        respuesta = self.client.post(reverse('extractos:aprender_regla'), {
            'patron': 'malacabeza', 'categoria_id': self.ocio.id,
            'incluir_categorizados': '1', 'ambito': 'mes', 'anio': 2026, 'mes': 7,
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

        datos = respuesta.json()
        self.assertTrue(datos['ok'])
        self.assertEqual(datos['aplicados'], 1)
        self.assertFalse(datos['recordada'])

        julio.refresh_from_db(); junio.refresh_from_db()
        self.assertEqual(julio.categoria, self.ocio)
        self.assertIsNone(junio.categoria)
        # Acotar a un mes no puede dejar una regla que recategorice el futuro.
        self.assertFalse(ReglaCategorizacion.objects.filter(hogar=self.hogar).exists())

    def test_aplicar_a_todos_reclasifica_tambien_lo_ya_categorizado(self):
        alimentacion = CategoriaGasto.objects.get(hogar=self.hogar, nombre='Alimentacion')
        mal = self.crear_movimiento('Malacabeza')
        mal.categoria = alimentacion
        mal.estado_categorizacion = 'manual'
        mal.save()
        junio = MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(2026, 6, 15),
            concepto='Malacabeza', importe=Decimal('-18.00'),
        )

        respuesta = self.client.post(reverse('extractos:aprender_regla'), {
            'patron': 'malacabeza', 'categoria_id': self.ocio.id,
            'incluir_categorizados': '1', 'ambito': 'todos',
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

        self.assertTrue(respuesta.json()['recordada'])
        mal.refresh_from_db(); junio.refresh_from_db()
        self.assertEqual(mal.categoria, self.ocio)
        self.assertEqual(junio.categoria, self.ocio)
        self.assertTrue(
            ReglaCategorizacion.objects.filter(hogar=self.hogar, patron='malacabeza').exists()
        )

    def test_lo_que_se_ofrece_es_lo_que_se_cambia(self):
        """El aviso contaba comercios idénticos pero el cambio se aplica por
        patrón: ofrecía «2» y cambiaba 3, porque «MALACABEZA CENTRO» también
        contiene el patrón."""
        objetivo = self.crear_movimiento('Malacabeza')
        self.crear_movimiento('Malacabeza')
        self.crear_movimiento('MALACABEZA CENTRO')

        respuesta = self.client.post(
            reverse('extractos:actualizar_movimiento', args=[objetivo.id]),
            {'categoria_id': self.ocio.id},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        ofrecidos = respuesta.json()['sugerencia']['n_similares']

        aplicados = self.client.post(reverse('extractos:aprender_regla'), {
            'patron': 'malacabeza', 'categoria_id': self.ocio.id,
            'incluir_categorizados': '1', 'ambito': 'todos', 'recordar': '0',
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest').json()['aplicados']

        self.assertEqual(ofrecidos, 2)
        self.assertEqual(aplicados, ofrecidos)

    def test_recordar_despues_crea_la_regla_sin_tocar_movimientos(self):
        """Aplicar y recordar son dos decisiones: el listado aplica primero y
        pregunta después si además debe quedarse así para siempre."""
        alimentacion = CategoriaGasto.objects.get(hogar=self.hogar, nombre='Alimentacion')
        otro = self.crear_movimiento('Malacabeza')
        otro.categoria = alimentacion
        otro.save()

        respuesta = self.client.post(reverse('extractos:aprender_regla'), {
            'patron': 'malacabeza', 'categoria_id': self.ocio.id, 'accion': 'solo_regla',
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

        datos = respuesta.json()
        self.assertTrue(datos['recordada'])
        self.assertEqual(datos['aplicados'], 0)
        self.assertTrue(
            ReglaCategorizacion.objects.filter(
                hogar=self.hogar, patron='malacabeza', categoria=self.ocio,
            ).exists()
        )
        # Crear la regla no reclasifica nada por su cuenta.
        otro.refresh_from_db()
        self.assertEqual(otro.categoria, alimentacion)

    def test_aplicar_desde_el_listado_no_recuerda_por_su_cuenta(self):
        self.crear_movimiento('Malacabeza')
        self.client.post(reverse('extractos:aprender_regla'), {
            'patron': 'malacabeza', 'categoria_id': self.ocio.id,
            'ambito': 'todos', 'incluir_categorizados': '1', 'recordar': '0',
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

        self.assertFalse(ReglaCategorizacion.objects.filter(hogar=self.hogar).exists())

    def test_un_mes_invalido_no_aplica_nada(self):
        self.crear_movimiento('Malacabeza')
        respuesta = self.client.post(reverse('extractos:aprender_regla'), {
            'patron': 'malacabeza', 'categoria_id': self.ocio.id, 'ambito': 'mes',
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

        self.assertEqual(respuesta.status_code, 400)
        self.assertEqual(
            MovimientoBancario.objects.filter(hogar=self.hogar, categoria__isnull=False).count(), 0,
        )

    def test_sin_categorizar_categoriza_todo_el_comercio_de_toda_la_app(self):
        """Lo que se envía desde «Sin categorizar» es exactamente el formulario
        de la plantilla: categoría + patrón, sin ámbito. Debe alcanzar a los
        movimientos de ese comercio en CUALQUIER mes y CUALQUIER extracto —no
        uno por uno— y dejar la regla puesta."""
        otro_extracto = ExtractoBancario.objects.create(
            hogar=self.hogar, usuario=self.user, nombre_banco='Otro banco',
        )
        MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(2026, 3, 4),
            concepto='MALACABEZA SEVILLA', importe=Decimal('-12.00'),
        )
        MovimientoBancario.objects.create(
            extracto=otro_extracto, hogar=self.hogar, fecha=date(2025, 11, 20),
            concepto='Compra en Malacabeza', importe=Decimal('-31.50'),
        )
        MovimientoBancario.objects.create(
            extracto=otro_extracto, hogar=self.hogar, fecha=date(2026, 8, 9),
            concepto='malacabeza centro', importe=Decimal('-9.00'),
        )
        ajeno = self.crear_movimiento('Otro sitio cualquiera')

        # El patrón que ofrece la plantilla es el comercio normalizado del grupo.
        grupos = {g['comercio']: g for g in
                  self.client.get(reverse('extractos:sin_categorizar')).context['grupos']}
        self.assertIn('malacabeza', grupos)

        respuesta = self.client.post(reverse('extractos:aprender_regla'), {
            'categoria_id': self.ocio.id, 'patron': grupos['malacabeza']['comercio'],
        }, follow=True)
        self.assertEqual(respuesta.status_code, 200)

        categorizados = MovimientoBancario.objects.filter(hogar=self.hogar, categoria=self.ocio)
        self.assertEqual(categorizados.count(), 3)
        # Cubre los dos extractos y los tres meses distintos, no solo el revisado.
        self.assertEqual(
            {(m.fecha.year, m.fecha.month) for m in categorizados},
            {(2026, 3), (2025, 11), (2026, 8)},
        )
        self.assertEqual({m.estado_categorizacion for m in categorizados}, {'por_regla'})
        self.assertTrue(
            ReglaCategorizacion.objects.filter(
                hogar=self.hogar, patron='malacabeza', categoria=self.ocio,
                origen='manual', activo=True,
            ).exists()
        )
        ajeno.refresh_from_db()
        self.assertIsNone(ajeno.categoria)
        # Y el grupo desaparece de la pantalla: no queda nada que repasar.
        restantes = self.client.get(reverse('extractos:sin_categorizar')).context['grupos']
        self.assertNotIn('malacabeza', {g['comercio'] for g in restantes})

    def test_sin_categorizar_arrastra_tambien_los_comercios_parecidos_marcados(self):
        """Los «también parecidos» viajan como patrones extra del mismo envío:
        una sola pasada debe cubrirlos y recordarlos todos."""
        MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(2026, 5, 2),
            concepto='Farmacia Ronda', importe=Decimal('-14.00'),
        )
        MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(2026, 5, 3),
            concepto='Farmacia Rondo', importe=Decimal('-8.00'),
        )

        self.client.post(reverse('extractos:aprender_regla'), {
            'categoria_id': self.ocio.id,
            'patron': ['farmacia ronda', 'farmacia rondo'],
        })

        self.assertEqual(
            MovimientoBancario.objects.filter(hogar=self.hogar, categoria=self.ocio).count(), 2,
        )
        self.assertEqual(
            set(ReglaCategorizacion.objects.filter(hogar=self.hogar).values_list('patron', flat=True)),
            {'farmacia ronda', 'farmacia rondo'},
        )


class ComputoDeCategoriaTests(TestCase):
    """El cómputo de la categoría (resta / suma / neutra) es lo que decide cómo
    entra cada movimiento en los totales.

    Antes mandaba el signo del importe, así que un traspaso a otra cuenta propia
    —o el pago de la tarjeta— se sumaba al gasto del mes aunque tuviera su
    categoría de traspaso puesta.
    """

    def setUp(self):
        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        self.user = User.objects.create_user(username='tester', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        self.extracto = ExtractoBancario.objects.create(hogar=self.hogar, usuario=self.user)
        self.client.force_login(self.user)
        self._dia = 0

    def movimiento(self, concepto, importe, categoria=None, es_traspaso=False):
        self._dia += 1
        cat = (
            CategoriaGasto.objects.get(hogar=self.hogar, nombre=categoria)
            if categoria else None
        )
        return MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(2026, 7, self._dia),
            concepto=concepto, importe=Decimal(importe), categoria=cat,
            es_traspaso=es_traspaso,
        )

    def panel(self):
        todos = list(
            MovimientoBancario.objects.filter(hogar=self.hogar).select_related('categoria')
        )
        return _panel_context(self.hogar, todos, RequestFactory().get('/'))

    def test_un_traspaso_categorizado_no_suma_al_gasto(self):
        """El caso que motivó el cambio: el traspaso trae su categoría puesta
        pero no viene marcado como traspaso interno (lo clasificó el usuario)."""
        self.movimiento('Compra semanal', '-100', 'Alimentacion')
        self.movimiento('A mi cuenta de ahorro', '-500', 'Traspaso entre cuentas')

        panel = self.panel()
        self.assertEqual(panel['kpi_gastos'], Decimal('-100'))
        self.assertEqual(panel['kpi_traspaso_neto'], Decimal('-500'))
        # El desglose es por BLOQUE del presupuesto, con la categoría dentro:
        # es la lectura que se puede conciliar con lo declarado.
        self.assertEqual([b['etiqueta'] for b in panel['bloques']], ['Variables'])
        self.assertEqual(
            [c['nombre'] for c in panel['bloques'][0]['categorias']], ['Alimentacion'],
        )
        self.assertEqual([d['nombre'] for d in panel['donut']], ['Variables'])

    def test_una_categoria_propia_marcada_como_neutra_tampoco_cuenta(self):
        cat = CategoriaGasto.objects.create(
            hogar=self.hogar, nombre='Pago tarjeta', tipo='variable', computo='neutro',
        )
        self.movimiento('Compra semanal', '-100', 'Alimentacion')
        mov = self.movimiento('Liquidacion tarjeta', '-320')
        mov.categoria = cat
        mov.save()

        panel = self.panel()
        self.assertEqual(panel['kpi_gastos'], Decimal('-100'))
        self.assertEqual(panel['num_traspasos'], 1)

    def test_un_ingreso_no_deja_de_serlo_por_su_categoria(self):
        self.movimiento('Nomina julio', '2000', 'Nomina')
        self.movimiento('Compra semanal', '-100', 'Alimentacion')

        panel = self.panel()
        self.assertEqual(panel['kpi_ingresos'], Decimal('2000'))
        self.assertEqual(panel['kpi_neto'], Decimal('1900'))

    def test_un_abono_en_una_categoria_de_gasto_resta_de_esa_categoria(self):
        """Una devolución dentro de una categoría de gasto no es un ingreso: es
        gasto que vuelve, así que baja el total de su propia categoría."""
        self.movimiento('Zapatillas', '-80', 'Ropa')
        self.movimiento('Devolucion zapatillas', '30', 'Ropa')

        panel = self.panel()
        self.assertEqual(panel['kpi_gastos'], Decimal('-50'))
        self.assertEqual(panel['kpi_ingresos'], Decimal('0'))
        self.assertEqual(panel['donut'][0]['importe'], 50.0)

    def test_el_neto_del_mes_ignora_los_movimientos_neutros(self):
        self.movimiento('Nomina julio', '2000', 'Nomina')
        self.movimiento('Compra semanal', '-100', 'Alimentacion')
        self.movimiento('A mi cuenta de ahorro', '-500', 'Traspaso entre cuentas')

        grupo = self.panel()['grupos'][0]
        self.assertEqual(grupo['ingresos'], Decimal('2000'))
        self.assertEqual(grupo['gastos'], Decimal('-100'))
        self.assertEqual(grupo['neutro'], Decimal('-500'))
        self.assertEqual(grupo['neto'], Decimal('1900'))

    def test_ocultar_los_neutros_los_saca_del_listado(self):
        self.movimiento('Compra semanal', '-100', 'Alimentacion')
        self.movimiento('A mi cuenta de ahorro', '-500', 'Traspaso entre cuentas')

        todos = list(
            MovimientoBancario.objects.filter(hogar=self.hogar).select_related('categoria')
        )
        panel = _panel_context(
            self.hogar, todos, RequestFactory().get('/', {'traspasos': '0'}),
        )
        self.assertEqual(panel['kpi_num'], 1)

    def test_la_conciliacion_ignora_las_categorias_neutras(self):
        cat = CategoriaGasto.objects.get(hogar=self.hogar, nombre='Gimnasio')
        cat.computo = 'neutro'
        cat.save(update_fields=['computo'])
        alimentacion = CategoriaGasto.objects.get(hogar=self.hogar, nombre='Alimentacion')
        PartidaGasto.objects.create(
            hogar=self.hogar, categoria=alimentacion, nombre='Super',
            importe=Decimal('400'), periodicidad='mensual',
        )
        self.movimiento('Compra semanal', '-380', 'Alimentacion')
        self.movimiento('Cuota gimnasio', '-30', 'Gimnasio')

        respuesta = self.client.get(reverse('extractos:conciliacion'))
        self.assertEqual(respuesta.context['total_observado'], Decimal('380'))
        self.assertEqual(respuesta.context['total_declarado'], Decimal('400'))

    def test_sin_categoria_sigue_mandando_el_signo(self):
        self.movimiento('Comercio desconocido', '-40')
        self.movimiento('Abono desconocido', '15')

        panel = self.panel()
        self.assertEqual(panel['kpi_gastos'], Decimal('-40'))
        self.assertEqual(panel['kpi_ingresos'], Decimal('15'))
        self.assertEqual(panel['kpi_sin_categorizar'], 2)


class ConciliacionPorMesTests(TestCase):
    """La conciliación se mira mes a mes: comparar el presupuesto contra la
    media de todo el histórico esconde justo lo que interesa ver."""

    def setUp(self):
        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        self.user = User.objects.create_user(username='tester', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        self.extracto = ExtractoBancario.objects.create(hogar=self.hogar, usuario=self.user)
        self.client.force_login(self.user)
        self.alimentacion = CategoriaGasto.objects.get(hogar=self.hogar, nombre='Alimentacion')
        PartidaGasto.objects.create(
            hogar=self.hogar, categoria=self.alimentacion, nombre='Super',
            importe=Decimal('400'), periodicidad='mensual',
        )

    def gasto(self, importe, fecha, categoria=None):
        MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=fecha,
            concepto=f'Compra {fecha}', importe=Decimal(importe),
            categoria=categoria or self.alimentacion,
        )

    def test_por_defecto_se_abre_en_el_ultimo_mes_con_datos(self):
        self.gasto('-300', date(2026, 6, 10))
        self.gasto('-500', date(2026, 7, 10))

        respuesta = self.client.get(reverse('extractos:conciliacion'))

        self.assertEqual(respuesta.context['periodo']['etiqueta'], 'Julio 2026')
        self.assertTrue(respuesta.context['periodo']['es_mes'])
        self.assertEqual(respuesta.context['total_observado'], Decimal('500'))
        self.assertEqual(respuesta.context['total_declarado'], Decimal('400'))

    def test_se_puede_mirar_otro_mes(self):
        self.gasto('-300', date(2026, 6, 10))
        self.gasto('-500', date(2026, 7, 10))

        respuesta = self.client.get(
            reverse('extractos:conciliacion'), {'anio': '2026', 'mes': '6'},
        )
        self.assertEqual(respuesta.context['periodo']['etiqueta'], 'Junio 2026')
        self.assertEqual(respuesta.context['total_observado'], Decimal('300'))

    def test_todos_los_meses_siguen_dando_la_media(self):
        self.gasto('-300', date(2026, 6, 10))
        self.gasto('-500', date(2026, 7, 10))

        respuesta = self.client.get(
            reverse('extractos:conciliacion'), {'anio': 'all', 'mes': 'all'},
        )
        self.assertFalse(respuesta.context['periodo']['es_mes'])
        self.assertEqual(respuesta.context['num_meses'], 2)
        self.assertEqual(respuesta.context['total_observado'], Decimal('400'))

    def test_un_anio_entero_promedia_solo_sus_meses(self):
        self.gasto('-300', date(2025, 12, 10))
        self.gasto('-500', date(2026, 7, 10))
        self.gasto('-100', date(2026, 8, 10))

        respuesta = self.client.get(
            reverse('extractos:conciliacion'), {'anio': '2026', 'mes': 'all'},
        )
        self.assertEqual(respuesta.context['num_meses'], 2)
        self.assertEqual(respuesta.context['total_observado'], Decimal('300'))

    def test_los_ingresos_tambien_son_los_del_mes(self):
        nomina = CategoriaGasto.objects.get(hogar=self.hogar, nombre='Nomina')
        self.gasto('2000', date(2026, 6, 25), categoria=nomina)
        self.gasto('3000', date(2026, 7, 25), categoria=nomina)

        respuesta = self.client.get(reverse('extractos:conciliacion'))
        self.assertEqual(respuesta.context['ingreso_observado'], Decimal('3000'))

    def test_una_categoria_declarada_se_concilia_aunque_este_archivada(self):
        """Si hay presupuesto declarado, la fila tiene que salir: si no,
        desaparece gasto comprometido de la comparación sin decir nada."""
        gimnasio = CategoriaGasto.objects.get(hogar=self.hogar, nombre='Gimnasio')
        PartidaGasto.objects.create(
            hogar=self.hogar, categoria=gimnasio, nombre='Cuota',
            importe=Decimal('30'), periodicidad='mensual',
        )
        gimnasio.activo = False
        gimnasio.save(update_fields=['activo'])
        self.gasto('-500', date(2026, 7, 10))

        respuesta = self.client.get(reverse('extractos:conciliacion'))

        categorias = [
            f['categoria'] for b in respuesta.context['bloques'] for f in b['filas']
        ]
        self.assertIn('Gimnasio', categorias)
        self.assertEqual(respuesta.context['total_declarado'], Decimal('430'))


class PanelBuscadorTests(TestCase):
    """El buscador libre del panel: con cientos de apuntes, dar con «ese recibo
    raro» a ojo es lo que convierte la pantalla en un muro de números."""

    def setUp(self):
        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        self.user = User.objects.create_user(username='tester', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        extracto = ExtractoBancario.objects.create(hogar=self.hogar, usuario=self.user)
        for dia, concepto in enumerate(['MERCADONA SEVILLA', 'Recibo Endesa', 'Steam'], 1):
            MovimientoBancario.objects.create(
                extracto=extracto, hogar=self.hogar, fecha=date(2026, 7, dia),
                concepto=concepto, importe=Decimal('-10.00'),
            )
        self.client.force_login(self.user)

    def panel(self, **params):
        return self.client.get(reverse('extractos:listar'), params).context['panel']

    def test_busca_sin_distinguir_mayusculas_ni_acentos(self):
        self.assertEqual(self.panel(q='mercadona')['kpi_num'], 1)
        self.assertEqual(self.panel(q='ENDÉSA')['kpi_num'], 1)

    def test_una_busqueda_sin_resultados_no_rompe_el_panel(self):
        panel = self.panel(q='no existe esto')
        self.assertEqual(panel['kpi_num'], 0)
        self.assertEqual(panel['grupos'], [])
        self.assertTrue(panel['hay_filtro'])

    def test_sin_busqueda_estan_todos(self):
        self.assertEqual(self.panel()['kpi_num'], 3)
        self.assertEqual(self.panel()['periodo_etiqueta'], 'Todo el histórico')

    def test_la_etiqueta_del_periodo_sigue_al_filtro(self):
        self.assertEqual(self.panel(anio='2026', mes='7')['periodo_etiqueta'], 'Julio 2026')
        self.assertEqual(self.panel(anio='2026')['periodo_etiqueta'], '2026')


class AnalisisDelMesTests(TestCase):
    """El motor que responde «en qué se ha ido el mes y qué lo explica»."""

    def setUp(self):
        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        self.user = User.objects.create_user(username='tester', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        self.extracto = ExtractoBancario.objects.create(hogar=self.hogar, usuario=self.user)
        self.client.force_login(self.user)

    def gasto(self, concepto, importe, anio, mes, dia, categoria='Restaurantes'):
        return MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(anio, mes, dia),
            concepto=concepto, importe=Decimal(importe),
            categoria=CategoriaGasto.objects.get(hogar=self.hogar, nombre=categoria) if categoria else None,
        )

    def analizar(self, anio=2026, mes=8, **kwargs):
        todos = list(
            MovimientoBancario.objects.filter(hogar=self.hogar)
            .select_related('categoria').prefetch_related('etiquetas')
        )
        return analizar_mes(todos, anio, mes, **kwargs)

    def _tres_meses_normales_y_un_agosto_caro(self):
        for mes in (5, 6, 7):
            self.gasto('Glovo pedido', '-50', 2026, mes, 5)
            self.gasto('Mercadona', '-200', 2026, mes, 10, categoria='Alimentacion')
        self.gasto('Glovo pedido', '-250', 2026, 8, 5)
        self.gasto('Mercadona', '-210', 2026, 8, 10, categoria='Alimentacion')

    def test_compara_contra_la_media_de_los_meses_anteriores(self):
        self._tres_meses_normales_y_un_agosto_caro()
        a = self.analizar()

        self.assertEqual(a['total'], Decimal('460'))
        self.assertEqual(a['media'], Decimal('250'))
        self.assertEqual(a['desviacion'], Decimal('210'))
        self.assertEqual(a['meses_referencia'], 3)
        self.assertTrue(a['hay_referencia'])

    def test_el_puente_señala_la_categoria_culpable(self):
        self._tres_meses_normales_y_un_agosto_caro()
        puente = self.analizar()['puente']

        self.assertEqual(puente[0]['categoria'], 'Restaurantes')
        self.assertEqual(puente[0]['desviacion'], Decimal('200'))
        # Alimentación también se desvía, pero mucho menos: va detrás.
        self.assertEqual(puente[1]['categoria'], 'Alimentacion')
        self.assertEqual(puente[1]['desviacion'], Decimal('10'))

    def test_el_puente_incluye_lo_que_baja(self):
        """Saber que la luz ha ido a favor es parte de entender el mes."""
        for mes in (6, 7):
            self.gasto('Recibo luz', '-100', 2026, mes, 3, categoria='Luz')
        self.gasto('Recibo luz', '-60', 2026, 8, 3, categoria='Luz')

        puente = self.analizar()['puente']
        self.assertEqual(puente[0]['categoria'], 'Luz')
        self.assertEqual(puente[0]['desviacion'], Decimal('-40'))

    def test_sin_meses_anteriores_no_se_inventa_una_comparacion(self):
        self.gasto('Glovo pedido', '-250', 2026, 8, 5)
        a = self.analizar()

        self.assertFalse(a['hay_referencia'])
        self.assertEqual(a['puente'], [])
        self.assertEqual(a['total'], Decimal('250'))

    def test_el_ranking_de_comercios_distingue_goteo_de_gasto_puntual(self):
        for dia in (3, 6, 9, 12, 15, 18, 21, 24):
            self.gasto('Glovo pedido', '-24', 2026, 8, dia)
        self.gasto('La Brunilda tapas', '-190', 2026, 8, 20)

        comercios = {c['comercio']: c for c in self.analizar()['comercios']}
        glovo = comercios['glovo pedido']
        brunilda = comercios['la brunilda tapas']

        self.assertEqual(glovo['num'], 8)
        self.assertEqual(glovo['total'], Decimal('192'))
        self.assertEqual(glovo['ticket_medio'], Decimal('24'))
        self.assertEqual(brunilda['num'], 1)
        self.assertEqual(brunilda['ticket_medio'], Decimal('190'))
        # El de mayor importe encabeza el ranking.
        self.assertEqual(self.analizar()['comercios'][0]['comercio'], 'glovo pedido')

    def test_separa_el_suelo_de_gasto_de_lo_excepcional(self):
        for mes in (5, 6, 7):
            self.gasto('Cuota gimnasio', '-30', 2026, mes, 2, categoria='Gimnasio')
        self.gasto('Cuota gimnasio', '-30', 2026, 8, 2, categoria='Gimnasio')
        self.gasto('Vuelo a Lisboa', '-180', 2026, 8, 12)

        a = self.analizar()
        self.assertEqual(a['gasto_recurrente'], Decimal('30'))
        self.assertEqual(a['gasto_puntual'], Decimal('180'))

    def test_sin_historia_nada_se_marca_como_habitual(self):
        """Marcar «puntual» algo que solo lleva un mes importado sería mentir
        con seguridad."""
        self.gasto('Cuota gimnasio', '-30', 2026, 8, 2, categoria='Gimnasio')
        a = self.analizar()
        self.assertEqual(a['gasto_recurrente'], Decimal('0'))
        self.assertFalse(a['comercios'][0]['recurrente'])

    def test_los_gastos_hormiga_se_suman_aparte(self):
        for dia in range(1, 11):
            self.gasto(f'Cafe {dia}', '-2.50', 2026, 8, dia)
        self.gasto('Cena', '-60', 2026, 8, 20)

        a = self.analizar()
        self.assertEqual(a['hormiga_num'], 10)
        self.assertEqual(a['hormiga_total'], Decimal('25.00'))

    def test_acotar_a_una_categoria_reduce_el_ambito(self):
        self._tres_meses_normales_y_un_agosto_caro()
        alimentacion = CategoriaGasto.objects.get(hogar=self.hogar, nombre='Alimentacion')

        a = self.analizar(categoria_id=alimentacion.id)
        self.assertEqual(a['total'], Decimal('210'))
        self.assertEqual(a['media'], Decimal('200'))
        self.assertEqual([c['nombre'] for c in a['categorias']], ['Alimentacion'])

    def test_acotar_a_un_bloque_reduce_el_ambito(self):
        self._tres_meses_normales_y_un_agosto_caro()
        a = self.analizar(bloque='discrecional')
        self.assertEqual(a['total'], Decimal('250'))
        self.assertEqual([c['nombre'] for c in a['categorias']], ['Restaurantes'])

    def test_ni_los_ingresos_ni_los_neutros_entran_en_el_analisis(self):
        self.gasto('Nomina agosto', '2500', 2026, 8, 1, categoria='Nomina')
        self.gasto('A mi cuenta', '-900', 2026, 8, 2, categoria='Traspaso entre cuentas')
        self.gasto('Glovo pedido', '-24', 2026, 8, 5)

        a = self.analizar()
        self.assertEqual(a['total'], Decimal('24'))
        self.assertEqual([c['nombre'] for c in a['categorias']], ['Restaurantes'])

    def test_una_devolucion_resta_de_su_categoria(self):
        self.gasto('Zapatillas', '-80', 2026, 8, 5, categoria='Ropa')
        self.gasto('Devolucion zapatillas', '30', 2026, 8, 9, categoria='Ropa')

        a = self.analizar()
        self.assertEqual(a['total'], Decimal('50'))


class AnalisisVistaTests(TestCase):
    """La pantalla: a dónde lleva el drill-down y qué enseña."""

    def setUp(self):
        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        self.user = User.objects.create_user(username='tester', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        self.extracto = ExtractoBancario.objects.create(hogar=self.hogar, usuario=self.user)
        self.client.force_login(self.user)
        self.ocio = CategoriaGasto.objects.get(hogar=self.hogar, nombre='Ocio')
        MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(2026, 7, 5),
            concepto='Cines', importe=Decimal('-20'), categoria=self.ocio,
        )
        MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(2026, 8, 5),
            concepto='Cines', importe=Decimal('-60'), categoria=self.ocio,
        )

    def test_se_abre_en_el_ultimo_mes_con_datos(self):
        respuesta = self.client.get(reverse('extractos:analisis'))
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.context['etiqueta_mes'], 'Agosto 2026')
        self.assertEqual(respuesta.context['a']['total'], Decimal('60'))

    def test_acota_por_categoria_desde_la_url(self):
        respuesta = self.client.get(reverse('extractos:analisis'), {
            'anio': 2026, 'mes': 8, 'categoria': self.ocio.id,
        })
        self.assertEqual(respuesta.context['categoria'], self.ocio)
        self.assertTrue(respuesta.context['hay_ambito'])

    def test_una_categoria_de_otro_hogar_no_acota_nada(self):
        otro = Hogar.objects.create(nombre='Otro')
        ajena = CategoriaGasto.objects.create(hogar=otro, nombre='Ajena', tipo='variable')
        respuesta = self.client.get(reverse('extractos:analisis'), {'categoria': ajena.id})
        self.assertIsNone(respuesta.context['categoria'])

    def test_un_bloque_inventado_se_ignora(self):
        respuesta = self.client.get(reverse('extractos:analisis'), {'bloque': 'inventado'})
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.context['bloque'], '')

    def test_la_conciliacion_enlaza_con_el_analisis_de_cada_categoria(self):
        PartidaGasto.objects.create(
            hogar=self.hogar, categoria=self.ocio, nombre='Ocio',
            importe=Decimal('40'), periodicidad='mensual',
        )
        respuesta = self.client.get(reverse('extractos:conciliacion'))
        filas = [f for b in respuesta.context['bloques'] for f in b['filas']]
        self.assertEqual(filas[0]['categoria_id'], self.ocio.id)
        self.assertContains(respuesta, f'categoria={self.ocio.id}')


class EtiquetasTests(TestCase):
    """Etiquetas: el corte transversal que las categorías no pueden dar."""

    def setUp(self):
        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        self.user = User.objects.create_user(username='tester', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        self.extracto = ExtractoBancario.objects.create(hogar=self.hogar, usuario=self.user)
        self.client.force_login(self.user)
        self.mov = MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(2026, 8, 5),
            concepto='Hotel Lisboa', importe=Decimal('-240'),
        )

    def etiquetar(self, mov, **datos):
        return self.client.post(
            reverse('extractos:etiquetar_movimiento', args=[mov.id]), datos,
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

    def test_etiquetar_crea_la_etiqueta_si_es_nueva(self):
        respuesta = self.etiquetar(self.mov, nombre='Vacaciones Lisboa')
        datos = respuesta.json()

        self.assertTrue(datos['ok'])
        self.assertEqual(datos['etiquetas'][0]['nombre'], 'Vacaciones Lisboa')
        self.assertEqual(Etiqueta.objects.filter(hogar=self.hogar).count(), 1)

    def test_la_misma_etiqueta_no_se_duplica_por_mayusculas(self):
        self.etiquetar(self.mov, nombre='Vacaciones')
        otro = MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(2026, 8, 6),
            concepto='Cena Lisboa', importe=Decimal('-40'),
        )
        self.etiquetar(otro, nombre='VACACIONES')

        self.assertEqual(Etiqueta.objects.filter(hogar=self.hogar).count(), 1)
        self.assertEqual(Etiqueta.objects.get().movimientos.count(), 2)

    def test_quitar_una_etiqueta_la_desvincula_sin_borrarla(self):
        self.etiquetar(self.mov, nombre='Vacaciones')
        etiqueta = Etiqueta.objects.get()
        respuesta = self.etiquetar(self.mov, quitar=etiqueta.id)

        self.assertEqual(respuesta.json()['etiquetas'], [])
        self.assertTrue(Etiqueta.objects.filter(pk=etiqueta.pk).exists())

    def test_ofrece_etiquetar_el_resto_del_comercio(self):
        for dia in (6, 7):
            MovimientoBancario.objects.create(
                extracto=self.extracto, hogar=self.hogar, fecha=date(2026, 8, dia),
                concepto='Hotel Lisboa', importe=Decimal('-240'),
            )
        sugerencia = self.etiquetar(self.mov, nombre='Vacaciones').json()['sugerencia']
        self.assertEqual(sugerencia['n_similares'], 2)

        etiqueta = Etiqueta.objects.get()
        respuesta = self.client.post(reverse('extractos:etiquetar_comercio'), {
            'etiqueta_id': etiqueta.id, 'comercio': self.mov.comercio,
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

        self.assertEqual(respuesta.json()['aplicados'], 2)
        self.assertEqual(etiqueta.movimientos.count(), 3)

    def test_borrar_una_etiqueta_no_toca_los_movimientos(self):
        self.etiquetar(self.mov, nombre='Vacaciones')
        etiqueta = Etiqueta.objects.get()
        self.client.post(reverse('extractos:etiquetas'), {
            'accion': 'eliminar', 'etiqueta_id': etiqueta.id,
        })

        self.assertFalse(Etiqueta.objects.exists())
        self.mov.refresh_from_db()
        self.assertEqual(self.mov.concepto, 'Hotel Lisboa')

    def test_el_analisis_reparte_el_gasto_por_etiqueta(self):
        self.etiquetar(self.mov, nombre='Vacaciones Lisboa')
        todos = list(
            MovimientoBancario.objects.filter(hogar=self.hogar)
            .select_related('categoria').prefetch_related('etiquetas')
        )
        etiquetas = analizar_mes(todos, 2026, 8)['etiquetas']

        self.assertEqual(etiquetas[0]['nombre'], 'Vacaciones Lisboa')
        self.assertEqual(etiquetas[0]['total'], Decimal('240'))

    def test_el_listado_se_puede_filtrar_por_etiqueta(self):
        self.etiquetar(self.mov, nombre='Vacaciones')
        MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(2026, 8, 9),
            concepto='Otra cosa', importe=Decimal('-10'),
        )
        etiqueta = Etiqueta.objects.get()

        panel = self.client.get(
            reverse('extractos:listar'), {'etiqueta': etiqueta.id},
        ).context['panel']
        self.assertEqual(panel['kpi_num'], 1)

    def test_el_listado_se_puede_filtrar_por_bloque(self):
        """Es el destino del drill-down desde la conciliación."""
        ocio = CategoriaGasto.objects.get(hogar=self.hogar, nombre='Ocio')
        MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(2026, 8, 9),
            concepto='Cines', importe=Decimal('-10'), categoria=ocio,
        )
        panel = self.client.get(
            reverse('extractos:listar'), {'bloque': 'discrecional'},
        ).context['panel']

        self.assertEqual(panel['kpi_num'], 1)
        self.assertEqual(panel['bloque_etiqueta'], 'Discrecionales')


class ImputacionAActivosTests(TestCase):
    """Marcar movimientos como gasto de un vehículo o una propiedad: es lo que
    da la pata REAL de «cuánto me cuesta el coche»."""

    def setUp(self):
        from finanzas.models import Vehiculo

        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        self.user = User.objects.create_user(username='tester', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        self.extracto = ExtractoBancario.objects.create(hogar=self.hogar, usuario=self.user)
        self.client.force_login(self.user)
        self.coche = Vehiculo.objects.create(hogar=self.hogar, nombre='Golf')
        self.mov = MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(2026, 3, 5),
            concepto='REPSOL E.S. 4021', importe=Decimal('-60'),
        )

    def imputar(self, mov, clave):
        return self.client.post(
            reverse('extractos:imputar_movimiento', args=[mov.id]), {'activo': clave},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

    def test_imputar_un_movimiento_a_un_vehiculo(self):
        respuesta = self.imputar(self.mov, self.coche.clave_activo)

        self.assertTrue(respuesta.json()['ok'])
        self.mov.refresh_from_db()
        self.assertEqual(self.mov.vehiculo, self.coche)
        self.assertEqual(self.mov.clave_activo, self.coche.clave_activo)

    def test_un_activo_de_otro_hogar_se_rechaza(self):
        from finanzas.models import Vehiculo

        ajeno = Vehiculo.objects.create(
            hogar=Hogar.objects.create(nombre='Otro'), nombre='Ajeno',
        )
        respuesta = self.imputar(self.mov, ajeno.clave_activo)

        self.assertEqual(respuesta.status_code, 400)
        self.mov.refresh_from_db()
        self.assertIsNone(self.mov.vehiculo_id)

    def test_desimputar_lo_deja_sin_activo(self):
        self.imputar(self.mov, self.coche.clave_activo)
        self.imputar(self.mov, '')

        self.mov.refresh_from_db()
        self.assertIsNone(self.mov.vehiculo_id)
        self.assertEqual(self.mov.clave_activo, '')

    def test_ofrece_imputar_el_resto_del_comercio(self):
        for dia in (7, 9):
            MovimientoBancario.objects.create(
                extracto=self.extracto, hogar=self.hogar, fecha=date(2026, 3, dia),
                concepto='REPSOL E.S. 4021', importe=Decimal('-55'),
            )
        sugerencia = self.imputar(self.mov, self.coche.clave_activo).json()['sugerencia']
        self.assertEqual(sugerencia['n_similares'], 2)

        respuesta = self.client.post(reverse('extractos:imputar_comercio'), {
            'activo': self.coche.clave_activo, 'comercio': self.mov.comercio,
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

        self.assertEqual(respuesta.json()['aplicados'], 3)
        self.assertEqual(
            MovimientoBancario.objects.filter(vehiculo=self.coche).count(), 3,
        )

    def test_el_listado_se_puede_filtrar_por_activo(self):
        self.imputar(self.mov, self.coche.clave_activo)
        MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(2026, 3, 11),
            concepto='Compra semanal', importe=Decimal('-40'),
        )
        panel = self.client.get(
            reverse('extractos:listar'), {'activo': self.coche.clave_activo},
        ).context['panel']

        self.assertEqual(panel['kpi_num'], 1)

    def test_lo_imputado_llega_a_la_ficha_del_vehiculo(self):
        """El recorrido completo: marco el gasto en extractos y aparece en el
        coste real del coche."""
        from finanzas import costes_activo

        self.imputar(self.mov, self.coche.clave_activo)
        ficha = costes_activo.costes(self.coche, 2026)

        self.assertEqual(ficha['real_anual'], Decimal('60'))
        self.assertEqual(ficha['num_movimientos'], 1)


class PagosDeGastosAnualesTests(TestCase):
    """El IBI se provisiona a 43 €/mes y se paga de golpe en junio.

    Sin marcar ese pago, junio parece un mes desastroso y los otros once un
    dechado de virtud: el pago tiene que salir de la comparación MENSUAL y
    llevarse a la del año, que es su unidad."""

    def setUp(self):
        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        self.user = User.objects.create_user(username='tester', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        self.extracto = ExtractoBancario.objects.create(hogar=self.hogar, usuario=self.user)
        self.client.force_login(self.user)

        self.ibi = PartidaGasto.objects.create(
            hogar=self.hogar, categoria=CategoriaGasto.objects.get(hogar=self.hogar, nombre='IBI'),
            nombre='IBI del piso', importe=Decimal('520'), periodicidad='anual',
        )
        self.compra = PartidaGasto.objects.create(
            hogar=self.hogar,
            categoria=CategoriaGasto.objects.get(hogar=self.hogar, nombre='Alimentacion'),
            nombre='Super', importe=Decimal('400'), periodicidad='mensual',
        )

    def gasto(self, concepto, importe, mes, categoria='IBI', dia=12):
        return MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(2026, mes, dia),
            concepto=concepto, importe=Decimal(importe),
            categoria=CategoriaGasto.objects.get(hogar=self.hogar, nombre=categoria),
        )

    def marcar(self, mov, partida_id):
        return self.client.post(
            reverse('extractos:marcar_provision', args=[mov.id]),
            {'partida_id': partida_id}, HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

    def test_marcar_un_pago_como_provision(self):
        mov = self.gasto('IBI AYUNTAMIENTO', '-260', 6)
        respuesta = self.marcar(mov, self.ibi.id)

        self.assertTrue(respuesta.json()['ok'])
        mov.refresh_from_db()
        self.assertEqual(mov.partida_conciliada, self.ibi)
        self.assertTrue(mov.es_pago_provision)

    def test_una_partida_mensual_no_vale_como_provision(self):
        """Un gasto mensual ya se compara mes a mes: marcarlo lo sacaría de la
        comparación sin motivo."""
        mov = self.gasto('Compra', '-380', 6, categoria='Alimentacion')
        respuesta = self.marcar(mov, self.compra.id)

        self.assertEqual(respuesta.status_code, 400)
        mov.refresh_from_db()
        self.assertFalse(mov.es_pago_provision)

    def test_el_pago_anual_sale_de_la_comparacion_del_mes(self):
        self.gasto('Compra semanal', '-380', 6, categoria='Alimentacion')
        ibi = self.gasto('IBI AYUNTAMIENTO', '-260', 6)

        sin_marcar = self.client.get(
            reverse('extractos:conciliacion'), {'anio': 2026, 'mes': 6},
        )
        self.assertEqual(sin_marcar.context['total_observado'], Decimal('640'))

        self.marcar(ibi, self.ibi.id)
        marcado = self.client.get(
            reverse('extractos:conciliacion'), {'anio': 2026, 'mes': 6},
        )
        self.assertEqual(marcado.context['total_observado'], Decimal('380'))
        self.assertEqual(marcado.context['total_provisiones_periodo'], Decimal('260'))
        self.assertEqual(len(marcado.context['pagos_provision']), 1)

    def test_el_gasto_no_mensual_no_descuadra_ninguno_de_los_dos_lados(self):
        """Si el pago sale del observado, su provisión sale del declarado: si
        no, el bloque de anuales saldría a «0 € de 43 €» todos los meses."""
        self.marcar(self.gasto('IBI AYUNTAMIENTO', '-260', 6), self.ibi.id)
        self.gasto('Compra semanal', '-380', 6, categoria='Alimentacion')

        respuesta = self.client.get(reverse('extractos:conciliacion'), {'anio': 2026, 'mes': 6})
        etiquetas = [b['etiqueta'] for b in respuesta.context['bloques']]

        self.assertNotIn('Fijos anuales', etiquetas)
        self.assertEqual(respuesta.context['total_declarado'], Decimal('400'))
        self.assertEqual(respuesta.context['total_observado'], Decimal('380'))

    def test_sobre_varios_meses_la_comparacion_vuelve_a_incluirlos(self):
        """En doce meses el prorrateo y los pagos se promedian bien: ahí el
        gasto anual sí tiene que estar en los dos lados."""
        self.marcar(self.gasto('IBI AYUNTAMIENTO', '-520', 6), self.ibi.id)

        respuesta = self.client.get(
            reverse('extractos:conciliacion'), {'anio': 'all', 'mes': 'all'},
        )
        etiquetas = [b['etiqueta'] for b in respuesta.context['bloques']]
        self.assertIn('Fijos anuales', etiquetas)
        self.assertEqual(respuesta.context['pagos_provision'], [])

    def test_el_bloque_anual_suma_los_pagos_del_anio(self):
        """Dos pagos parciales: junio y noviembre."""
        for mes, importe in ((6, '-260'), (11, '-260')):
            self.marcar(self.gasto('IBI AYUNTAMIENTO', importe, mes), self.ibi.id)

        respuesta = self.client.get(reverse('extractos:conciliacion'), {'anio': 2026, 'mes': 6})
        fila = next(f for f in respuesta.context['provisiones'] if f['partida'] == self.ibi)

        self.assertEqual(fila['objetivo'], Decimal('520'))
        self.assertEqual(fila['pagado'], Decimal('520'))
        self.assertEqual(fila['num_pagos'], 2)
        self.assertEqual(fila['pct'], 100)
        self.assertTrue(fila['completo'])

    def test_un_pago_a_medias_se_ve_a_medias(self):
        self.marcar(self.gasto('IBI AYUNTAMIENTO', '-260', 6), self.ibi.id)

        respuesta = self.client.get(reverse('extractos:conciliacion'), {'anio': 2026, 'mes': 6})
        fila = next(f for f in respuesta.context['provisiones'] if f['partida'] == self.ibi)

        self.assertEqual(fila['pagado'], Decimal('260'))
        self.assertEqual(fila['pendiente'], Decimal('260'))
        self.assertEqual(fila['pct'], 50)
        self.assertFalse(fila['completo'])

    def test_los_pagos_de_otro_anio_no_se_cuelan(self):
        viejo = MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(2025, 6, 12),
            concepto='IBI AYUNTAMIENTO', importe=Decimal('-500'),
            categoria=CategoriaGasto.objects.get(hogar=self.hogar, nombre='IBI'),
        )
        self.marcar(viejo, self.ibi.id)
        self.marcar(self.gasto('IBI AYUNTAMIENTO', '-260', 6), self.ibi.id)

        respuesta = self.client.get(reverse('extractos:conciliacion'), {'anio': 2026, 'mes': 6})
        fila = next(f for f in respuesta.context['provisiones'] if f['partida'] == self.ibi)
        self.assertEqual(fila['pagado'], Decimal('260'))

    def test_una_partida_sin_pagos_sale_igualmente_para_recordarla(self):
        respuesta = self.client.get(reverse('extractos:conciliacion'), {'anio': 2026, 'mes': 6})
        fila = next(f for f in respuesta.context['provisiones'] if f['partida'] == self.ibi)

        self.assertEqual(fila['pagado'], Decimal('0'))
        self.assertEqual(fila['num_pagos'], 0)

    def test_desmarcar_lo_devuelve_a_la_comparacion_mensual(self):
        ibi = self.gasto('IBI AYUNTAMIENTO', '-260', 6)
        self.marcar(ibi, self.ibi.id)
        self.marcar(ibi, '')

        ibi.refresh_from_db()
        self.assertFalse(ibi.es_pago_provision)
        respuesta = self.client.get(reverse('extractos:conciliacion'), {'anio': 2026, 'mes': 6})
        self.assertEqual(respuesta.context['total_observado'], Decimal('260'))

    def test_el_pago_hereda_la_categoria_del_gasto_declarado(self):
        """Si no, el mismo apunte contaría en un sitio y en otro no."""
        mov = MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(2026, 6, 12),
            concepto='RECIBO AYUNTAMIENTO', importe=Decimal('-260'),
        )
        self.marcar(mov, self.ibi.id)

        mov.refresh_from_db()
        self.assertEqual(mov.categoria, self.ibi.categoria)


class PilaresDelPresupuestoTests(TestCase):
    """El gasto observado se lee por los cuatro pilares con los que se declara
    el presupuesto: es lo único que permite conciliar uno con otro. Las
    categorías viven dentro de su pilar, no al lado."""

    def setUp(self):
        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        self.user = User.objects.create_user(username='tester', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        self.extracto = ExtractoBancario.objects.create(hogar=self.hogar, usuario=self.user)
        self.client.force_login(self.user)
        self._dia = 0

    def gasto(self, importe, categoria=None):
        self._dia += 1
        return MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(2026, 8, self._dia),
            concepto=f'Gasto {self._dia}', importe=Decimal(importe),
            categoria=(
                CategoriaGasto.objects.get(hogar=self.hogar, nombre=categoria)
                if categoria else None
            ),
        )

    def panel(self, **params):
        return self.client.get(reverse('extractos:listar'), params).context['panel']

    def test_los_bloques_llevan_sus_categorias_dentro(self):
        self.gasto('-300', 'Alimentacion')     # variable
        self.gasto('-120', 'Luz')              # variable
        self.gasto('-200', 'Restaurantes')     # discrecional
        self.gasto('-900', 'Hipoteca / Alquiler')  # fijo

        bloques = {b['etiqueta']: b for b in self.panel()['bloques']}

        self.assertEqual(
            [b['etiqueta'] for b in self.panel()['bloques']],
            ['Fijos', 'Variables', 'Discrecionales'],
        )
        self.assertEqual(bloques['Variables']['importe'], Decimal('420'))
        self.assertEqual(
            [c['nombre'] for c in bloques['Variables']['categorias']],
            ['Alimentacion', 'Luz'],
        )
        self.assertEqual(bloques['Variables']['categorias'][0]['pct_bloque'], 71.4)

    def test_lo_sin_categorizar_es_un_bloque_mas(self):
        """Tiene que verse: si el 79% del gasto no está clasificado, el reparto
        por pilares no significa nada y hay que decirlo."""
        self.gasto('-300', 'Alimentacion')
        self.gasto('-700')

        bloques = {b['etiqueta']: b for b in self.panel()['bloques']}
        self.assertEqual(bloques['Sin categorizar']['importe'], Decimal('700'))
        self.assertEqual(bloques['Sin categorizar']['pct'], 70.0)

    def test_el_donut_va_por_bloque_y_cuadra_con_la_lista(self):
        self.gasto('-300', 'Alimentacion')
        self.gasto('-200', 'Restaurantes')

        panel = self.panel()
        self.assertEqual(
            [d['nombre'] for d in panel['donut']],
            [b['etiqueta'] for b in panel['bloques']],
        )
        self.assertEqual(
            sum(d['importe'] for d in panel['donut']), float(panel['donut_total']),
        )

    def test_mover_una_categoria_de_bloque_la_recoloca(self):
        """«Salud / Farmacia va en variables»: se dice desde la propia pantalla
        donde se ve mal colocada."""
        salud = CategoriaGasto.objects.get(hogar=self.hogar, nombre='Salud / Farmacia')
        salud.tipo = 'discrecional'
        salud.save(update_fields=['tipo'])
        self.gasto('-60', 'Salud / Farmacia')

        self.assertEqual(
            [b['etiqueta'] for b in self.panel()['bloques']], ['Discrecionales'],
        )

        respuesta = self.client.post(reverse('finanzas:cambiar_bloque_categoria'), {
            'categoria_id': salud.id, 'tipo': 'variable',
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

        self.assertTrue(respuesta.json()['ok'])
        self.assertEqual(
            [b['etiqueta'] for b in self.panel()['bloques']], ['Variables'],
        )

    def test_un_bloque_inventado_no_recoloca_nada(self):
        salud = CategoriaGasto.objects.get(hogar=self.hogar, nombre='Salud / Farmacia')
        respuesta = self.client.post(reverse('finanzas:cambiar_bloque_categoria'), {
            'categoria_id': salud.id, 'tipo': 'inventado',
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

        self.assertEqual(respuesta.status_code, 400)
        salud.refresh_from_db()
        self.assertEqual(salud.tipo, 'variable')

    def test_una_categoria_de_otro_hogar_no_se_toca(self):
        otro = Hogar.objects.create(nombre='Otro')
        ajena = CategoriaGasto.objects.create(hogar=otro, nombre='Ajena', tipo='fijo')
        respuesta = self.client.post(reverse('finanzas:cambiar_bloque_categoria'), {
            'categoria_id': ajena.id, 'tipo': 'variable',
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

        self.assertEqual(respuesta.status_code, 400)
        ajena.refresh_from_db()
        self.assertEqual(ajena.tipo, 'fijo')

    def test_el_analisis_tambien_se_lee_por_pilares(self):
        self.gasto('-300', 'Alimentacion')
        self.gasto('-200', 'Restaurantes')

        respuesta = self.client.get(reverse('extractos:analisis'), {'anio': 2026, 'mes': 8})
        bloques = respuesta.context['a']['bloques']

        self.assertEqual([b['etiqueta'] for b in bloques], ['Variables', 'Discrecionales'])
        self.assertEqual(bloques[0]['importe'], Decimal('300'))
        self.assertEqual([c['nombre'] for c in bloques[0]['categorias']], ['Alimentacion'])


class FueraDePresupuestoTests(TestCase):
    """El análisis del mes contra el presupuesto declarado: qué se ha pasado
    del límite, cuál era ese límite y cuánto fue el gasto real."""

    def setUp(self):
        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        self.user = User.objects.create_user(username='tester', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        self.extracto = ExtractoBancario.objects.create(hogar=self.hogar, usuario=self.user)
        self.client.force_login(self.user)
        self._dia = 0

    def declarar(self, categoria, importe, periodicidad='mensual'):
        return PartidaGasto.objects.create(
            hogar=self.hogar, nombre=f'Presupuesto {categoria}',
            categoria=CategoriaGasto.objects.get(hogar=self.hogar, nombre=categoria),
            importe=Decimal(importe), periodicidad=periodicidad,
        )

    def gasto(self, categoria, importe, mes=8, concepto=None):
        # El concepto por defecto lleva la categoría porque el comercio se
        # deduce de él: con «Gasto 1», «Gasto 2»… todos serían el mismo
        # comercio y el reparto habitual/puntual no distinguiría nada.
        self._dia += 1
        return MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(2026, mes, self._dia),
            concepto=concepto or f'Comercio de {categoria}', importe=Decimal(importe),
            categoria=CategoriaGasto.objects.get(hogar=self.hogar, nombre=categoria),
        )

    def analisis(self, **params):
        params.setdefault('anio', 2026)
        params.setdefault('mes', 8)
        return self.client.get(reverse('extractos:analisis'), params).context['a']

    def test_solo_aparece_lo_que_se_ha_pasado(self):
        self.declarar('Alimentacion', '400')
        self.declarar('Ocio', '100')
        self.gasto('Alimentacion', '-520')   # se pasa 120
        self.gasto('Ocio', '-60')            # dentro

        fuera = self.analisis()['fuera_presupuesto']

        self.assertEqual([f['nombre'] for f in fuera], ['Alimentacion'])
        self.assertEqual(fuera[0]['importe'], Decimal('520'))
        self.assertEqual(fuera[0]['limite'], Decimal('400'))
        self.assertEqual(fuera[0]['exceso'], Decimal('120'))

    def test_se_ordena_por_lo_que_se_ha_pasado(self):
        self.declarar('Alimentacion', '400')
        self.declarar('Ocio', '50')
        self.gasto('Alimentacion', '-450')   # +50
        self.gasto('Ocio', '-250')           # +200

        a = self.analisis()
        self.assertEqual([f['nombre'] for f in a['fuera_presupuesto']], ['Ocio', 'Alimentacion'])
        self.assertEqual(a['exceso_total'], Decimal('250'))

    def test_el_gasto_anual_se_compara_prorrateado(self):
        """Un IBI de 520 € al año son 43,33 €/mes de límite."""
        self.declarar('IBI', '520', 'anual')
        self.gasto('IBI', '-100')

        fuera = self.analisis()['fuera_presupuesto']
        self.assertEqual(fuera[0]['nombre'], 'IBI')
        self.assertEqual(fuera[0]['limite'], Decimal('43.33'))

    def test_lo_que_no_tiene_presupuesto_va_aparte(self):
        """Sin límite declarado no está «fuera»: no hay con qué compararlo, y
        decir que se ha pasado sería inventárselo."""
        self.gasto('Restaurantes', '-300')

        a = self.analisis()
        self.assertEqual(a['fuera_presupuesto'], [])
        self.assertEqual([f['nombre'] for f in a['sin_presupuesto']], ['Restaurantes'])

    def test_los_bloques_saben_si_caben_en_el_presupuesto(self):
        self.declarar('Alimentacion', '400')      # bloque variable
        self.declarar('Ocio', '300')              # bloque discrecional
        self.gasto('Alimentacion', '-500')        # variable: se pasa
        self.gasto('Ocio', '-100')                # discrecional: cabe

        bloques = {b['etiqueta']: b for b in self.analisis()['bloques']}
        self.assertFalse(bloques['Variables']['dentro'])
        self.assertEqual(bloques['Variables']['limite'], Decimal('400'))
        self.assertTrue(bloques['Discrecionales']['dentro'])

    def test_habitual_puntual_y_hormiga_traen_sus_movimientos(self):
        """Saber «cuánto» sin poder ver «cuáles» obliga a salir a buscarlo."""
        for mes in (5, 6, 7):
            self.gasto('Suscripciones', '-14', mes=mes, concepto='Netflix')
        self.gasto('Suscripciones', '-14', concepto='Netflix')
        self.gasto('Ropa', '-120', concepto='Zara')
        self.gasto('Restaurantes', '-3.50', concepto='Cafeteria Lola')

        a = self.analisis()
        self.assertEqual([m.concepto for m in a['movs_recurrentes']], ['Netflix'])
        self.assertEqual(
            sorted(m.concepto for m in a['movs_puntuales']), ['Cafeteria Lola', 'Zara'],
        )
        # «Pequeños» es un corte transversal, no un tercer grupo: Netflix es
        # habitual Y pequeño a la vez.
        self.assertEqual(
            sorted(m.concepto for m in a['movs_hormiga']), ['Cafeteria Lola', 'Netflix'],
        )

    def test_el_panel_pinta_los_bloques_contra_el_presupuesto(self):
        self.declarar('Alimentacion', '400')
        self.gasto('Alimentacion', '-500')

        panel = self.client.get(
            reverse('extractos:listar'), {'anio': 2026, 'mes': 8},
        ).context['panel']
        bloque = panel['bloques'][0]

        self.assertFalse(bloque['dentro'])
        self.assertEqual(bloque['limite'], Decimal('400'))
        self.assertEqual(bloque['categorias'][0]['limite'], Decimal('400'))
        self.assertEqual(bloque['pct'], 100.0)   # y el peso sobre el total sigue ahí

    def test_sobre_varios_meses_el_limite_se_multiplica(self):
        """El presupuesto es mensual; si miras dos meses, el límite son dos."""
        self.declarar('Alimentacion', '400')
        self.gasto('Alimentacion', '-380', mes=7)
        self.gasto('Alimentacion', '-380', mes=8)

        panel = self.client.get(
            reverse('extractos:listar'), {'anio': 2026},
        ).context['panel']

        self.assertEqual(panel['meses_periodo'], 2)
        self.assertEqual(panel['bloques'][0]['limite'], Decimal('800'))
        self.assertTrue(panel['bloques'][0]['dentro'])


class NavegacionDelModuloTests(TestCase):
    """El periodo elegido tiene que sobrevivir al cambio de pestaña."""

    def setUp(self):
        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        self.user = User.objects.create_user(username='tester', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        self.client.force_login(self.user)

    def test_las_pestañas_conservan_el_mes(self):
        respuesta = self.client.get(reverse('extractos:listar'), {'anio': 2026, 'mes': 8})
        self.assertContains(respuesta, 'analisis/?anio=2026&amp;mes=8')
        self.assertContains(respuesta, 'conciliacion/?anio=2026&amp;mes=8')

    def test_sin_periodo_las_pestañas_van_limpias(self):
        respuesta = self.client.get(reverse('extractos:listar'))
        self.assertNotContains(respuesta, 'analisis/?anio=')

    def test_todos_no_es_un_periodo(self):
        respuesta = self.client.get(reverse('extractos:listar'), {'anio': 'all', 'mes': 'all'})
        self.assertNotContains(respuesta, 'analisis/?anio=')

    def test_el_analisis_ofrece_volver_a_donde_estabas(self):
        respuesta = self.client.get(reverse('extractos:analisis'), {
            'anio': 2026, 'mes': 8, 'volver': 'conciliacion',
        })
        self.assertEqual(respuesta.context['volver_nombre'], 'Conciliación')
        self.assertEqual(
            respuesta.context['volver_url'], '/extractos/conciliacion/?anio=2026&mes=8',
        )

    def test_un_destino_inventado_no_pinta_boton(self):
        """El «volver» es una lista blanca: aceptar cualquier URL sería un
        redirector abierto."""
        respuesta = self.client.get(reverse('extractos:analisis'), {
            'volver': 'https://example.com/phishing',
        })
        self.assertEqual(respuesta.context['volver_url'], '')
