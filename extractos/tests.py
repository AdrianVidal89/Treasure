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

    def test_los_ingresos_sin_clasificar_tambien_salen_al_filtrar(self):
        """Una nómina sin clasificar es tan invisible en el análisis como un
        gasto sin clasificar: el filtro tiene que traer las dos cosas."""
        extracto = ExtractoBancario.objects.create(hogar=self.hogar, usuario=self.user)
        ingreso = MovimientoBancario.objects.create(
            extracto=extracto, hogar=self.hogar, fecha=date(2026, 7, 3),
            concepto='Ingreso raro de alguien', importe=Decimal('250.00'),
        )
        self.client.force_login(self.user)
        panel = self.client.get(
            reverse('extractos:listar'), {'categoria': 'sin'},
        ).context['panel']
        vistos = [m.id for g in panel['grupos'] for m in g['movimientos']]
        self.assertIn(ingreso.id, vistos)


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

    def test_la_ruta_vieja_de_sin_categorizar_lleva_al_listado_filtrado(self):
        """La pantalla se fundió con Movimientos: filtrar por «Sin categorizar»
        hace lo mismo y además tiene el buscador, el periodo y el cambio en
        bloque."""
        respuesta = self.client.get(reverse('extractos:sin_categorizar'))
        self.assertRedirects(
            respuesta, reverse('extractos:listar') + '?categoria=sin',
        )

    def test_el_filtro_de_sin_categorizar_trae_solo_lo_que_falta(self):
        for _ in range(3):
            self.crear_movimiento('Malacabeza')
        clasificado = self.crear_movimiento('Otro sitio cualquiera')
        clasificado.categoria = self.ocio
        clasificado.save()

        panel = self.client.get(
            reverse('extractos:listar'), {'categoria': 'sin'},
        ).context['panel']
        vistos = [m for g in panel['grupos'] for m in g['movimientos']]
        self.assertEqual(len(vistos), 3)
        self.assertNotIn(clasificado.id, [m.id for m in vistos])

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

        # El patrón es el comercio normalizado, que es lo que el aviso del
        # listado ofrece al cambiar la categoría de uno de ellos.
        respuesta = self.client.post(reverse('extractos:aprender_regla'), {
            'categoria_id': self.ocio.id, 'patron': 'malacabeza',
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
        # Y ya no quedan de ese comercio por clasificar.
        panel = self.client.get(
            reverse('extractos:listar'), {'categoria': 'sin'},
        ).context['panel']
        pendientes = [m.comercio for g in panel['grupos'] for m in g['movimientos']]
        self.assertNotIn('malacabeza', pendientes)

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
    """El análisis ya no es una pantalla aparte: vive dentro de Movimientos.
    Aquí se comprueba que lo que enseñaba sigue llegando y que el drill-down de
    la conciliación acaba en el sitio correcto."""

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

    def panel(self, **params):
        return self.client.get(reverse('extractos:listar'), params).context['panel']

    def test_la_ruta_vieja_del_analisis_lleva_a_movimientos(self):
        respuesta = self.client.get(reverse('extractos:analisis'), {'anio': 2026, 'mes': 8})
        self.assertRedirects(respuesta, reverse('extractos:listar') + '?anio=2026&mes=8')

    def test_con_un_mes_elegido_se_compara_con_la_media_anterior(self):
        panel = self.panel(anio=2026, mes=8)
        self.assertEqual(panel['comparativa']['total'], Decimal('60'))
        self.assertEqual(panel['comparativa']['media'], Decimal('20'))
        self.assertEqual(panel['comparativa']['desviacion'], Decimal('40'))

    def test_sin_mes_elegido_no_hay_comparacion(self):
        """La comparación es contra la media de los meses ANTERIORES: sin un mes
        concreto no hay nada que comparar, y fingir una cifra sería peor que no
        dar ninguna."""
        self.assertIsNone(self.panel(anio=2026)['comparativa'])
        self.assertIsNone(self.panel()['comparativa'])

    def test_con_el_buscador_puesto_no_se_compara(self):
        """El motor de la comparación no conoce el buscador: si se ofreciera,
        diría algo distinto de la lista que tiene justo debajo."""
        self.assertIsNone(self.panel(anio=2026, mes=8, q='cines')['comparativa'])

    def test_acota_por_categoria_desde_la_url(self):
        panel = self.panel(anio=2026, mes=8, categoria=self.ocio.id)
        self.assertEqual(panel['categoria_activa'], self.ocio)
        self.assertEqual(panel['comparativa']['total'], Decimal('60'))

    def test_una_categoria_de_otro_hogar_no_acota_nada(self):
        otro = Hogar.objects.create(nombre='Otro')
        ajena = CategoriaGasto.objects.create(hogar=otro, nombre='Ajena', tipo='variable')
        panel = self.panel(anio=2026, mes=8, categoria=ajena.id)
        self.assertIsNone(panel['categoria_activa'])

    def test_el_boton_de_volver_se_pinta_en_la_pantalla(self):
        respuesta = self.client.get(reverse('extractos:listar'), {
            'anio': 2026, 'mes': 8, 'volver': 'conciliacion',
        })
        self.assertContains(respuesta, 'Volver a Conciliación')

    def test_el_desglose_por_comercio_respeta_los_filtros(self):
        comercios = self.panel(anio=2026, mes=8)['comercios']['filas']
        self.assertEqual([c['total'] for c in comercios], [Decimal('60')])
        self.assertEqual(comercios[0]['etiqueta'], 'Cines')

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

        respuesta = self.client.get(reverse('extractos:listar'), {'anio': 2026, 'mes': 8})
        bloques = respuesta.context['panel']['comparativa']['bloques']

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

    def panel(self, **params):
        params.setdefault('anio', 2026)
        params.setdefault('mes', 8)
        return self.client.get(reverse('extractos:listar'), params).context['panel']

    def fuera(self, **params):
        return self.panel(**params)['fuera_presupuesto']

    def test_solo_aparece_lo_que_se_ha_pasado(self):
        self.declarar('Alimentacion', '400')
        self.declarar('Ocio', '100')
        self.gasto('Alimentacion', '-520')   # se pasa 120
        self.gasto('Ocio', '-60')            # dentro

        fuera = self.fuera()['categorias']

        self.assertEqual([f['nombre'] for f in fuera], ['Alimentacion'])
        self.assertEqual(fuera[0]['importe'], Decimal('520'))
        self.assertEqual(fuera[0]['limite'], Decimal('400'))
        self.assertEqual(fuera[0]['exceso'], Decimal('120'))

    def test_se_ordena_por_lo_que_se_ha_pasado(self):
        self.declarar('Alimentacion', '400')
        self.declarar('Luz', '50')
        self.gasto('Alimentacion', '-450')   # +50
        self.gasto('Luz', '-250')            # +200

        fuera = self.fuera()
        self.assertEqual([f['nombre'] for f in fuera['categorias']], ['Luz', 'Alimentacion'])

    def test_el_exceso_total_es_el_de_los_bloques_y_no_cuenta_dos_veces(self):
        """Sumar el exceso del bloque MÁS el de sus categorías contaría el mismo
        euro dos veces y daría una cifra que no existe."""
        self.declarar('Alimentacion', '400')
        self.gasto('Alimentacion', '-500')

        fuera = self.fuera()
        self.assertEqual([b['nombre'] for b in fuera['bloques']], ['Variables'])
        self.assertEqual(fuera['exceso_total'], Decimal('100'))

    def test_el_gasto_anual_se_compara_prorrateado(self):
        """Un IBI de 520 € al año son 43,33 €/mes de límite."""
        self.declarar('IBI', '520', 'anual')
        self.gasto('IBI', '-100')

        fuera = self.fuera()['categorias']
        self.assertEqual(fuera[0]['nombre'], 'IBI')
        self.assertEqual(fuera[0]['limite'], Decimal('43.33'))

    def test_lo_que_no_tiene_presupuesto_va_aparte(self):
        """Sin límite declarado no está «fuera»: no hay con qué compararlo, y
        decir que se ha pasado sería inventárselo."""
        self.gasto('Alimentacion', '-300')

        fuera = self.fuera()
        self.assertEqual(fuera['categorias'], [])
        self.assertEqual([f['nombre'] for f in fuera['sin_presupuesto']], ['Alimentacion'])

    def test_en_discrecionales_solo_se_avisa_del_bloque(self):
        """Dentro de discrecionales no hay presupuesto por categoría ni tiene
        sentido que lo haya: el usuario sabe que tiene un tope para sus
        caprichos y quiere ver en qué se le fue, no un reproche por gastar
        40 € en una categoría para la que nunca declaró un límite."""
        self.declarar('Ocio', '100')             # discrecional, con límite
        self.gasto('Ocio', '-300')               # se pasa 200
        self.gasto('Restaurantes', '-250')       # discrecional, sin límite

        fuera = self.fuera()

        self.assertEqual([b['nombre'] for b in fuera['bloques']], ['Discrecionales'])
        # Ni la que se ha pasado ni la que no tiene límite salen por su nombre.
        self.assertEqual(fuera['categorias'], [])
        self.assertEqual(fuera['sin_presupuesto'], [])
        # Pero el reparto de dentro del bloque sigue estando, que es lo que se
        # quería ver: en qué se fue.
        bloque = next(b for b in self.panel()['bloques'] if b['tipo'] == 'discrecional')
        self.assertEqual(
            sorted(c['nombre'] for c in bloque['categorias']), ['Ocio', 'Restaurantes'],
        )

    def test_la_tarjeta_sigue_apareciendo_solo_con_categorias_sin_limite(self):
        """Sin excesos pero con gasto sin presupuestar, la tarjeta es la lista
        desde la que se declara: esconderla las deja invisibles para siempre."""
        self.gasto('Alimentacion', '-300')

        fuera = self.fuera()
        self.assertFalse(fuera['hay_exceso'])
        self.assertTrue(fuera['hay_algo'])
        self.assertEqual([f['nombre'] for f in fuera['sin_presupuesto']], ['Alimentacion'])

    def test_fuera_de_discrecionales_si_se_avisa_por_categoria(self):
        """El mismo caso en variables sí se desglosa: ahí el presupuesto se
        declara categoría a categoría y saltarse el límite es información."""
        self.declarar('Alimentacion', '100')
        self.gasto('Alimentacion', '-300')

        fuera = self.fuera()
        self.assertEqual([f['nombre'] for f in fuera['categorias']], ['Alimentacion'])

    def test_los_bloques_saben_si_caben_en_el_presupuesto(self):
        self.declarar('Alimentacion', '400')      # bloque variable
        self.declarar('Ocio', '300')              # bloque discrecional
        self.gasto('Alimentacion', '-500')        # variable: se pasa
        self.gasto('Ocio', '-100')                # discrecional: cabe

        bloques = {b['etiqueta']: b for b in self.panel()['bloques']}
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

        a = self.panel()['comparativa']
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
        self.assertContains(respuesta, 'conciliacion/?anio=2026&amp;mes=8')

    def test_sin_periodo_las_pestañas_van_limpias(self):
        respuesta = self.client.get(reverse('extractos:listar'))
        self.assertNotContains(respuesta, 'conciliacion/?anio=')

    def test_todos_no_es_un_periodo(self):
        respuesta = self.client.get(reverse('extractos:listar'), {'anio': 'all', 'mes': 'all'})
        self.assertNotContains(respuesta, 'conciliacion/?anio=')

    def test_ya_no_hay_pestaña_de_analisis(self):
        """Se fundió con Movimientos: dejar la pestaña apuntando a una redirección
        sería un sitio al que ir para acabar donde ya estabas."""
        respuesta = self.client.get(reverse('extractos:listar'))
        self.assertNotContains(respuesta, 'ext-nav-tab">\n        <svg class="icon"><use href="#i-evolution"/></svg> Análisis')
        self.assertNotContains(respuesta, '/extractos/analisis/')

    def test_movimientos_ofrece_volver_a_donde_estabas(self):
        respuesta = self.client.get(reverse('extractos:listar'), {
            'anio': 2026, 'mes': 8, 'volver': 'conciliacion',
        })
        panel = respuesta.context['panel']
        self.assertEqual(panel['volver_nombre'], 'Conciliación')
        self.assertEqual(panel['volver_url'], '/extractos/conciliacion/?anio=2026&mes=8')

    def test_un_destino_inventado_no_pinta_boton(self):
        """El «volver» es una lista blanca: aceptar cualquier URL sería un
        redirector abierto."""
        respuesta = self.client.get(reverse('extractos:listar'), {
            'volver': 'https://example.com/phishing',
        })
        self.assertEqual(respuesta.context['panel']['volver_url'], '')


class MediaMensualDelFiltroTests(TestCase):
    """«¿Cuánto me cuesta la gasolina al mes?» no lo responde el total del
    filtro, sino el total dividido entre los meses que abarca."""

    def setUp(self):
        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        self.user = User.objects.create_user(username='tester', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        self.extracto = ExtractoBancario.objects.create(hogar=self.hogar, usuario=self.user)
        self.client.force_login(self.user)
        self.combustible = CategoriaGasto.objects.get(hogar=self.hogar, nombre='Gasolina')
        self.alimentacion = CategoriaGasto.objects.get(hogar=self.hogar, nombre='Alimentacion')
        self._dia = 0

    def gasto(self, categoria, importe, anio=2026, mes=8, concepto='Gasolinera'):
        self._dia += 1
        return MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(anio, mes, self._dia),
            concepto=concepto, importe=Decimal(importe), categoria=categoria,
        )

    def media(self, **params):
        return self.client.get(reverse('extractos:listar'), params).context['panel']['media']

    def test_la_media_divide_entre_los_meses_del_periodo(self):
        self.gasto(self.combustible, '-60', mes=1)
        self.gasto(self.combustible, '-90', mes=2)
        self.gasto(self.combustible, '-30', mes=3)

        media = self.media(anio=2026, categoria=self.combustible.id)
        self.assertEqual(media['meses'], 3)
        self.assertEqual(media['gasto'], Decimal('60'))
        self.assertEqual(media['ambito'], 'Gasolina')
        self.assertTrue(media['mostrar'])

    def test_un_mes_sin_repostar_cuenta_como_cero(self):
        """Si hay extracto de marzo pero no se repostó, marzo sigue siendo un mes
        del periodo: dividir solo entre los meses con gasolina daría una media
        infladísima que no es lo que cuesta al mes."""
        self.gasto(self.combustible, '-60', mes=1)
        self.gasto(self.combustible, '-60', mes=2)
        self.gasto(self.alimentacion, '-200', mes=3)   # marzo tiene datos, pero no gasolina

        media = self.media(anio=2026, categoria=self.combustible.id)
        self.assertEqual(media['meses'], 3)
        self.assertEqual(media['gasto'], Decimal('40'))

    def test_el_año_acota_la_media(self):
        self.gasto(self.combustible, '-100', anio=2025, mes=1)
        self.gasto(self.combustible, '-100', anio=2025, mes=2)
        self.gasto(self.combustible, '-50', anio=2026, mes=1)

        self.assertEqual(self.media(anio=2025, categoria=self.combustible.id)['gasto'], Decimal('100'))
        self.assertEqual(self.media(anio=2026, categoria=self.combustible.id)['gasto'], Decimal('50'))

    def test_sin_año_es_la_media_de_siempre(self):
        self.gasto(self.combustible, '-100', anio=2025, mes=12)
        self.gasto(self.combustible, '-50', anio=2026, mes=1)

        media = self.media(categoria=self.combustible.id)
        self.assertEqual(media['meses'], 2)
        self.assertEqual(media['gasto'], Decimal('75'))

    def test_con_un_solo_mes_no_se_muestra(self):
        """La media de un mes ES el mes: repetir la cifra solo añade ruido."""
        self.gasto(self.combustible, '-60', mes=8)
        self.assertFalse(self.media(anio=2026, mes=8)['mostrar'])

    def test_la_media_dice_de_qué_es(self):
        self.gasto(self.alimentacion, '-100', mes=1)
        self.gasto(self.alimentacion, '-100', mes=2)

        self.assertEqual(self.media(anio=2026)['ambito'], 'todo el gasto')
        self.assertEqual(self.media(anio=2026, bloque='variable')['ambito'], 'Variables')
        self.assertEqual(self.media(anio=2026, q='mercadona')['ambito'], '«mercadona»')

    def test_la_media_trae_el_presupuesto_de_la_categoria(self):
        """Ver 60 €/mes de media al lado de los 50 €/mes declarados es lo que
        convierte la cifra en una decisión."""
        PartidaGasto.objects.create(
            hogar=self.hogar, categoria=self.combustible, nombre='Gasolina',
            importe=Decimal('50'), periodicidad='mensual',
        )
        self.gasto(self.combustible, '-60', mes=1)
        self.gasto(self.combustible, '-60', mes=2)

        media = self.media(anio=2026, categoria=self.combustible.id)
        self.assertEqual(media['gasto'], Decimal('60'))
        self.assertEqual(media['limite'], Decimal('50'))


class DesgloseDeCategoriaTests(TestCase):
    """El modal que se abre al pinchar una categoría dentro de su pilar: tiene
    que enseñar lo mismo que decía la fila y dejar cambiarlo ahí mismo."""

    def setUp(self):
        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        self.user = User.objects.create_user(username='tester', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        self.extracto = ExtractoBancario.objects.create(hogar=self.hogar, usuario=self.user)
        self.client.force_login(self.user)
        self.restaurantes = CategoriaGasto.objects.get(hogar=self.hogar, nombre='Restaurantes')
        self.alimentacion = CategoriaGasto.objects.get(hogar=self.hogar, nombre='Alimentacion')
        self._dia = 0

    def gasto(self, categoria, importe, mes=8, concepto='La Taberna'):
        self._dia += 1
        return MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(2026, mes, self._dia),
            concepto=concepto, importe=Decimal(importe), categoria=categoria,
        )

    def desglose(self, **params):
        return self.client.get(reverse('extractos:desglose_categoria'), params)

    def test_trae_los_movimientos_de_la_categoria_y_su_total(self):
        self.gasto(self.restaurantes, '-40')
        self.gasto(self.restaurantes, '-25', concepto='Bar Pepe')
        self.gasto(self.alimentacion, '-90', concepto='Mercadona')

        respuesta = self.desglose(categoria=self.restaurantes.id, anio=2026, mes=8)
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.context['total'], Decimal('65'))
        self.assertEqual(respuesta.context['num'], 2)
        self.assertEqual(
            sorted(m.concepto for m in respuesta.context['movimientos']),
            ['Bar Pepe', 'La Taberna'],
        )

    def test_respeta_el_periodo_de_la_pantalla(self):
        """Lo que se ve dentro tiene que sumar exactamente lo que decía la fila
        de la que se ha entrado; si no, el modal contradice a la pantalla."""
        self.gasto(self.restaurantes, '-40', mes=7)
        self.gasto(self.restaurantes, '-25', mes=8)

        self.assertEqual(self.desglose(categoria=self.restaurantes.id, anio=2026, mes=8).context['total'], Decimal('25'))
        self.assertEqual(self.desglose(categoria=self.restaurantes.id, anio=2026).context['total'], Decimal('65'))

    def test_trae_la_media_al_mes_y_el_reparto_por_mes(self):
        self.gasto(self.restaurantes, '-40', mes=7)
        self.gasto(self.restaurantes, '-20', mes=8)

        contexto = self.desglose(categoria=self.restaurantes.id, anio=2026).context
        self.assertEqual(contexto['meses_periodo'], 2)
        self.assertEqual(contexto['media_mes'], Decimal('30'))
        self.assertEqual(
            [m['etiqueta'] for m in contexto['meses']], ['Agosto 2026', 'Julio 2026'],
        )

    def test_dice_si_se_ha_pasado_del_presupuesto(self):
        PartidaGasto.objects.create(
            hogar=self.hogar, categoria=self.restaurantes, nombre='Restaurantes',
            importe=Decimal('50'), periodicidad='mensual',
        )
        self.gasto(self.restaurantes, '-80')

        contexto = self.desglose(categoria=self.restaurantes.id, anio=2026, mes=8).context
        self.assertEqual(contexto['limite'], Decimal('50'))
        self.assertFalse(contexto['dentro'])
        self.assertEqual(contexto['exceso'], Decimal('30'))

    def test_las_filas_son_las_mismas_del_listado_y_se_pueden_editar(self):
        """El modal reutiliza la plantilla de fila del listado: si trajera una
        versión reducida, cambiar algo desde ahí obligaría a volver abajo."""
        mov = self.gasto(self.restaurantes, '-40')
        contenido = self.desglose(categoria=self.restaurantes.id, anio=2026, mes=8).content.decode()

        self.assertIn(f'data-mov-id="{mov.id}"', contenido)
        self.assertIn('ext-cat-select', contenido)
        self.assertIn('ext-tag-add', contenido)

    def test_lo_sin_categorizar_tambien_tiene_desglose(self):
        suelto = self.gasto(None, '-15', concepto='Cargo raro')

        contexto = self.desglose(categoria='sin', anio=2026, mes=8).context
        self.assertIsNone(contexto['categoria'])
        self.assertEqual(contexto['nombre'], 'Sin categorizar')
        self.assertEqual([m.id for m in contexto['movimientos']], [suelto.id])

    def test_una_categoria_de_otro_hogar_no_se_desglosa(self):
        otro = Hogar.objects.create(nombre='Otro')
        ajena = CategoriaGasto.objects.create(hogar=otro, nombre='Ajena', tipo='variable')
        self.assertEqual(self.desglose(categoria=ajena.id).status_code, 404)

    def test_el_pilar_deja_abrir_el_desglose_de_cada_categoria(self):
        self.gasto(self.restaurantes, '-40')
        respuesta = self.client.get(reverse('extractos:listar'), {'anio': 2026, 'mes': 8})
        self.assertContains(respuesta, f'class="ext-cat-nombre ext-cat-abrir"')
        self.assertContains(respuesta, f'data-categoria="{self.restaurantes.id}"')


class CambioEnLoteTests(TestCase):
    """Marcar varios movimientos y cambiarlos de una vez: es lo que evita que un
    mes se quede a medio repasar."""

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
        self._dia = 0

    def mov(self, concepto='Un cargo', importe='-20'):
        self._dia += 1
        return MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(2026, 8, self._dia),
            concepto=concepto, importe=Decimal(importe),
        )

    def lote(self, **datos):
        return self.client.post(
            reverse('extractos:accion_lote'), datos, HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

    def test_categoriza_todos_los_seleccionados(self):
        a, b, c = self.mov(), self.mov(), self.mov()

        respuesta = self.lote(accion='categoria', categoria_id=self.ocio.id, ids=[a.id, b.id])
        self.assertEqual(respuesta.json()['num'], 2)

        a.refresh_from_db(); b.refresh_from_db(); c.refresh_from_db()
        self.assertEqual(a.categoria, self.ocio)
        self.assertEqual(b.categoria, self.ocio)
        self.assertEqual(a.estado_categorizacion, 'manual')
        self.assertIsNone(c.categoria)

    def test_tambien_puede_quitar_la_categoria(self):
        m = self.mov()
        m.categoria = self.ocio
        m.save()

        self.lote(accion='categoria', categoria_id='', ids=[m.id])
        m.refresh_from_db()
        self.assertIsNone(m.categoria)
        self.assertEqual(m.estado_categorizacion, 'sin_categorizar')

    def test_etiqueta_en_bloque_creando_la_etiqueta(self):
        a, b = self.mov(), self.mov()

        respuesta = self.lote(accion='etiqueta', nombre='Viaje a Lisboa', ids=[a.id, b.id])
        self.assertEqual(respuesta.json()['etiqueta'], 'Viaje a Lisboa')

        etiqueta = Etiqueta.objects.get(hogar=self.hogar, nombre='Viaje a Lisboa')
        self.assertEqual(
            set(etiqueta.movimientos.values_list('id', flat=True)), {a.id, b.id},
        )

    def test_imputa_a_un_activo_en_bloque(self):
        from finanzas.models import Vehiculo

        coche = Vehiculo.objects.create(hogar=self.hogar, nombre='Ibiza', tipo='coche')
        a, b = self.mov(), self.mov()

        self.lote(accion='activo', activo=coche.clave_activo, ids=[a.id, b.id])
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual(a.vehiculo, coche)
        self.assertEqual(b.vehiculo, coche)

    def test_marca_varios_pagos_de_un_gasto_anual(self):
        ibi = PartidaGasto.objects.create(
            hogar=self.hogar, nombre='IBI', importe=Decimal('520'), periodicidad='anual',
            categoria=CategoriaGasto.objects.get(hogar=self.hogar, nombre='IBI'),
        )
        a, b = self.mov(), self.mov()

        self.lote(accion='provision', partida_id=ibi.id, ids=[a.id, b.id])
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual(a.partida_conciliada, ibi)
        self.assertEqual(b.partida_conciliada, ibi)

    def test_elimina_los_seleccionados_y_recuenta_el_extracto(self):
        a, b, c = self.mov(), self.mov(), self.mov()

        self.lote(accion='eliminar', ids=[a.id, b.id])
        self.assertEqual(
            list(MovimientoBancario.objects.filter(hogar=self.hogar).values_list('id', flat=True)),
            [c.id],
        )
        self.extracto.refresh_from_db()
        self.assertEqual(self.extracto.num_movimientos, 1)

    def test_no_toca_movimientos_de_otro_hogar(self):
        otro = Hogar.objects.create(nombre='Otro')
        extracto_ajeno = ExtractoBancario.objects.create(hogar=otro, usuario=self.user)
        ajeno = MovimientoBancario.objects.create(
            extracto=extracto_ajeno, hogar=otro, fecha=date(2026, 8, 1),
            concepto='Ajeno', importe=Decimal('-10'),
        )
        mio = self.mov()

        respuesta = self.lote(accion='categoria', categoria_id=self.ocio.id, ids=[mio.id, ajeno.id])
        self.assertEqual(respuesta.json()['num'], 1)
        ajeno.refresh_from_db()
        self.assertIsNone(ajeno.categoria)

    def test_una_accion_inventada_no_hace_nada(self):
        """Las acciones están enumeradas a propósito: un endpoint que acepte «el
        campo que venga» sobre una lista de ids cambia cualquier cosa en bloque."""
        m = self.mov()
        respuesta = self.lote(accion='importe', importe='-9999', ids=[m.id])
        self.assertEqual(respuesta.status_code, 400)
        m.refresh_from_db()
        self.assertEqual(m.importe, Decimal('-20'))

    def test_sin_seleccion_no_se_aplica_nada(self):
        respuesta = self.lote(accion='categoria', categoria_id=self.ocio.id, ids=[])
        self.assertEqual(respuesta.status_code, 400)

    def test_la_pantalla_ofrece_la_barra_de_seleccion(self):
        self.mov()
        respuesta = self.client.get(reverse('extractos:listar'), {'anio': 2026, 'mes': 8})
        self.assertContains(respuesta, 'ext-lote')
        self.assertContains(respuesta, 'ext-check')


class ListadoLigeroTests(TestCase):
    """La pantalla de movimientos no puede mandar el histórico entero.

    Con tres años importados llegaba a veinte megas de HTML y cuatro segundos de
    render: el 79 % eran los tres desplegables de cada fila repetidos miles de
    veces, y el resto, los apuntes de meses que estaban plegados."""

    def setUp(self):
        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        self.user = User.objects.create_user(username='tester', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        self.extracto = ExtractoBancario.objects.create(hogar=self.hogar, usuario=self.user)
        self.client.force_login(self.user)

    def sembrar(self, por_mes, meses=(6, 7, 8)):
        for mes in meses:
            for dia in range(1, por_mes + 1):
                MovimientoBancario.objects.create(
                    extracto=self.extracto, hogar=self.hogar,
                    fecha=date(2026, mes, (dia % 28) + 1),
                    concepto=f'Compra {mes}-{dia}', importe=Decimal('-10.00'),
                    saldo=Decimal(str(1000 + dia)),
                )

    def test_la_fila_no_repite_las_opciones_de_los_desplegables(self):
        """Las opciones son las mismas en todas las filas: van una vez en un
        molde y se clonan al usarlas."""
        self.sembrar(2, meses=(8,))
        contenido = self.client.get(reverse('extractos:listar')).content.decode()

        self.assertIn('id="ext-molde-categoria"', contenido)
        # El molde trae las opciones; las filas, solo un botón con el valor.
        self.assertEqual(contenido.count('<template id="ext-molde-categoria">'), 1)
        self.assertIn('ext-cat-boton', contenido)

    def test_los_meses_plegados_llegan_sin_sus_movimientos(self):
        self.sembrar(150)   # 450 movimientos, por encima del umbral
        panel = self.client.get(reverse('extractos:listar')).context['panel']

        primero, resto = panel['grupos'][0], panel['grupos'][1:]
        self.assertFalse(primero['pendiente'])
        self.assertEqual(len(primero['movimientos']), 150)
        for grupo in resto:
            self.assertTrue(grupo['pendiente'])
            self.assertEqual(grupo['movimientos'], [])
            # La cabecera sí viaja: el total del mes se lee sin desplegarlo.
            self.assertEqual(grupo['num'], 150)
            self.assertEqual(grupo['gastos'], Decimal('-1500.00'))

    def test_con_pocos_movimientos_no_se_difiere_nada(self):
        """Pedir por red lo que cabe de sobra en la respuesta solo añade espera."""
        self.sembrar(10)
        panel = self.client.get(reverse('extractos:listar')).context['panel']
        self.assertTrue(all(not g['pendiente'] for g in panel['grupos']))
        self.assertTrue(all(g['movimientos'] for g in panel['grupos']))

    def test_desplegar_un_mes_trae_exactamente_sus_filas(self):
        self.sembrar(150)
        respuesta = self.client.get(reverse('extractos:filas_mes'), {
            'anio_mes_anio': 2026, 'anio_mes_mes': 7,
        })
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(len(respuesta.context['movimientos']), 150)
        self.assertTrue(all(m.fecha.month == 7 for m in respuesta.context['movimientos']))

    def test_el_mes_desplegado_respeta_los_filtros_de_la_pantalla(self):
        """Lo que se despliega tiene que sumar lo que dice la cabecera del mes."""
        self.sembrar(150)
        ocio = CategoriaGasto.objects.get(hogar=self.hogar, nombre='Ocio')
        marcado = MovimientoBancario.objects.filter(fecha__month=7).first()
        marcado.categoria = ocio
        marcado.save()

        respuesta = self.client.get(reverse('extractos:filas_mes'), {
            'anio_mes_anio': 2026, 'anio_mes_mes': 7, 'categoria': ocio.id,
        })
        self.assertEqual([m.id for m in respuesta.context['movimientos']], [marcado.id])

    def test_un_mes_inventado_no_devuelve_nada(self):
        respuesta = self.client.get(reverse('extractos:filas_mes'), {
            'anio_mes_anio': 2026, 'anio_mes_mes': 13,
        })
        self.assertEqual(respuesta.status_code, 400)

    def test_dentro_de_un_extracto_solo_se_despliegan_sus_movimientos(self):
        """El panel también se usa en el detalle de UN extracto."""
        self.sembrar(150)
        otro = ExtractoBancario.objects.create(hogar=self.hogar, usuario=self.user)
        ajeno = MovimientoBancario.objects.create(
            extracto=otro, hogar=self.hogar, fecha=date(2026, 7, 5),
            concepto='De otro extracto', importe=Decimal('-99.00'),
        )
        respuesta = self.client.get(reverse('extractos:filas_mes'), {
            'anio_mes_anio': 2026, 'anio_mes_mes': 7, 'extracto': self.extracto.id,
        })
        ids = [m.id for m in respuesta.context['movimientos']]
        self.assertNotIn(ajeno.id, ids)
        self.assertEqual(len(ids), 150)

    def test_el_listado_completo_no_se_dispara_de_tamaño(self):
        """La prueba que faltaba: la página responde 200 igual estando gorda,
        así que ningún test veía los veinte megas."""
        self.sembrar(150, meses=(1, 2, 3, 4, 5, 6, 7, 8))   # 1.200 movimientos
        contenido = self.client.get(reverse('extractos:listar')).content
        self.assertLess(
            len(contenido), 900_000,
            f'la pantalla de movimientos pesa {len(contenido)//1024} KB: '
            'algo ha vuelto a pintar de más por fila o por mes.',
        )


class PagosAnualesEnElPanelTests(TestCase):
    """Un gasto que se provisiona todo el año y se paga de golpe no puede
    compararse contra el mes en el que cae.

    La conciliación ya lo hacía; el panel de movimientos no, así que pagar la
    revisión del coche en septiembre decía «te has pasado 1.085 €» cuando lo
    que habías hecho era pagar exactamente lo que tenías apartado.
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
        self.mantenimiento = CategoriaGasto.objects.get(
            hogar=self.hogar, nombre='Mantenimiento vehicular')
        # 1.968 €/año de revisión = 164 €/mes de provisión.
        self.revision = PartidaGasto.objects.create(
            hogar=self.hogar, categoria=self.mantenimiento, nombre='Revisión Polo',
            importe=Decimal('1968'), periodicidad='anual',
        )
        self.alimentacion = CategoriaGasto.objects.get(hogar=self.hogar, nombre='Alimentacion')
        PartidaGasto.objects.create(
            hogar=self.hogar, categoria=self.alimentacion, nombre='Compra',
            importe=Decimal('400'), periodicidad='mensual',
        )

    def mov(self, importe, dia, categoria, provision=None, concepto='Norauto'):
        return MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(2026, 9, dia),
            concepto=f'{concepto} {dia}', importe=Decimal(importe),
            categoria=categoria, partida_conciliada=provision,
        )

    def panel(self, **params):
        return self.client.get(reverse('extractos:listar'), params).context['panel']

    def test_el_pago_anual_no_cuenta_contra_el_limite_del_mes(self):
        self.mov('-1249.34', 5, self.mantenimiento, provision=self.revision)
        self.mov('-380', 10, self.alimentacion)

        panel = self.panel(anio=2026, mes=9)

        # El gasto del mes es solo lo mensual.
        self.assertEqual(panel['kpi_gastos'], Decimal('-380'))
        bloques = {b['tipo']: b for b in panel['bloques']}
        self.assertNotIn('anual', bloques)
        self.assertTrue(bloques['variable']['dentro'])

    def test_y_tampoco_se_avisa_de_que_esa_categoria_se_ha_pasado(self):
        """Era el aviso que no tenía sentido: «Mantenimiento vehicular
        1.249 € de 164 €»."""
        self.mov('-1249.34', 5, self.mantenimiento, provision=self.revision)

        fuera = self.panel(anio=2026, mes=9)['fuera_presupuesto']
        self.assertEqual([f['nombre'] for f in fuera['categorias']], [])
        self.assertEqual([b['nombre'] for b in fuera['bloques']], [])

    def test_la_pantalla_explica_a_dónde_ha_ido_ese_dinero(self):
        """Si no, el bloque de los anuales desaparece y parece que la pantalla
        se ha comido mil euros."""
        self.mov('-1249.34', 5, self.mantenimiento, provision=self.revision)

        respuesta = self.client.get(reverse('extractos:listar'), {'anio': 2026, 'mes': 9})
        self.assertEqual(respuesta.context['panel']['total_provisiones'], Decimal('1249.34'))
        self.assertContains(respuesta, 'pagos de gastos que')

    def test_sobre_todo_el_año_el_pago_anual_sí_cuenta(self):
        """Con doce meses a la vista los dos lados se promedian bien y sacarlo
        sería esconder gasto real."""
        self.mov('-1249.34', 5, self.mantenimiento, provision=self.revision)

        panel = self.panel(anio=2026)
        self.assertEqual(panel['kpi_gastos'], Decimal('-1249.34'))
        self.assertIn('anual', {b['tipo'] for b in panel['bloques']})
        self.assertEqual(panel['pagos_provision'], [])

    def test_un_gasto_del_mes_sin_marcar_sigue_avisando(self):
        """Solo se perdona lo que está marcado como pago de una provisión: un
        gasto normal que se pasa tiene que seguir saltando."""
        self.mov('-900', 5, self.alimentacion)

        fuera = self.panel(anio=2026, mes=9)['fuera_presupuesto']
        self.assertEqual([f['nombre'] for f in fuera['categorias']], ['Alimentacion'])

    def test_el_bloque_anual_conserva_su_limite_en_la_vista_del_mes(self):
        """Los 164 €/mes que apartas para la revisión son el presupuesto de ese
        mes aunque el recibo llegue en septiembre. Se quitaban junto con el pago
        y el bloque salía «sin límite», cuando tiene uno bien definido."""
        # Un gasto del bloque anual SIN marcar como pago de provisión: es lo que
        # se compara contra lo que se aparta cada mes.
        self.mov('-200', 8, self.mantenimiento)

        bloques = {b['tipo']: b for b in self.panel(anio=2026, mes=9)['bloques']}
        self.assertEqual(bloques['anual']['limite'], Decimal('164.00'))
        self.assertFalse(bloques['anual']['dentro'])   # 200 > 164

    def test_el_pago_marcado_sale_del_gasto_pero_el_limite_sigue(self):
        self.mov('-1249.34', 9, self.mantenimiento, provision=self.revision)
        self.mov('-50', 9, self.mantenimiento)

        panel = self.panel(anio=2026, mes=9)
        bloques = {b['tipo']: b for b in panel['bloques']}
        # Solo cuentan los 50 € no marcados, contra los 164 € que se apartan.
        self.assertEqual(bloques['anual']['importe'], Decimal('50'))
        self.assertEqual(bloques['anual']['limite'], Decimal('164.00'))
        self.assertTrue(bloques['anual']['dentro'])

class DividirMovimientoTests(TestCase):
    """Un cobro puede ser varias cosas a la vez.

    En Norauto se paga de una vez los neumáticos y la revisión anual: son dos
    partidas distintas del presupuesto, y el movimiento solo admite una
    categoría."""

    def setUp(self):
        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        self.user = User.objects.create_user(username='tester', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        self.extracto = ExtractoBancario.objects.create(hogar=self.hogar, usuario=self.user)
        self.client.force_login(self.user)
        self.mantenimiento = CategoriaGasto.objects.get(
            hogar=self.hogar, nombre='Mantenimiento vehicular')
        self.gasolina = CategoriaGasto.objects.get(hogar=self.hogar, nombre='Gasolina')
        self.mov = MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(2026, 9, 5),
            concepto='Norauto', importe=Decimal('-928.63'), saldo=Decimal('1500.00'),
        )

    def dividir(self, mov, partes):
        datos = {'importe': [], 'categoria_id': [], 'concepto': []}
        for importe, categoria, concepto in partes:
            datos['importe'].append(importe)
            datos['categoria_id'].append(str(categoria.id) if categoria else '')
            datos['concepto'].append(concepto)
        return self.client.post(
            reverse('extractos:dividir_movimiento', args=[mov.id]), datos,
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

    def test_reparte_el_cobro_en_partes_con_su_propia_categoria(self):
        respuesta = self.dividir(self.mov, [
            ('-700.00', self.mantenimiento, 'Neumáticos'),
            ('-228.63', self.gasolina, 'Revisión'),
        ])
        self.assertEqual(respuesta.json(), {'ok': True, 'partes': 2})

        partes = list(self.mov.partes.order_by('orden_parte'))
        self.assertEqual([p.concepto for p in partes], ['Neumáticos', 'Revisión'])
        self.assertEqual([p.importe for p in partes], [Decimal('-700.00'), Decimal('-228.63')])
        self.assertEqual([p.categoria for p in partes], [self.mantenimiento, self.gasolina])

    def test_el_original_se_conserva_pero_deja_de_contar(self):
        """Es lo que dice el banco y lo que evita que reimportar el extracto lo
        duplique, pero si siguiera sumando contaríamos el dinero dos veces."""
        self.dividir(self.mov, [
            ('-700.00', self.mantenimiento, 'Neumáticos'),
            ('-228.63', self.gasolina, 'Revisión'),
        ])
        self.mov.refresh_from_db()

        self.assertTrue(self.mov.esta_dividido)
        self.assertFalse(self.mov.cuenta_como_gasto)
        self.assertTrue(MovimientoBancario.objects.filter(pk=self.mov.pk).exists())

    def test_los_totales_no_cuentan_el_dinero_dos_veces(self):
        self.dividir(self.mov, [
            ('-700.00', self.mantenimiento, 'Neumáticos'),
            ('-228.63', self.gasolina, 'Revisión'),
        ])
        panel = self.client.get(
            reverse('extractos:listar'), {'anio': 2026, 'mes': 9},
        ).context['panel']

        self.assertEqual(panel['kpi_gastos'], Decimal('-928.63'))
        por_categoria = {
            c['nombre']: c['importe']
            for b in panel['bloques'] for c in b['categorias']
        }
        self.assertEqual(por_categoria['Mantenimiento vehicular'], Decimal('700.00'))
        self.assertEqual(por_categoria['Gasolina'], Decimal('228.63'))

    def test_las_partes_tienen_que_sumar_el_cobro(self):
        """Si no cuadran, el reparto no representa lo que pasó."""
        respuesta = self.dividir(self.mov, [
            ('-700.00', self.mantenimiento, 'Neumáticos'),
            ('-100.00', self.gasolina, 'Revisión'),
        ])
        self.assertEqual(respuesta.status_code, 400)
        self.assertEqual(respuesta.json()['error'], 'no_cuadra')
        self.assertEqual(self.mov.partes.count(), 0)

    def test_dividir_de_nuevo_reemplaza_el_reparto_anterior(self):
        self.dividir(self.mov, [
            ('-700.00', self.mantenimiento, 'Neumáticos'),
            ('-228.63', self.gasolina, 'Revisión'),
        ])
        self.dividir(self.mov, [
            ('-500.00', self.mantenimiento, 'Neumáticos'),
            ('-428.63', self.gasolina, 'Revisión'),
        ])
        self.assertEqual(self.mov.partes.count(), 2)
        self.assertEqual(
            sorted(p.importe for p in self.mov.partes.all()),
            [Decimal('-500.00'), Decimal('-428.63')],
        )

    def test_dos_partes_iguales_no_chocan_entre_si(self):
        """Pagar dos ruedas del mismo precio es legítimo, y el unique por hash
        las rechazaría sin algo que las distinga."""
        respuesta = self.dividir(self.mov, [
            ('-464.315', self.mantenimiento, 'Rueda'),
            ('-464.315', self.mantenimiento, 'Rueda'),
        ])
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(self.mov.partes.count(), 2)

    def test_deshacer_devuelve_el_movimiento_a_contar_solo(self):
        self.dividir(self.mov, [
            ('-700.00', self.mantenimiento, 'Neumáticos'),
            ('-228.63', self.gasolina, 'Revisión'),
        ])
        respuesta = self.client.post(
            reverse('extractos:deshacer_division', args=[self.mov.id]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertTrue(respuesta.json()['ok'])

        self.mov.refresh_from_db()
        self.assertFalse(self.mov.esta_dividido)
        self.assertTrue(self.mov.cuenta_como_gasto)
        self.assertEqual(self.mov.partes.count(), 0)

    def test_una_sola_parte_no_es_dividir(self):
        respuesta = self.dividir(self.mov, [('-928.63', self.mantenimiento, 'Todo')])
        self.assertEqual(respuesta.status_code, 400)
        self.assertEqual(respuesta.json()['error'], 'minimo_dos')

    def test_no_se_divide_una_parte(self):
        self.dividir(self.mov, [
            ('-700.00', self.mantenimiento, 'Neumáticos'),
            ('-228.63', self.gasolina, 'Revisión'),
        ])
        parte = self.mov.partes.first()
        respuesta = self.dividir(parte, [
            ('-350.00', self.mantenimiento, 'A'), ('-350.00', self.gasolina, 'B'),
        ])
        self.assertEqual(respuesta.status_code, 400)
        self.assertEqual(respuesta.json()['error'], 'ya_es_parte')

    def test_borrar_el_original_se_lleva_sus_partes(self):
        self.dividir(self.mov, [
            ('-700.00', self.mantenimiento, 'Neumáticos'),
            ('-228.63', self.gasolina, 'Revisión'),
        ])
        self.mov.delete()
        self.assertEqual(MovimientoBancario.objects.filter(hogar=self.hogar).count(), 0)

    def test_una_parte_puede_ser_el_pago_de_un_gasto_anual(self):
        """El caso de Norauto entero: los neumáticos son gasto del mes y la
        revisión es el pago de la provisión anual."""
        revision = PartidaGasto.objects.create(
            hogar=self.hogar, categoria=self.mantenimiento, nombre='Revisión Polo',
            importe=Decimal('1968'), periodicidad='anual',
        )
        self.dividir(self.mov, [
            ('-700.00', self.mantenimiento, 'Neumáticos'),
            ('-228.63', self.mantenimiento, 'Revisión anual'),
        ])
        parte = self.mov.partes.get(concepto='Revisión anual')
        self.client.post(
            reverse('extractos:marcar_provision', args=[parte.id]),
            {'partida_id': revision.id}, HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        parte.refresh_from_db()
        self.assertTrue(parte.es_pago_provision)

        # En la vista del mes solo cuentan los neumáticos.
        panel = self.client.get(
            reverse('extractos:listar'), {'anio': 2026, 'mes': 9},
        ).context['panel']
        self.assertEqual(panel['kpi_gastos'], Decimal('-700.00'))
        self.assertEqual(panel['total_provisiones'], Decimal('228.63'))

    def test_el_reparto_no_devuelve_la_pantalla_al_n_mas_uno(self):
        """Saber si un movimiento está dividido se consulta para CADA fila al
        calcular los totales: sin prefetch eran miles de consultas."""
        for i in range(60):
            MovimientoBancario.objects.create(
                extracto=self.extracto, hogar=self.hogar, fecha=date(2026, 9, 1 + i % 28),
                concepto=f'Compra {i}', importe=Decimal('-12.00'), saldo=Decimal(str(900 + i)),
            )
        self.dividir(self.mov, [
            ('-700.00', self.mantenimiento, 'Neumáticos'),
            ('-228.63', self.gasolina, 'Revisión'),
        ])
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as ctx:
            self.client.get(reverse('extractos:listar'), {'anio': 2026, 'mes': 9})

        # Umbral, no número exacto: lo que importa es que NO crezca con el
        # número de movimientos, no cuántas consultas hace hoy la pantalla.
        self.assertLess(
            len(ctx.captured_queries), 40,
            f'{len(ctx.captured_queries)} consultas para 61 movimientos: '
            'algo vuelve a preguntar por fila.',
        )

    def test_las_partes_heredan_el_activo_del_cobro(self):
        """El cobro deja de contar al repartirse, así que si sus partes no
        heredan el coche, el gasto desaparece de su ficha."""
        from finanzas.models import Vehiculo

        coche = Vehiculo.objects.create(hogar=self.hogar, nombre='Polo', tipo='coche')
        self.mov.vehiculo = coche
        self.mov.save()

        self.dividir(self.mov, [
            ('-700.00', self.mantenimiento, 'Neumáticos'),
            ('-228.63', self.gasolina, 'Revisión'),
        ])
        self.assertEqual(
            [p.vehiculo_id for p in self.mov.partes.all()], [coche.id, coche.id],
        )

    def test_el_gasto_no_se_pierde_de_la_ficha_del_coche(self):
        from finanzas import costes_activo
        from finanzas.models import Vehiculo

        coche = Vehiculo.objects.create(hogar=self.hogar, nombre='Polo', tipo='coche')
        self.mov.vehiculo = coche
        self.mov.save()
        antes = costes_activo.costes(coche, 2026)['real_anual']

        self.dividir(self.mov, [
            ('-700.00', self.mantenimiento, 'Neumáticos'),
            ('-228.63', self.gasolina, 'Revisión'),
        ])
        self.assertEqual(costes_activo.costes(coche, 2026)['real_anual'], antes)

    def test_un_recibo_puede_repartirse_entre_dos_vehiculos(self):
        """El caso de Mybox: un solo recibo paga el seguro de dos coches y algo
        más, y cada parte tiene que ir a su ficha."""
        from finanzas import costes_activo
        from finanzas.models import Vehiculo

        polo = Vehiculo.objects.create(hogar=self.hogar, nombre='Polo', tipo='coche')
        golf = Vehiculo.objects.create(hogar=self.hogar, nombre='Golf', tipo='coche')
        seguros = CategoriaGasto.objects.get(hogar=self.hogar, nombre='Seguros')
        recibo = MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(2026, 4, 3),
            concepto='Mybox', importe=Decimal('-180.00'), saldo=Decimal('900'),
        )

        respuesta = self.client.post(
            reverse('extractos:dividir_movimiento', args=[recibo.id]), {
                'importe': ['-60.00', '-70.00', '-50.00'],
                'categoria_id': [str(seguros.id), str(seguros.id), str(seguros.id)],
                'concepto': ['Seguro Polo', 'Seguro Golf', 'Resto del recibo'],
                'activo': [polo.clave_activo, golf.clave_activo, ''],
            }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(respuesta.json(), {'ok': True, 'partes': 3})

        self.assertEqual(costes_activo.costes(polo, 2026)['real_anual'], Decimal('60.00'))
        self.assertEqual(costes_activo.costes(golf, 2026)['real_anual'], Decimal('70.00'))
        # El resto no es de ningún coche y no se le cuela a ninguno.
        resto = recibo.partes.get(concepto='Resto del recibo')
        self.assertIsNone(resto.vehiculo_id)
        self.assertIsNone(resto.propiedad_id)

    def test_una_parte_puede_ir_a_una_propiedad_y_otra_a_un_coche(self):
        from finanzas import costes_activo
        from finanzas.models import Propiedad, Vehiculo

        coche = Vehiculo.objects.create(hogar=self.hogar, nombre='Polo', tipo='coche')
        piso = Propiedad.objects.create(
            hogar=self.hogar, nombre='Piso', fecha_compra=date(2020, 1, 1),
            precio_compra=Decimal('100000'), valor_actual=Decimal('120000'),
        )
        self.dividir_con_activos([
            ('-500.00', 'Coche', coche.clave_activo),
            ('-428.63', 'Casa', piso.clave_activo),
        ])
        self.assertEqual(costes_activo.costes(coche, 2026)['real_anual'], Decimal('500.00'))
        self.assertEqual(costes_activo.costes(piso, 2026)['real_anual'], Decimal('428.63'))

    def dividir_con_activos(self, partes):
        return self.client.post(
            reverse('extractos:dividir_movimiento', args=[self.mov.id]), {
                'importe': [p[0] for p in partes],
                'categoria_id': [str(self.mantenimiento.id)] * len(partes),
                'concepto': [p[1] for p in partes],
                'activo': [p[2] for p in partes],
            }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

    def test_un_activo_de_otro_hogar_no_cuela(self):
        from finanzas.models import Vehiculo

        otro = Hogar.objects.create(nombre='Otro')
        ajeno = Vehiculo.objects.create(hogar=otro, nombre='Ajeno', tipo='coche')
        self.dividir_con_activos([
            ('-500.00', 'Uno', ajeno.clave_activo),
            ('-428.63', 'Dos', ''),
        ])
        self.assertTrue(all(p.vehiculo_id is None for p in self.mov.partes.all()))


class ReservaQueCubreUnPagoTests(TestCase):
    """El dinero que tenías apartado no es un ingreso: rebaja un pago concreto.

    Ahorras todo el año, sin que se vea, para la revisión del coche. Cuando
    llega el recibo de 1.200 €, metes 928 € de esa hucha en la cuenta. El golpe
    real del mes fueron 272 €, pero el banco solo sabe de un cargo de 1.200 y un
    abono de 928 sin relación entre ellos: septiembre parecía un desastre y el
    mes de la recarga, un milagro.

    Emparejando el abono con el pago —pago a pago, no por fondos— se puede decir
    exactamente cuánto puso el ahorro. Con dos reglas que son el fondo del
    asunto: el coche sigue costando 1.200 €, y un gasto SIN contraparte no es
    automáticamente un exceso, porque puede que aún no hubieras recargado.
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
        self.mantenimiento = CategoriaGasto.objects.get(
            hogar=self.hogar, nombre='Mantenimiento vehicular')
        # 1.200 €/año de revisión = 100 €/mes apartados.
        self.revision = PartidaGasto.objects.create(
            hogar=self.hogar, categoria=self.mantenimiento, nombre='Revisión del coche',
            importe=Decimal('1200'), periodicidad='anual',
        )

    def mov(self, importe, dia=5, categoria=None, provision=None, concepto='Norauto'):
        return MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(2026, 9, dia),
            concepto=f'{concepto} {dia}', importe=Decimal(importe),
            categoria=categoria, partida_conciliada=provision,
        )

    def emparejar(self, reposicion, pago):
        return self.client.post(
            reverse('extractos:cubrir_con_reserva', args=[reposicion.id]),
            {'cubre': str(pago.id) if pago else ''},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

    def panel(self, **params):
        return self.client.get(reverse('extractos:listar'), params).context['panel']

    # ── Lo que pesó de verdad ────────────────────────────────────────────

    def test_el_impacto_real_es_el_pago_menos_lo_que_puso_la_reserva(self):
        pago = self.mov('-1200', 12, self.mantenimiento)
        reposicion = self.mov('928', 10, concepto='Traspaso de la hucha')
        self.emparejar(reposicion, pago)

        pago.refresh_from_db()
        self.assertEqual(pago.cubierto_por_reserva, Decimal('928'))
        self.assertEqual(pago.impacto_real, Decimal('272'))

    def test_cubrir_de_mas_no_convierte_un_gasto_en_ingreso(self):
        pago = self.mov('-1200', 12, self.mantenimiento)
        self.emparejar(self.mov('1500', 10, concepto='Hucha'), pago)

        self.assertEqual(pago.impacto_real, Decimal('0'))

    def test_la_reposicion_no_cuenta_como_ingreso(self):
        """Es dinero tuyo cambiando de sitio. Contarlo como ingreso inflaría el
        mes y descuadraría el ahorro."""
        pago = self.mov('-1200', 12, self.mantenimiento)
        reposicion = self.mov('928', 10, concepto='Hucha')
        self.emparejar(reposicion, pago)

        reposicion.refresh_from_db()
        self.assertTrue(reposicion.es_neutro)
        self.assertFalse(reposicion.cuenta_como_ingreso)
        self.assertEqual(self.panel(anio=2026, mes=9)['kpi_ingresos'], Decimal('0'))

    # ── Lo que pide el usuario ver en septiembre ─────────────────────────

    def test_septiembre_ensena_los_272_que_se_pasaron_y_no_los_1200(self):
        """El caso entero, tal cual: «que cuando vaya a septiembre, gastos fijos
        anuales y lo despliegue, me salga reflejado ahí que me excedí»."""
        pago = self.mov('-1200', 12, self.mantenimiento, provision=self.revision)
        self.emparejar(self.mov('928', 10, concepto='De la reserva'), pago)

        panel = self.panel(anio=2026, mes=9)
        bloques = {b['tipo']: b for b in panel['bloques']}
        self.assertEqual(bloques['anual']['importe'], Decimal('272'))
        self.assertEqual(bloques['anual']['limite'], Decimal('100.00'))
        self.assertFalse(bloques['anual']['dentro'])
        self.assertEqual(panel['kpi_gastos'], Decimal('-272'))
        self.assertEqual(panel['cubierto_reserva'], Decimal('928'))

    def test_y_se_despliega_con_su_categoria_dentro(self):
        pago = self.mov('-1200', 12, self.mantenimiento, provision=self.revision)
        self.emparejar(self.mov('928', 10, concepto='De la reserva'), pago)

        bloques = {b['tipo']: b for b in self.panel(anio=2026, mes=9)['bloques']}
        categorias = {c['nombre']: c['importe'] for c in bloques['anual']['categorias']}
        self.assertEqual(categorias['Mantenimiento vehicular'], Decimal('272'))

    def test_un_pago_emparejado_ya_no_se_saca_del_mes(self):
        """Sin emparejar se saca entero, porque no se sabe qué lo pagó. En
        cuanto lo dices, se queda: los 272 € que no cubriste son del mes."""
        pago = self.mov('-1200', 12, self.mantenimiento, provision=self.revision)
        self.emparejar(self.mov('928', 10, concepto='De la reserva'), pago)

        panel = self.panel(anio=2026, mes=9)
        self.assertEqual(panel['pagos_provision'], [])
        self.assertEqual(panel['total_provisiones'], Decimal('0'))

    # ── Lo que NO debe pasar ─────────────────────────────────────────────

    def test_un_gasto_sin_contraparte_no_es_automaticamente_un_exceso(self):
        """«Puede que no haya recargado el dinero lo suficientemente rápido.»
        Sin emparejar no se sabe nada, así que se sigue tratando como hasta
        ahora: fuera del mes, a comparar con el año."""
        self.mov('-50', 20, self.mantenimiento, provision=self.revision, concepto='Otro coche')

        panel = self.panel(anio=2026, mes=9)
        self.assertEqual(panel['kpi_gastos'], Decimal('0'))
        self.assertEqual(panel['total_provisiones'], Decimal('50'))

    def test_el_pago_cubierto_no_esconde_el_gasto_de_otro_coche(self):
        """Cada activo es independiente: emparejar la revisión de uno no puede
        arrastrar el gasto del otro."""
        pago = self.mov('-1200', 12, self.mantenimiento, provision=self.revision)
        self.emparejar(self.mov('928', 10, concepto='De la reserva'), pago)
        suelto = self.mov('-50', 20, self.mantenimiento, concepto='Otro coche')

        bloques = {b['tipo']: b for b in self.panel(anio=2026, mes=9)['bloques']}
        self.assertEqual(bloques['anual']['importe'], Decimal('322'))   # 272 + 50
        self.assertEqual(suelto.impacto_real, Decimal('50'))

    def test_en_el_año_la_revision_vuelve_a_costar_1200(self):
        """La reserva es cosa de CUÁNDO, no de cuánto: sobre doce meses el
        ahorro salió de meses que están dentro, y descontarlo diría que el coche
        costó 272 €."""
        pago = self.mov('-1200', 12, self.mantenimiento, provision=self.revision)
        self.emparejar(self.mov('928', 10, concepto='De la reserva'), pago)

        panel = self.panel(anio=2026)
        bloques = {b['tipo']: b for b in panel['bloques']}
        self.assertEqual(bloques['anual']['importe'], Decimal('1200'))
        self.assertEqual(panel['kpi_gastos'], Decimal('-1200'))

    def test_la_ficha_del_coche_sigue_diciendo_1200(self):
        """Lo que cuesta mantener el coche no depende de con qué dinero se
        pagó. Es la respuesta que dio el usuario cuando se le preguntó."""
        from finanzas.costes_activo import costes
        from finanzas.models import Vehiculo

        coche = Vehiculo.objects.create(hogar=self.hogar, nombre='Polo', tipo='coche')
        pago = self.mov('-1200', 12, self.mantenimiento, provision=self.revision)
        pago.vehiculo = coche
        pago.save(update_fields=['vehiculo'])
        self.emparejar(self.mov('928', 10, concepto='De la reserva'), pago)

        self.assertEqual(costes(coche, 2026)['real_anual'], Decimal('1200'))

    def test_una_devolucion_sigue_restando_de_su_categoria(self):
        """`impacto_real` va en las mismas unidades que `-importe`. Con un abs()
        ahí dentro, devolver 30 € se contaba como gastarlos."""
        self.mov('-200', 5, self.mantenimiento)
        self.mov('30', 6, self.mantenimiento, concepto='Devolución')

        bloques = {b['tipo']: b for b in self.panel(anio=2026, mes=9)['bloques']}
        self.assertEqual(bloques['anual']['importe'], Decimal('170'))

    # ── Lo que la vista no deja hacer ────────────────────────────────────

    def test_no_se_puede_cubrir_a_si_mismo(self):
        mov = self.mov('928', 10, concepto='Hucha')
        respuesta = self.emparejar(mov, mov)

        self.assertEqual(respuesta.status_code, 400)
        mov.refresh_from_db()
        self.assertIsNone(mov.cubre_id)

    def test_no_se_pueden_encadenar_coberturas(self):
        """Si una reposición pudiera cubrir a otra reposición, el impacto real
        dejaría de significar nada."""
        pago = self.mov('-1200', 12, self.mantenimiento)
        primera = self.mov('928', 10, concepto='Hucha')
        self.emparejar(primera, pago)

        segunda = self.mov('100', 11, concepto='Más hucha')
        self.assertEqual(self.emparejar(segunda, primera).status_code, 400)

    def test_una_reserva_no_cubre_un_ingreso(self):
        nomina = self.mov('2000', 1, concepto='Nómina')
        respuesta = self.emparejar(self.mov('928', 10, concepto='Hucha'), nomina)

        self.assertEqual(respuesta.status_code, 400)

    def test_se_puede_deshacer_el_emparejamiento(self):
        pago = self.mov('-1200', 12, self.mantenimiento)
        reposicion = self.mov('928', 10, concepto='Hucha')
        self.emparejar(reposicion, pago)
        self.emparejar(reposicion, None)

        reposicion.refresh_from_db()
        self.assertIsNone(reposicion.cubre_id)
        self.assertEqual(pago.impacto_real, Decimal('1200'))

    def test_no_se_puede_cubrir_un_pago_de_otro_hogar(self):
        otro_hogar = Hogar.objects.create(nombre='Otro')
        otro_usuario = User.objects.create_user(username='ajeno', password='x')
        otro_extracto = ExtractoBancario.objects.create(hogar=otro_hogar, usuario=otro_usuario)
        ajeno = MovimientoBancario.objects.create(
            extracto=otro_extracto, hogar=otro_hogar, fecha=date(2026, 9, 12),
            concepto='Pago ajeno', importe=Decimal('-500'),
        )
        respuesta = self.emparejar(self.mov('928', 10, concepto='Hucha'), ajeno)

        self.assertEqual(respuesta.status_code, 400)

    # ── La lista de pagos que se pueden cubrir ───────────────────────────

    def test_los_candidatos_son_gastos_cercanos(self):
        cercano = self.mov('-1200', 12, self.mantenimiento)
        lejano = MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(2026, 1, 5),
            concepto='Muy lejos', importe=Decimal('-300'),
        )
        reposicion = self.mov('928', 10, concepto='Hucha')

        datos = self.client.get(
            reverse('extractos:pagos_cubribles'), {'mov': reposicion.id},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        ).json()
        ids = {p['id'] for p in datos['pagos']}
        self.assertIn(cercano.id, ids)
        self.assertNotIn(lejano.id, ids)
        self.assertNotIn(reposicion.id, ids)

    def test_el_candidato_dice_cuanto_le_falta_por_cubrir(self):
        pago = self.mov('-1200', 12, self.mantenimiento)
        self.emparejar(self.mov('400', 9, concepto='Primera hucha'), pago)
        otra = self.mov('528', 11, concepto='Segunda hucha')

        datos = self.client.get(
            reverse('extractos:pagos_cubribles'), {'mov': otra.id},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        ).json()
        fila = next(p for p in datos['pagos'] if p['id'] == pago.id)
        self.assertEqual(fila['cubierto'], 400)
        self.assertEqual(fila['pendiente'], 800)

    def test_dos_reposiciones_suman_sobre_el_mismo_pago(self):
        pago = self.mov('-1200', 12, self.mantenimiento)
        self.emparejar(self.mov('400', 9, concepto='Una'), pago)
        self.emparejar(self.mov('528', 11, concepto='Otra'), pago)

        self.assertEqual(pago.impacto_real, Decimal('272'))

    def test_la_comparativa_con_la_media_tambien_pesa_lo_real(self):
        """Los pilares decían 272 € y la tarjeta de «frente a tu media», 1.200:
        dos números para el mismo mes en la misma pantalla."""
        pago = self.mov('-1200', 12, self.mantenimiento)
        self.emparejar(self.mov('928', 10, concepto='Hucha'), pago)

        comparativa = self.panel(anio=2026, mes=9)['comparativa']
        self.assertEqual(comparativa['total'], Decimal('272'))

    def test_el_desglose_de_la_categoria_dice_lo_mismo_que_su_pilar(self):
        """Se abre pinchando en la fila: si ahí pone 272 €, dentro no puede
        poner 1.200."""
        pago = self.mov('-1200', 12, self.mantenimiento)
        self.emparejar(self.mov('928', 10, concepto='Hucha'), pago)

        respuesta = self.client.get(
            reverse('extractos:desglose_categoria'),
            {'categoria': self.mantenimiento.id, 'anio': 2026, 'mes': 9},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        # El total del desglose, no el importe del recibo: el banco cobró
        # 1.200 y su fila tiene que seguir diciéndolo.
        self.assertContains(respuesta, '<span class="mc-total">272,00 €</span>')

    def test_la_cabecera_del_mes_suma_lo_mismo_que_el_kpi(self):
        pago = self.mov('-1200', 12, self.mantenimiento)
        self.emparejar(self.mov('928', 10, concepto='Hucha'), pago)

        panel = self.panel(anio=2026, mes=9)
        septiembre = panel['grupos'][0]
        self.assertEqual(septiembre['gastos'], panel['kpi_gastos'])
        self.assertEqual(septiembre['gastos'], Decimal('-272'))

    # ── Lo que se ve en pantalla ─────────────────────────────────────────

    def test_la_fila_ofrece_emparejar_un_ingreso(self):
        self.mov('928', 10, concepto='Traspaso de la hucha')

        respuesta = self.client.get(reverse('extractos:listar'), {'anio': 2026, 'mes': 9})
        self.assertContains(respuesta, 'ext-cubre-boton')
        self.assertContains(respuesta, '— de la reserva —')

    def test_el_pago_cubierto_dice_en_su_fila_lo_que_peso(self):
        pago = self.mov('-1200', 12, self.mantenimiento)
        self.emparejar(self.mov('928', 10, concepto='Hucha'), pago)

        respuesta = self.client.get(reverse('extractos:listar'), {'anio': 2026, 'mes': 9})
        self.assertContains(respuesta, 'ext-badge-reserva')
        self.assertContains(respuesta, 'pesó')
