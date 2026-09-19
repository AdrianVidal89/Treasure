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
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase
from django.urls import reverse

from core.models import Hogar
from finanzas.models import CategoriaGasto, PartidaGasto
from finanzas.parsing import es_excel, leer_tabla
from finanzas.views_gastos import CATEGORIA_TRASPASO, _crear_categorias_predefinidas

from .analisis import analizar_mes
from .categorizacion import categorizar_por_codigo
from .models import Etiqueta, ExtractoBancario, MovimientoBancario, ReglaCategorizacion
from .normalizacion import (
    contiene_patron, es_traspaso_interno, normalizar_comercio, normalizar_texto,
)
from .parser import analizar_extracto
from .views import _importar_analizados, _marcar_duplicados, _panel_context

FIXTURES = Path(__file__).resolve().parent / 'tests_fixtures'


def leer_bytes(nombre, datos):
    """Pasa unos bytes por `leer_tabla` como si fueran un archivo subido: el
    formato se decide por el contenido, pero la extensión del nombre marca si
    se trata como Excel o como CSV."""
    class _Archivo:
        name = nombre

        def read(self):
            return datos

    return leer_tabla(_Archivo())


def leer_fixture(nombre):
    ruta = FIXTURES / nombre
    if ruta.suffix == '.xls':
        return leer_bytes(nombre, ruta.read_bytes())
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

    def test_lee_xls_que_es_xml_de_excel_2003(self):
        # Varias bancas exportan un «.xls» que es SpreadsheetML (XML), no BIFF.
        # Lleva un `<Table>` dentro, así que colaba por el lector de tablas
        # HTML: este no encuentra ni un `<tr>` y devolvía el archivo vacío.
        xml = (
            '<?xml version="1.0"?>'
            '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"'
            ' xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">'
            '<Worksheet ss:Name="Movimientos"><Table>'
            '<Row><Cell><Data ss:Type="String">Fecha</Data></Cell>'
            '<Cell><Data ss:Type="String">Concepto</Data></Cell>'
            '<Cell><Data ss:Type="String">Importe</Data></Cell>'
            '<Cell><Data ss:Type="String">Saldo</Data></Cell></Row>'
            '<Row><Cell><Data ss:Type="DateTime">2026-07-01T00:00:00.000</Data></Cell>'
            '<Cell><Data ss:Type="String">COMPRA MERCADONA</Data></Cell>'
            '<Cell><Data ss:Type="Number">-45.67</Data></Cell>'
            '<Cell><Data ss:Type="Number">1200.5</Data></Cell></Row>'
            # Sin concepto: el banco omite la celda y salta con ss:Index, así
            # que el importe tiene que seguir cayendo en su columna.
            '<Row><Cell><Data ss:Type="DateTime">2026-07-02T00:00:00.000</Data></Cell>'
            '<Cell ss:Index="3"><Data ss:Type="Number">-10</Data></Cell>'
            '<Cell><Data ss:Type="Number">1190.5</Data></Cell></Row>'
            '</Table></Worksheet></Workbook>'
        ).encode('utf-8')

        r = analizar_extracto(leer_bytes('movimientos.xls', xml))
        self.assertFalse(r['errores_generales'])
        self.assertEqual(len(r['movimientos']), 2)
        primero, segundo = r['movimientos']
        self.assertEqual(primero['fecha'], date(2026, 7, 1))
        self.assertEqual(primero['concepto'], 'COMPRA MERCADONA')
        self.assertEqual(primero['importe'], Decimal('-45.67'))
        self.assertEqual(primero['saldo'], Decimal('1200.5'))
        self.assertEqual(segundo['importe'], Decimal('-10'))
        self.assertEqual(segundo['saldo'], Decimal('1190.5'))

    def test_un_xml_con_dtd_se_rechaza_en_vez_de_expandirlo(self):
        # ElementTree expande las entidades internas: sin esta criba, un «.xls»
        # amañado («billion laughs») se come la memoria del servidor.
        bomba = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE lolz [<!ENTITY lol "lol">'
            '<!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>'
            '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet">'
            '<Worksheet><Table><Row><Cell><Data>&lol1;</Data></Cell></Row>'
            '</Table></Worksheet></Workbook>'
        ).encode('utf-8')

        with self.assertRaises(ValueError):
            leer_bytes('bomba.xls', bomba)

    def test_lee_xls_que_es_una_tabla_html(self):
        html = (
            '<html><body><table>'
            '<tr><th>Fecha</th><th>Concepto</th><th>Importe</th></tr>'
            '<tr><td>01/07/2026</td><td>N\u00d3MINA</td><td>1.234,56</td></tr>'
            '</table></body></html>'
        ).encode('utf-8')

        r = analizar_extracto(leer_bytes('movimientos.xls', html))
        self.assertFalse(r['errores_generales'])
        self.assertEqual(len(r['movimientos']), 1)
        self.assertEqual(r['movimientos'][0]['concepto'], 'N\u00d3MINA')
        self.assertEqual(r['movimientos'][0]['importe'], Decimal('1234.56'))


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

    def test_el_gasto_anual_se_compara_contra_el_año(self):
        """Un IBI de 520 € al año se compara con 520 €, no con los 43,33 €/mes
        que se apartan.

        Ese límite mensual no es un tope de agosto: es la doceava parte de lo
        que cuesta el año, y el recibo llega de golpe. Comparar el pago contra
        él solo podía decir que te habías pasado el mes en que se paga."""
        self.declarar('IBI', '520', 'anual')
        self.gasto('IBI', '-100')

        self.assertEqual(self.fuera()['categorias'], [])

        # Pasarse del año sí se ve.
        self.gasto('IBI', '-500', concepto='Ayuntamiento otra vez')
        fuera = self.fuera()['categorias']
        self.assertEqual(fuera[0]['nombre'], 'IBI')
        self.assertEqual(fuera[0]['limite'], Decimal('520.00'))
        self.assertEqual(fuera[0]['exceso'], Decimal('80.00'))

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
        """El bloque tiene límite en la vista mensual: se quitaba junto con el
        pago y salía «sin límite», cuando tiene uno bien definido.

        Y ese límite es el del AÑO —los 164 €/mes apartados son su doceava
        parte—, porque contra el mes el recibo siempre saldría en rojo."""
        # Un gasto del bloque anual SIN marcar como pago de provisión.
        self.mov('-200', 8, self.mantenimiento)

        bloques = {b['tipo']: b for b in self.panel(anio=2026, mes=9)['bloques']}
        self.assertEqual(bloques['anual']['limite'], Decimal('1968.00'))
        self.assertTrue(bloques['anual']['dentro'])   # 200 de 1.968 al año

    def test_pasarse_del_presupuesto_del_año_sigue_saltando(self):
        self.mov('-2000', 8, self.mantenimiento)

        bloques = {b['tipo']: b for b in self.panel(anio=2026, mes=9)['bloques']}
        self.assertFalse(bloques['anual']['dentro'])
        self.assertEqual(bloques['anual']['exceso'], Decimal('32.00'))

    def test_el_pago_marcado_sale_del_gasto_pero_el_limite_sigue(self):
        self.mov('-1249.34', 9, self.mantenimiento, provision=self.revision)
        self.mov('-50', 9, self.mantenimiento)

        panel = self.panel(anio=2026, mes=9)
        bloques = {b['tipo']: b for b in panel['bloques']}
        # Solo cuentan los 50 € no marcados, contra los 1.968 € del año.
        self.assertEqual(bloques['anual']['importe'], Decimal('50'))
        self.assertEqual(bloques['anual']['limite'], Decimal('1968.00'))
        self.assertTrue(bloques['anual']['dentro'])

class RepartoAprendidoTests(TestCase):
    """Repartir a mano el recibo del taller cada vez que llega es el trabajo que
    hace que la pantalla se abandone a medias.

    Un reparto se puede llevar al resto de recibos del mismo comercio y quedarse
    como regla, igual que una categoría. Va en PROPORCIONES porque los importes
    no se repiten: la revisión de este año no cuesta la del anterior.
    """

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
        self.mantenimiento = CategoriaGasto.objects.get(
            hogar=self.hogar, nombre='Mantenimiento vehicular')
        self.itv = CategoriaGasto.objects.get(hogar=self.hogar, nombre='ITV')
        self.coche = Vehiculo.objects.create(hogar=self.hogar, nombre='Polo', tipo='coche')

    def mov(self, importe, mes=9, concepto='Norauto', dia=5, vehiculo=None):
        return MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(2026, mes, dia),
            concepto=concepto, importe=Decimal(importe), vehiculo=vehiculo,
        )

    def dividir(self, mov, partes):
        """partes: lista de (importe, categoria, concepto)."""
        datos = {'importe': [], 'categoria_id': [], 'concepto': []}
        for importe, categoria, concepto in partes:
            datos['importe'].append(importe)
            datos['categoria_id'].append(str(categoria.id) if categoria else '')
            datos['concepto'].append(concepto)
        return self.client.post(
            reverse('extractos:dividir_movimiento', args=[mov.id]), datos,
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        ).json()

    def aprender(self, modelo, **extra):
        datos = {'modelo': modelo.id, 'patron': 'norauto'}
        datos.update(extra)
        return self.client.post(
            reverse('extractos:aprender_division'), datos,
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        ).json()

    def partes_de(self, mov):
        return [(p.importe, p.categoria.nombre if p.categoria else None)
                for p in mov.partes.order_by('orden_parte')]

    # ── Ofrecerlo ────────────────────────────────────────────────────────

    def test_al_repartir_dice_cuantos_recibos_parecidos_hay(self):
        modelo = self.mov('-800')
        self.mov('-500', mes=5)
        self.mov('-300', mes=9, dia=20)

        sug = self.dividir(modelo, [
            ('-600', self.mantenimiento, 'Neumáticos'),
            ('-200', self.itv, 'ITV'),
        ])['sugerencia']

        self.assertEqual(sug['patron'], 'norauto')
        self.assertEqual(sug['n_similares'], 2)
        self.assertEqual(sug['n_mes'], 1)          # solo el de septiembre
        self.assertEqual(sug['pesos'], [75.0, 25.0])
        self.assertFalse(sug['ya_hay_regla'])

    def test_los_ya_repartidos_se_cuentan_aparte(self):
        """Entran en la cuenta —son los que hay que corregir cuando el reparto
        estaba mal— pero se dicen aparte, para que quede claro que aceptar
        significa volver a repartirlos."""
        otro = self.mov('-500', mes=5)
        self.dividir(otro, [('-400', self.mantenimiento, ''), ('-100', self.itv, '')])

        modelo = self.mov('-800')
        sug = self.dividir(modelo, [
            ('-600', self.mantenimiento, ''), ('-200', self.itv, ''),
        ])['sugerencia']
        self.assertEqual(sug['n_similares'], 1)
        self.assertEqual(sug['n_repartidos'], 1)

    def test_la_importacion_nunca_pisa_un_reparto_hecho(self):
        """Propagar a mano sí alcanza a lo ya repartido, porque lo pide quien
        está corrigiendo. Una importación, no: ahí nadie ha dicho nada."""
        from extractos.models import ReglaDivision

        modelo = self.mov('-800')
        self.dividir(modelo, [('-600', self.mantenimiento, ''), ('-200', self.itv, '')])
        self.aprender(modelo, accion='solo_regla', ambito='todos')
        self.assertTrue(ReglaDivision.objects.filter(hogar=self.hogar).exists())

        a_mano = self.mov('-500', mes=5)
        self.dividir(a_mano, [('-250', self.itv, 'mitad'), ('-250', self.itv, 'mitad')])

        _importar_analizados(self.hogar, self.user, 'Banco', None, [{
            'nombre': 'x.csv',
            'resultado': {'movimientos': [
                {'fecha': '2026-07-02', 'concepto': 'Norauto', 'concepto_raw': '',
                 'importe': Decimal('-900'), 'saldo': None},
            ], 'filas_error': [], 'filas_omitidas': []},
        }])

        # El repartido a mano sigue con SU reparto, no con el de la regla.
        self.assertEqual(
            [p.importe for p in a_mano.partes.order_by('orden_parte')],
            [Decimal('-250.00'), Decimal('-250.00')],
        )

    # ── Aplicarlo ────────────────────────────────────────────────────────

    def test_reparte_los_parecidos_en_la_misma_proporcion(self):
        modelo = self.mov('-800')
        otro = self.mov('-500', mes=5)
        self.dividir(modelo, [
            ('-600', self.mantenimiento, 'Neumáticos'),
            ('-200', self.itv, 'ITV'),
        ])

        self.assertEqual(self.aprender(modelo, ambito='todos')['aplicados'], 1)
        self.assertEqual(self.partes_de(otro), [
            (Decimal('-375.00'), 'Mantenimiento vehicular'),   # 75 % de 500
            (Decimal('-125.00'), 'ITV'),                       # 25 % de 500
        ])

    def test_las_partes_siempre_suman_el_cobro(self):
        """Tres tercios de 100 € dan 33,33 tres veces —99,99— y el reparto se
        rechazaría por no cuadrar. El último absorbe el resto."""
        modelo = self.mov('-900')
        otro = self.mov('-100', mes=5)
        self.dividir(modelo, [
            ('-300', self.mantenimiento, ''), ('-300', self.itv, ''),
            ('-300', self.mantenimiento, ''),
        ])
        self.aprender(modelo, ambito='todos')

        importes = [p.importe for p in otro.partes.order_by('orden_parte')]
        self.assertEqual(importes, [Decimal('-33.33'), Decimal('-33.33'), Decimal('-33.34')])
        self.assertEqual(sum(importes), otro.importe)

    def test_el_alcance_puede_acotarse_a_un_mes(self):
        modelo = self.mov('-800')
        del_mes = self.mov('-400', mes=9, dia=20)
        de_otro_mes = self.mov('-400', mes=5)
        self.dividir(modelo, [('-600', self.mantenimiento, ''), ('-200', self.itv, '')])

        self.assertEqual(
            self.aprender(modelo, ambito='mes', anio=2026, mes=9)['aplicados'], 1,
        )
        self.assertEqual(del_mes.partes.count(), 2)
        self.assertEqual(de_otro_mes.partes.count(), 0)

    def test_un_cobro_demasiado_pequeño_no_se_reparte(self):
        """Repartir 1 céntimo en tres dejaría partes a cero: movimientos
        fantasma que no suman nada y confunden al leer la lista."""
        modelo = self.mov('-900')
        calderilla = self.mov('-0.01', mes=5)
        self.dividir(modelo, [
            ('-300', self.mantenimiento, ''), ('-300', self.itv, ''),
            ('-300', self.mantenimiento, ''),
        ])

        self.assertEqual(self.aprender(modelo, ambito='todos')['aplicados'], 0)
        self.assertEqual(calderilla.partes.count(), 0)

    def test_las_partes_heredan_el_vehiculo_del_cobro(self):
        """El padre deja de contar al repartirse: sin heredar el activo, el
        gasto desaparecía de la ficha del coche."""
        modelo = self.mov('-800', vehiculo=self.coche)
        otro = self.mov('-400', mes=5, vehiculo=self.coche)
        self.dividir(modelo, [('-600', self.mantenimiento, ''), ('-200', self.itv, '')])
        self.aprender(modelo, ambito='todos')

        self.assertEqual(
            [p.vehiculo_id for p in otro.partes.order_by('orden_parte')],
            [self.coche.id, self.coche.id],
        )

    # ── Recordarlo ───────────────────────────────────────────────────────

    def test_recordarlo_guarda_la_regla_con_sus_proporciones(self):
        from extractos.models import ReglaDivision

        modelo = self.mov('-800')
        self.dividir(modelo, [
            ('-600', self.mantenimiento, 'Neumáticos'),
            ('-200', self.itv, 'ITV'),
        ])
        self.assertTrue(self.aprender(modelo, accion='solo_regla')['recordada'])

        regla = ReglaDivision.objects.get(hogar=self.hogar, patron='norauto')
        partes = list(regla.partes.all())
        self.assertEqual([p.porcentaje for p in partes], [75.0, 25.0])
        self.assertEqual(
            [p.categoria.nombre for p in partes], ['Mantenimiento vehicular', 'ITV'],
        )
        self.assertEqual([p.concepto for p in partes], ['Neumáticos', 'ITV'])

    def test_solo_recordar_no_toca_ningun_movimiento(self):
        modelo = self.mov('-800')
        otro = self.mov('-500', mes=5)
        self.dividir(modelo, [('-600', self.mantenimiento, ''), ('-200', self.itv, '')])

        self.assertEqual(self.aprender(modelo, accion='solo_regla')['aplicados'], 0)
        self.assertEqual(otro.partes.count(), 0)

    def test_volver_a_aprender_reemplaza_las_partes_viejas(self):
        """Media regla vieja mezclada con media nueva no es lo que pidió nadie."""
        from extractos.models import ReglaDivision

        modelo = self.mov('-900')
        self.dividir(modelo, [('-600', self.mantenimiento, ''), ('-300', self.itv, '')])
        self.aprender(modelo, accion='solo_regla')

        self.dividir(modelo, [
            ('-300', self.mantenimiento, ''), ('-300', self.itv, ''),
            ('-300', self.mantenimiento, ''),
        ])
        self.aprender(modelo, accion='solo_regla')

        regla = ReglaDivision.objects.get(hogar=self.hogar, patron='norauto')
        self.assertEqual(regla.partes.count(), 3)
        self.assertEqual(ReglaDivision.objects.filter(hogar=self.hogar).count(), 1)

    def test_si_la_regla_ya_hace_esto_no_se_vuelve_a_ofrecer(self):
        """Y basta con que una regla lo CUBRA: «norauto» ya vale para
        «norauto sevilla», que es como se aplica luego en la importación."""
        modelo = self.mov('-800')
        self.dividir(modelo, [('-600', self.mantenimiento, ''), ('-200', self.itv, '')])
        self.aprender(modelo, accion='solo_regla', ambito='todos')

        otro = self.mov('-400', mes=5, concepto='Norauto Sevilla Nervion')
        sug = self.dividir(otro, [
            ('-300', self.mantenimiento, ''), ('-100', self.itv, ''),
        ])['sugerencia']
        self.assertTrue(sug['ya_hay_regla'])
        self.assertFalse(sug['regla_desfasada'])

    def test_una_regla_fechada_no_alcanza_a_los_recibos_anteriores(self):
        """«Solo este» fecha la regla en ese recibo: lo anterior no era así y no
        se toca, que es lo que se pide al fecharla."""
        modelo = self.mov('-800', mes=9)
        self.dividir(modelo, [('-600', self.mantenimiento, ''), ('-200', self.itv, '')])
        self.aprender(modelo, accion='solo_regla')      # sin ámbito: desde este

        anterior = self.mov('-400', mes=5)
        sug = self.dividir(anterior, [
            ('-300', self.mantenimiento, ''), ('-100', self.itv, ''),
        ])['sugerencia']
        self.assertFalse(sug['ya_hay_regla'])

    def test_la_pantalla_de_reglas_ensena_los_repartos(self):
        modelo = self.mov('-800')
        self.dividir(modelo, [
            ('-600', self.mantenimiento, 'Neumáticos'), ('-200', self.itv, 'ITV'),
        ])
        self.aprender(modelo, accion='solo_regla')

        respuesta = self.client.get(reverse('extractos:reglas'))
        self.assertContains(respuesta, 'Repartos aprendidos')
        self.assertContains(respuesta, 'norauto')
        self.assertContains(respuesta, '75,0 %')

    def test_se_puede_borrar_un_reparto_aprendido(self):
        from extractos.models import ReglaDivision

        modelo = self.mov('-800')
        self.dividir(modelo, [('-600', self.mantenimiento, ''), ('-200', self.itv, '')])
        self.aprender(modelo, accion='solo_regla')
        regla = ReglaDivision.objects.get(hogar=self.hogar)

        self.client.post(reverse('extractos:reglas'), {
            'accion': 'eliminar_division', 'regla_id': regla.id,
        })
        self.assertFalse(ReglaDivision.objects.filter(hogar=self.hogar).exists())
        # Lo ya repartido no se toca: deshacerlo es cosa de cada fila.
        self.assertEqual(modelo.partes.count(), 2)


class UnTraspasoQueNoLoEraTests(TestCase):
    """«Transferencia de ADRIAN VIDAL RODRIGUEZ» de +423,68 € que no es un
    traspaso: es tu primo, que se llama igual, devolviéndote algo.

    La importación marca como traspaso lo que habla de transferencia Y menciona
    a alguien del hogar. Acierta casi siempre. Cuando no, no había salida:
    `es_traspaso` mandaba sobre la categoría, así que ponerle «Otros ingresos»
    no hacía nada y quitarle la categoría, tampoco. Y el campo solo se escribía
    al importar: no había ningún sitio donde tocarlo.
    """

    def setUp(self):
        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        self.user = User.objects.create_user(
            username='tester', password='clave-de-prueba',
            first_name='Adrian', last_name='Vidal Rodriguez',
        )
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        self.extracto = ExtractoBancario.objects.create(hogar=self.hogar, usuario=self.user)
        self.client.force_login(self.user)
        self.otros = CategoriaGasto.objects.get(hogar=self.hogar, nombre='Otros ingresos')
        self.traspasos = CategoriaGasto.objects.get(
            hogar=self.hogar, nombre=CATEGORIA_TRASPASO)

    def mov(self, importe='423.68', concepto='Transferencia de ADRIAN VIDAL RODRIGUEZ',
            categoria=None, traspaso=True):
        return MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(2026, 6, 9),
            concepto=concepto, importe=Decimal(importe),
            categoria=categoria, es_traspaso=traspaso,
        )

    def panel(self, **params):
        params.setdefault('anio', 2026)
        params.setdefault('mes', 6)
        return self.client.get(reverse('extractos:listar'), params).context['panel']

    # ── El bug ───────────────────────────────────────────────────────────

    def test_ponerle_una_categoria_de_ingreso_ahora_si_hace_algo(self):
        """Era lo primero que uno intenta, y no servía de nada: la marca de
        traspaso ganaba a la categoría."""
        m = self.mov()
        self.assertFalse(m.cuenta_como_ingreso)

        self.client.post(
            reverse('extractos:actualizar_movimiento', args=[m.id]),
            {'categoria_id': self.otros.id},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        m.refresh_from_db()
        self.assertTrue(m.cuenta_como_ingreso)
        self.assertFalse(m.es_neutro)
        self.assertEqual(self.panel()['kpi_ingresos'], Decimal('423.68'))

    def test_al_declararlo_ingreso_se_le_quita_la_chapa(self):
        """Si no, la fila diría «traspaso» al lado de «Otros ingresos»: dos
        cosas distintas del mismo apunte."""
        m = self.mov()
        self.client.post(
            reverse('extractos:actualizar_movimiento', args=[m.id]),
            {'categoria_id': self.otros.id},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        m.refresh_from_db()
        self.assertFalse(m.es_traspaso)

    def test_se_puede_quitar_la_marca_desde_la_fila(self):
        """Antes no había ningún sitio donde tocarla: solo se escribía al
        importar."""
        m = self.mov()
        respuesta = self.client.post(
            reverse('extractos:marcar_traspaso', args=[m.id]), {'es_traspaso': '0'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertTrue(respuesta.json()['ok'])

        m.refresh_from_db()
        self.assertFalse(m.es_traspaso)
        # Sin categoría, manda el signo: un abono es un ingreso.
        self.assertTrue(m.cuenta_como_ingreso)

    def test_quitar_la_marca_suelta_tambien_la_categoria_de_traspasos(self):
        """Con la categoría de traspasos puesta, quitar la marca no cambiaría
        nada: la categoría es neutra y manda ella."""
        m = self.mov(categoria=self.traspasos)
        self.client.post(
            reverse('extractos:marcar_traspaso', args=[m.id]), {'es_traspaso': '0'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        m.refresh_from_db()
        self.assertIsNone(m.categoria)
        self.assertTrue(m.cuenta_como_ingreso)

    def test_tambien_se_puede_marcar_uno_que_la_importacion_no_pilló(self):
        """Al revés: un traspaso desde un banco que no pone tu nombre entra
        como ingreso e infla el mes."""
        m = self.mov(concepto='Abono desde mi otra cuenta', traspaso=False)
        self.assertTrue(m.cuenta_como_ingreso)

        self.client.post(
            reverse('extractos:marcar_traspaso', args=[m.id]), {'es_traspaso': '1'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        m.refresh_from_db()
        self.assertTrue(m.es_neutro)
        self.assertEqual(self.panel()['kpi_ingresos'], Decimal('0'))

    # ── Lo que NO debe cambiar ───────────────────────────────────────────

    def test_un_traspaso_de_verdad_sigue_siendo_neutro(self):
        """La importación le pone la categoría de traspasos, que es neutra: el
        cambio de orden no puede haberlos soltado a todos."""
        m = self.mov(categoria=self.traspasos)
        self.assertTrue(m.es_neutro)
        self.assertFalse(m.cuenta_como_ingreso)
        self.assertEqual(self.panel()['kpi_ingresos'], Decimal('0'))

    def test_un_traspaso_sin_categoria_sigue_siendo_neutro(self):
        """Si la categoría de traspasos no existiera, la marca sola lo sostiene."""
        self.assertTrue(self.mov().es_neutro)

    def test_la_importacion_los_sigue_detectando(self):
        totales = _importar_analizados(self.hogar, self.user, 'Banco', None, [{
            'nombre': 'x.csv',
            'resultado': {'movimientos': [
                {'fecha': '2026-06-09', 'concepto': 'Transferencia a ADRIAN VIDAL RODRIGUEZ',
                 'concepto_raw': '', 'importe': Decimal('-200'), 'saldo': None},
            ], 'filas_error': [], 'filas_omitidas': []},
        }])
        self.assertEqual(totales['total_traspasos'], 1)
        m = MovimientoBancario.objects.get(hogar=self.hogar, importe=Decimal('-200'))
        self.assertTrue(m.es_traspaso)
        self.assertTrue(m.es_neutro)

    def test_poner_la_categoria_de_traspasos_a_mano_no_quita_la_marca(self):
        """Solo la suelta una categoría que SÍ cuenta: decir «esto es un
        traspaso» no puede desmarcarlo."""
        m = self.mov(traspaso=True)
        self.client.post(
            reverse('extractos:actualizar_movimiento', args=[m.id]),
            {'categoria_id': self.traspasos.id},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        m.refresh_from_db()
        self.assertTrue(m.es_traspaso)
        self.assertTrue(m.es_neutro)

    # ── En bloque ────────────────────────────────────────────────────────

    def test_se_pueden_desmarcar_varios_de_golpe(self):
        """Cuando el banco no pone tu nombre, son todos los de esa cuenta los
        que entran mal, no uno."""
        # Importes distintos: dos apuntes idénticos del banco SÍ son un
        # duplicado, y el hash de deduplicación los rechaza con razón.
        unos = [self.mov(importe=str(100 + n), categoria=self.traspasos) for n in range(3)]

        self.client.post(reverse('extractos:accion_lote'), {
            'accion': 'traspaso', 'es_traspaso': '0',
            'ids': [m.id for m in unos],
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

        for m in unos:
            m.refresh_from_db()
            self.assertFalse(m.es_traspaso)
            self.assertIsNone(m.categoria)
            self.assertTrue(m.cuenta_como_ingreso)

    def test_se_pueden_marcar_varios_de_golpe(self):
        unos = [
            self.mov(importe=str(200 + n), concepto='Abono cuenta propia', traspaso=False)
            for n in range(2)
        ]

        self.client.post(reverse('extractos:accion_lote'), {
            'accion': 'traspaso', 'es_traspaso': '1',
            'ids': [m.id for m in unos],
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

        for m in unos:
            m.refresh_from_db()
            self.assertTrue(m.es_neutro)

    # ── En pantalla ──────────────────────────────────────────────────────

    def test_la_chapa_de_traspaso_es_un_boton(self):
        self.mov(categoria=self.traspasos)
        respuesta = self.client.get(reverse('extractos:listar'), {'anio': 2026, 'mes': 6})
        self.assertContains(respuesta, 'ext-traspaso-boton')
        self.assertContains(respuesta, 'pulsa si no lo es')


class ApuntarAManoTests(TestCase):
    """Lo pagado en efectivo no está en ningún extracto.

    Sin poder apuntarlo, el mes dice que gastaste menos de lo que gastaste: la
    cifra es «lo que movió la cuenta» y no «lo que gastaste», que es la que se
    quiere.
    """

    def setUp(self):
        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        self.user = User.objects.create_user(username='tester', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        self.client.force_login(self.user)
        self.restaurantes = CategoriaGasto.objects.get(
            hogar=self.hogar, nombre='Restaurantes')

    def apuntar(self, **campos):
        datos = {
            'fecha': '2026-09-14', 'concepto': 'Bar Manolo',
            'importe': '12.40', 'tipo': 'gasto',
        }
        datos.update(campos)
        return self.client.post(reverse('extractos:crear_movimiento'), datos, follow=True)

    def ultimo(self):
        return MovimientoBancario.objects.filter(hogar=self.hogar).order_by('-id').first()

    # ── Lo básico ────────────────────────────────────────────────────────

    def test_apunta_un_gasto_en_efectivo(self):
        self.apuntar()
        m = self.ultimo()

        self.assertEqual(m.concepto, 'Bar Manolo')
        self.assertEqual(m.importe, Decimal('-12.40'))     # el signo lo pone «tipo»
        self.assertEqual(m.fecha, date(2026, 9, 14))
        self.assertTrue(m.manual)
        self.assertIsNone(m.extracto)                      # no viene de ningún archivo
        self.assertTrue(m.cuenta_como_gasto)

    def test_un_ingreso_entra_en_positivo(self):
        self.apuntar(tipo='ingreso', concepto='Venta de la bici', importe='80')
        self.assertEqual(self.ultimo().importe, Decimal('80'))

    def test_el_importe_se_pide_en_positivo(self):
        """Escribir el signo a mano es la forma más fácil de meter un ingreso
        donde iba un gasto, y no se ve hasta que los totales no cuadran."""
        self.apuntar(importe='-12.40')
        self.assertIsNone(self.ultimo())

    def test_sin_fecha_concepto_o_importe_no_se_guarda_nada(self):
        for falta in ('fecha', 'concepto', 'importe'):
            with self.subTest(falta=falta):
                self.apuntar(**{falta: ''})
                self.assertIsNone(self.ultimo())

    def test_una_fecha_inventada_no_se_guarda(self):
        self.apuntar(fecha='31/02/2026')
        self.assertIsNone(self.ultimo())

    # ── Dos cafés iguales el mismo día ───────────────────────────────────

    def test_dos_apuntes_identicos_el_mismo_dia_son_dos_apuntes(self):
        """Dos cafés de 1,50 € el martes son dos cafés, no un duplicado. Con el
        hash de deduplicación normal, el segundo chocaba contra el unique del
        hogar y no se podía guardar."""
        self.apuntar(concepto='Café', importe='1.50')
        self.apuntar(concepto='Café', importe='1.50')

        cafes = MovimientoBancario.objects.filter(hogar=self.hogar, concepto='Café')
        self.assertEqual(cafes.count(), 2)
        self.assertNotEqual(*[c.hash_dedupe for c in cafes])

    def test_editarlo_no_le_cambia_la_huella(self):
        """No hay nada contra lo que deduplicar un apunte a mano: su huella es
        suya y no se recalcula al tocarlo."""
        self.apuntar()
        m = self.ultimo()
        huella = m.hash_dedupe

        m.concepto = 'Bar Manolo (cena)'
        m.save()
        m.refresh_from_db()
        self.assertEqual(m.hash_dedupe, huella)

    # ── Se integra con el resto ──────────────────────────────────────────

    def test_la_categoria_elegida_manda(self):
        self.apuntar(categoria_id=self.restaurantes.id)
        m = self.ultimo()
        self.assertEqual(m.categoria, self.restaurantes)
        self.assertEqual(m.estado_categorizacion, 'manual')

    def test_sin_categoria_se_intenta_adivinar(self):
        """El mismo criterio que al importar: meter «Mercadona» a mano tiene que
        acabar donde acaban los demás Mercadona."""
        self.apuntar(concepto='Mercadona centro', categoria_id='')
        m = self.ultimo()
        self.assertEqual(m.categoria.nombre, 'Alimentacion')
        self.assertEqual(m.estado_categorizacion, 'por_codigo')

    def test_una_regla_aprendida_tambien_vale(self):
        ReglaCategorizacion.objects.create(
            hogar=self.hogar, patron='bar manolo', categoria=self.restaurantes,
        )
        self.apuntar(categoria_id='')
        m = self.ultimo()
        self.assertEqual(m.categoria, self.restaurantes)
        self.assertEqual(m.estado_categorizacion, 'por_regla')

    def test_se_puede_imputar_a_un_vehiculo(self):
        from finanzas.models import Vehiculo

        coche = Vehiculo.objects.create(hogar=self.hogar, nombre='Polo', tipo='coche')
        self.apuntar(concepto='Lavadero a mano', activo=coche.clave_activo)
        self.assertEqual(self.ultimo().vehiculo, coche)

    def test_cuenta_en_los_totales_del_mes(self):
        self.apuntar(importe='12.40', categoria_id=self.restaurantes.id)
        panel = self.client.get(
            reverse('extractos:listar'), {'anio': 2026, 'mes': 9},
        ).context['panel']

        self.assertEqual(panel['kpi_gasto_abs'], Decimal('12.40'))
        self.assertEqual(panel['grupos'][0]['gastos'], Decimal('-12.40'))

    def test_no_ensucia_los_totales_de_los_extractos_importados(self):
        """No salió de ningún archivo: sumarlo ahí diría que el banco trajo algo
        que no trajo."""
        extracto = ExtractoBancario.objects.create(hogar=self.hogar, usuario=self.user)
        MovimientoBancario.objects.create(
            extracto=extracto, hogar=self.hogar, fecha=date(2026, 9, 2),
            concepto='Nomina', importe=Decimal('1500'),
        )
        self.apuntar()

        extracto.refresh_from_db()
        self.assertEqual(extracto.total_gastos, 0)
        self.assertEqual(extracto.total_ingresos, Decimal('1500'))

    def test_se_puede_borrar_aunque_no_tenga_extracto(self):
        """Pedirle `num_movimientos` a un extracto que no existe reventaba el
        borrado."""
        self.apuntar()
        m = self.ultimo()

        respuesta = self.client.post(
            reverse('extractos:eliminar_movimiento', args=[m.id]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertTrue(respuesta.json()['ok'])
        self.assertIsNone(self.ultimo())

    def test_se_puede_repartir_como_cualquier_otro(self):
        """Un cobro en efectivo también puede ser varias cosas a la vez."""
        self.apuntar(importe='60')
        m = self.ultimo()
        alimentacion = CategoriaGasto.objects.get(hogar=self.hogar, nombre='Alimentacion')

        self.client.post(reverse('extractos:dividir_movimiento', args=[m.id]), {
            'importe': ['-40', '-20'],
            'categoria_id': [str(self.restaurantes.id), str(alimentacion.id)],
            'concepto': ['Cena', 'Compra'],
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

        self.assertEqual(m.partes.count(), 2)
        self.assertEqual(sum(p.importe for p in m.partes.all()), m.importe)
        # Y las partes heredan el «no viene del banco» de su padre.
        self.assertEqual({p.extracto_id for p in m.partes.all()}, {None})

    def test_la_fila_dice_que_lo_pusiste_tu(self):
        self.apuntar(categoria_id=self.restaurantes.id)
        respuesta = self.client.get(reverse('extractos:listar'), {'anio': 2026, 'mes': 9})
        self.assertContains(respuesta, 'ext-badge-manual')

    def test_la_pantalla_ofrece_apuntarlo(self):
        respuesta = self.client.get(reverse('extractos:listar'))
        self.assertContains(respuesta, 'ext-abrir-manual')
        self.assertContains(respuesta, 'Apuntar un movimiento')


class CuadranLasCuentasConUnRepartoTests(TestCase):
    """Un cobro repartido no puede sumar en ningún sitio. Cuentan sus partes.

    El apunte del banco se queda a la vista —es lo que dice el extracto y lo que
    evita que reimportarlo lo duplique— pero deja de contar: si contara, el mismo
    dinero estaría dos veces. La fila ya lo decía («repartido en 4 · no cuenta»,
    tachada) y aun así la cabecera del mes lo sumaba: 924,81 € donde la suma real
    eran 799,89.

    Este caso son las cifras reales de un septiembre: dos recibos sueltos y un
    cobro agrupado de MyBox repartido en cuatro. Recorre TODAS las pantallas que
    suman, porque el fallo estaba en una sola de ellas y las demás no tenían nada
    que lo impidiera.
    """

    # Los dos recibos sueltos y el cobro agrupado, que suman lo que el banco.
    SUELTOS = (Decimal('-99.24'), Decimal('-515.92'))
    AGRUPADO = Decimal('-184.73')
    PARTES = (Decimal('-52.03'), Decimal('-40.96'), Decimal('-31.93'), Decimal('-59.81'))
    TOTAL = Decimal('799.89')            # 99,24 + 515,92 + 184,73

    def setUp(self):
        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        self.user = User.objects.create_user(username='tester', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        self.extracto = ExtractoBancario.objects.create(hogar=self.hogar, usuario=self.user)
        self.client.force_login(self.user)

        cat = lambda n: CategoriaGasto.objects.get(hogar=self.hogar, nombre=n)
        self.comunidad = cat('Comunidad')
        self.hipoteca = cat('Hipoteca / Alquiler')
        self.seguros = cat('Seguros')
        self.alarmas = CategoriaGasto.objects.create(
            hogar=self.hogar, nombre='Alarmas', tipo='fijo')

        self.mov('CP R2 C.P.MAIRENA', self.SUELTOS[0], 7, self.comunidad)
        self.mov('PRES.32199162107', self.SUELTOS[1], 1, self.hipoteca)
        self.agrupado = self.mov(
            'RECIBO UNICO MYBOX', self.AGRUPADO, 1, self.seguros)

        categorias = [self.alarmas, self.seguros, self.seguros, self.seguros]
        self.client.post(
            reverse('extractos:dividir_movimiento', args=[self.agrupado.id]),
            {
                'importe': [str(i) for i in self.PARTES],
                'categoria_id': [str(c.id) for c in categorias],
                'concepto': ['Alarmas', 'Seguro vida', 'Seguro hogar', 'Asistencia'],
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

    def mov(self, concepto, importe, dia, categoria):
        return MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(2026, 9, dia),
            concepto=concepto, importe=Decimal(importe), categoria=categoria,
        )

    def panel(self, **params):
        params.setdefault('anio', 2026)
        return self.client.get(reverse('extractos:listar'), params).context['panel']

    def test_las_partes_suman_el_cobro(self):
        """La premisa de todo lo demás: si esto no cuadra, el reparto miente."""
        self.assertEqual(sum(self.PARTES), self.AGRUPADO)
        self.assertEqual(self.agrupado.partes.count(), 4)
        self.assertFalse(self.agrupado.cuenta_como_gasto)

    # ── Movimientos ──────────────────────────────────────────────────────

    def test_la_cabecera_del_mes_no_suma_el_cobro_repartido(self):
        """El bug, tal cual: la cabecera decía 924,81 €, que es todo sumado
        incluido el apunte tachado."""
        grupo = self.panel(mes=9)['grupos'][0]

        self.assertEqual(grupo['gastos'], -self.TOTAL)
        self.assertEqual(grupo['neto'], -self.TOTAL)
        self.assertEqual(grupo['ingresos'], Decimal('0'))
        # Y sigue pintándose, que para eso está: es lo que dice el banco.
        self.assertIn(self.agrupado, grupo['movimientos'])

    def test_la_cabecera_cuadra_con_el_kpi_de_arriba(self):
        """Dos cifras de la misma pantalla que no coinciden es lo que hace que
        no te fíes de ninguna."""
        panel = self.panel(mes=9)
        self.assertEqual(panel['kpi_gastos'], panel['grupos'][0]['gastos'])
        self.assertEqual(panel['kpi_gasto_abs'], self.TOTAL)

    def test_el_reparto_por_bloques_suma_lo_mismo(self):
        panel = self.panel(mes=9)
        self.assertEqual(
            sum(b['importe'] for b in panel['bloques']), self.TOTAL,
        )
        self.assertEqual(Decimal(str(panel['donut_total'])), self.TOTAL)

    def test_el_ranking_por_comercio_suma_lo_mismo(self):
        comercios = self.panel(mes=9)['comercios']
        self.assertEqual(
            sum(f['total'] for f in comercios['filas']) + comercios['total_resto'],
            self.TOTAL,
        )

    def test_la_media_del_periodo_sale_del_mismo_gasto(self):
        panel = self.panel(mes=9)
        self.assertEqual(panel['media']['gasto'] * panel['meses_periodo'], self.TOTAL)

    def test_el_desglose_de_una_categoria_no_cuenta_el_padre(self):
        """El modal que se abre desde un bloque: Seguros son las tres partes,
        no las tres partes MÁS el recibo entero."""
        respuesta = self.client.get(
            reverse('extractos:desglose_categoria'),
            {'categoria': self.seguros.id, 'anio': 2026, 'mes': 9},
        )
        esperado = -sum(self.PARTES[1:])        # las tres de Seguros
        self.assertEqual(respuesta.context['total'], esperado)

    # ── Conciliación ─────────────────────────────────────────────────────

    def test_la_conciliacion_cuenta_lo_mismo(self):
        respuesta = self.client.get(
            reverse('extractos:conciliacion'), {'anio': 2026, 'mes': 9},
        )
        observado = sum(
            f['observado'] for b in respuesta.context['bloques'] for f in b['filas']
        )
        self.assertEqual(observado, self.TOTAL)

    # ── Etiquetas ────────────────────────────────────────────────────────

    def test_una_etiqueta_no_cuenta_el_padre_y_sus_partes(self):
        """Etiquetar el recibo entero y sus partes contaba el dinero dos veces."""
        etiqueta = Etiqueta.objects.create(hogar=self.hogar, nombre='Seguros 2026')
        etiqueta.movimientos.add(self.agrupado, *self.agrupado.partes.all())

        fila = self.client.get(reverse('extractos:etiquetas')).context['filas'][0]
        self.assertEqual(fila['gasto'], -self.AGRUPADO)

    # ── El extracto importado ────────────────────────────────────────────

    def test_los_totales_del_extracto_son_los_del_banco(self):
        """Las partes no venían en el archivo: las creó el usuario al repartir."""
        self.extracto.refresh_from_db()
        self.assertEqual(
            self.extracto.total_gastos, sum(self.SUELTOS) + self.AGRUPADO,
        )
        self.assertEqual(self.extracto.saldo_neto, -self.TOTAL)

    # ── La ficha de un activo ────────────────────────────────────────────

    def test_la_ficha_de_una_propiedad_no_cuenta_el_padre(self):
        from finanzas.costes_activo import costes
        from finanzas.models import Propiedad

        casa = Propiedad.objects.create(
            hogar=self.hogar, nombre='Piso', fecha_compra=date(2019, 1, 1),
            precio_compra=Decimal('180000'), valor_actual=Decimal('200000'),
        )
        MovimientoBancario.objects.filter(hogar=self.hogar).update(propiedad=casa)

        f = costes(casa, 2026)
        self.assertEqual(f['real_anual'], self.TOTAL)
        self.assertEqual(
            sum(m['total'] for m in f['por_mes']), self.TOTAL,
        )
        self.assertEqual(
            sum(c['real_anual'] for c in f['por_categoria']), f['devengado_anual'],
        )

    # ── El asistente ─────────────────────────────────────────────────────

    def test_el_asistente_no_propone_categorizar_un_cobro_repartido(self):
        from asistente_ia.herramientas import movimientos_sin_categorizar

        sin_cat = self.mov('Bar Pepe', Decimal('-12'), 8, None)
        self.agrupado.categoria = None
        self.agrupado.save(update_fields=['categoria'])
        for parte in self.agrupado.partes.all():
            parte.categoria = None
            parte.save(update_fields=['categoria'])

        datos = movimientos_sin_categorizar(self.hogar)
        conceptos = {g['concepto'] for g in datos['conceptos']}
        self.assertIn(sin_cat.concepto, conceptos)
        self.assertNotIn(self.agrupado.concepto, conceptos)
        # Las partes sí: son las que hay que nombrar.
        self.assertEqual(datos['total_sin_categorizar'], 1 + len(self.PARTES))


class CorregirUnRepartoTests(TestCase):
    """Corregir un reparto ya hecho, y poder fecharlo.

    El bug: un recibo repartido en cuatro, tres partes al piso y la cuarta al
    coche. Se podía repartir, pero no CORREGIR. Dos causas:

    * los recibos parecidos que ya estaban repartidos no se contaban —y después
      de la primera vez lo están todos—, así que «hay N recibos más» salía cero
      y la corrección se quedaba en ese único apunte;
    * «¿ya hay regla?» solo miraba si existía alguna para el comercio, no si
      seguía haciendo lo mismo, así que tampoco se ofrecía actualizarla.

    Y el encargo: un recibo que cambia de forma —el seguro pasa de tres
    coberturas a cuatro— tiene que poder corregirse de esa fecha en adelante sin
    reescribir lo de antes.
    """

    def setUp(self):
        from finanzas.models import Propiedad, Vehiculo

        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        self.user = User.objects.create_user(username='tester', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        self.extracto = ExtractoBancario.objects.create(hogar=self.hogar, usuario=self.user)
        self.client.force_login(self.user)

        self.luz = CategoriaGasto.objects.get(hogar=self.hogar, nombre='Luz')
        self.agua = CategoriaGasto.objects.get(hogar=self.hogar, nombre='Agua')
        self.comunidad = CategoriaGasto.objects.get(hogar=self.hogar, nombre='Comunidad')
        self.seguro = CategoriaGasto.objects.get(hogar=self.hogar, nombre='Seguro coche')
        self.piso = Propiedad.objects.create(
            hogar=self.hogar, nombre='Piso', fecha_compra=date(2019, 1, 1),
            precio_compra=Decimal('180000'), valor_actual=Decimal('200000'),
        )
        self.coche = Vehiculo.objects.create(hogar=self.hogar, nombre='Polo', tipo='coche')

    def mov(self, importe, mes, dia=10, concepto='Mapfre'):
        return MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(2026, mes, dia),
            concepto=concepto, importe=Decimal(importe), propiedad=self.piso,
        )

    def dividir(self, m, partes):
        """partes: lista de (importe, categoria, concepto, clave_activo)."""
        datos = {'importe': [], 'categoria_id': [], 'concepto': [], 'activo': []}
        for importe, categoria, concepto, activo in partes:
            datos['importe'].append(importe)
            datos['categoria_id'].append(str(categoria.id))
            datos['concepto'].append(concepto)
            datos['activo'].append(activo)
        return self.client.post(
            reverse('extractos:dividir_movimiento', args=[m.id]), datos,
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        ).json()

    def aprender(self, m, **extra):
        datos = {'modelo': m.id, 'patron': 'mapfre'}
        datos.update(extra)
        return self.client.post(
            reverse('extractos:aprender_division'), datos,
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        ).json()

    def de_tres(self):
        return [
            ('-120', self.luz, 'Luz', self.piso.clave_activo),
            ('-100', self.agua, 'Agua', self.piso.clave_activo),
            ('-80', self.comunidad, 'Comunidad', self.piso.clave_activo),
        ]

    def de_cuatro(self):
        return [
            ('-120', self.luz, 'Luz', self.piso.clave_activo),
            ('-100', self.agua, 'Agua', self.piso.clave_activo),
            ('-80', self.comunidad, 'Comunidad', self.piso.clave_activo),
            ('-60', self.seguro, 'Seguro coche', self.coche.clave_activo),
        ]

    def activos_de(self, m):
        return [str(p.activo_imputado) for p in m.partes.order_by('orden_parte')]

    # ── Tres al piso y una al coche ──────────────────────────────────────

    def test_cada_parte_va_a_su_activo(self):
        nov = self.mov('-360', 11)
        self.dividir(nov, self.de_cuatro())

        self.assertEqual(
            self.activos_de(nov), ['Piso (Vivienda)'] * 3 + ['Polo'],
        )

    def test_al_propagarlo_cada_parte_llega_a_su_activo(self):
        marzo = self.mov('-360', 3)
        nov = self.mov('-360', 11)
        self.dividir(nov, self.de_cuatro())
        self.aprender(nov, ambito='todos', recordar='1')

        self.assertEqual(
            self.activos_de(marzo), ['Piso (Vivienda)'] * 3 + ['Polo'],
        )

    # ── El bug: corregir lo ya repartido ─────────────────────────────────

    def test_corregir_un_reparto_hecho_si_se_puede_propagar(self):
        """Antes salía «hay 0 recibos más» porque ya estaban todos repartidos,
        y la corrección se quedaba en el apunte que se tocaba."""
        marzo = self.mov('-300', 3)
        junio = self.mov('-300', 6)
        self.dividir(junio, self.de_tres())
        self.aprender(junio, ambito='todos', recordar='1')

        # Corrijo: la comunidad era en realidad el seguro del coche.
        corregido = self.de_tres()
        corregido[2] = ('-80', self.seguro, 'Seguro coche', self.coche.clave_activo)
        sug = self.dividir(junio, corregido)['sugerencia']

        self.assertEqual(sug['n_similares'], 1)
        self.assertEqual(sug['n_repartidos'], 1)
        self.assertFalse(sug['ya_hay_regla'])
        self.assertTrue(sug['regla_desfasada'])

        datos = self.aprender(junio, ambito='todos', recordar='1')
        self.assertEqual(datos['aplicados'], 1)
        self.assertEqual(datos['rehechos'], 1)     # marzo ya estaba repartido
        self.assertEqual(
            self.activos_de(marzo), ['Piso (Vivienda)', 'Piso (Vivienda)', 'Polo'],
        )

    def test_la_regla_se_actualiza_con_la_correccion(self):
        from extractos.models import ReglaDivision

        junio = self.mov('-300', 6)
        self.dividir(junio, self.de_tres())
        self.aprender(junio, ambito='todos', recordar='1')

        corregido = self.de_tres()
        corregido[2] = ('-80', self.seguro, 'Seguro coche', self.coche.clave_activo)
        self.dividir(junio, corregido)
        self.aprender(junio, accion='solo_regla', ambito='todos')

        regla = ReglaDivision.objects.get(hogar=self.hogar, patron='mapfre', desde=None)
        self.assertEqual(
            [str(p.activo_imputado) for p in regla.partes.all()],
            ['Piso (Vivienda)', 'Piso (Vivienda)', 'Polo'],
        )

    def test_si_la_regla_ya_hace_esto_no_se_pregunta_nada(self):
        """Volver a guardar el MISMO reparto no tiene nada que ofrecer."""
        junio = self.mov('-300', 6)
        self.dividir(junio, self.de_tres())
        self.aprender(junio, ambito='todos', recordar='1')

        sug = self.dividir(junio, self.de_tres())['sugerencia']
        self.assertTrue(sug['ya_hay_regla'])
        self.assertFalse(sug['regla_desfasada'])

    # ── De esta fecha en adelante ────────────────────────────────────────

    def test_corregir_de_una_fecha_en_adelante_no_toca_el_pasado(self):
        """El recibo sube de valor porque se añade una cobertura. Lo de antes
        no era así y tiene que quedarse como estaba."""
        marzo = self.mov('-300', 3)
        octubre = self.mov('-360', 10)
        nov = self.mov('-360', 11)
        self.dividir(marzo, self.de_tres())
        self.aprender(marzo, ambito='todos', recordar='1')
        self.assertEqual(octubre.partes.count(), 3)

        # Ahora el de noviembre lleva cuatro: se corrige de esa fecha en adelante.
        self.dividir(nov, self.de_cuatro())
        datos = self.aprender(nov, ambito='adelante', recordar='1')

        self.assertEqual(datos['desde'], '2026-11-10')
        # Marzo y octubre son anteriores: intactos.
        self.assertEqual(marzo.partes.count(), 3)
        self.assertEqual(octubre.partes.count(), 3)
        self.assertEqual(self.activos_de(marzo), ['Piso (Vivienda)'] * 3)

    def test_de_aqui_en_adelante_si_alcanza_a_los_posteriores(self):
        marzo = self.mov('-300', 3)
        junio = self.mov('-360', 6)
        diciembre = self.mov('-360', 12)
        self.dividir(junio, self.de_cuatro())
        self.aprender(junio, ambito='adelante', recordar='1')

        self.assertEqual(marzo.partes.count(), 0)          # anterior: intacto
        self.assertEqual(diciembre.partes.count(), 4)      # posterior: repartido

    def test_conviven_dos_versiones_del_mismo_comercio(self):
        from extractos.models import ReglaDivision

        junio = self.mov('-300', 6)
        nov = self.mov('-360', 11)
        self.dividir(junio, self.de_tres())
        self.aprender(junio, accion='solo_regla', ambito='todos')
        self.dividir(nov, self.de_cuatro())
        self.aprender(nov, accion='solo_regla', ambito='adelante')

        versiones = ReglaDivision.objects.filter(hogar=self.hogar, patron='mapfre')
        self.assertEqual(versiones.count(), 2)
        self.assertEqual(
            sorted(((v.desde, v.partes.count()) for v in versiones),
                   key=lambda par: par[0] or date.min),
            [(None, 3), (date(2026, 11, 10), 4)],
        )

    def test_al_importar_cada_recibo_usa_la_version_de_su_fecha(self):
        """Lo que de verdad pedía el encargo: de ahí en adelante, sin tocar el
        pasado, también para lo que se importe después."""
        junio = self.mov('-300', 6)
        nov = self.mov('-360', 11)
        self.dividir(junio, self.de_tres())
        self.aprender(junio, accion='solo_regla', ambito='todos')
        self.dividir(nov, self.de_cuatro())
        self.aprender(nov, accion='solo_regla', ambito='adelante')

        _importar_analizados(self.hogar, self.user, 'Banco', None, [{
            'nombre': 'x.csv',
            'resultado': {'movimientos': [
                {'fecha': '2026-05-04', 'concepto': 'MAPFRE recibo', 'concepto_raw': '',
                 'importe': Decimal('-300'), 'saldo': None},
                {'fecha': '2026-12-04', 'concepto': 'MAPFRE recibo', 'concepto_raw': '',
                 'importe': Decimal('-360'), 'saldo': None},
            ], 'filas_error': [], 'filas_omitidas': []},
        }])

        # `dividido_de__isnull` porque las partes comparten la fecha del padre.
        viejo = MovimientoBancario.objects.get(
            hogar=self.hogar, fecha=date(2026, 5, 4), dividido_de__isnull=True)
        nuevo = MovimientoBancario.objects.get(
            hogar=self.hogar, fecha=date(2026, 12, 4), dividido_de__isnull=True)
        self.assertEqual(viejo.partes.count(), 3)
        self.assertEqual(nuevo.partes.count(), 4)
        self.assertEqual(self.activos_de(nuevo)[-1], 'Polo')

    def test_un_recibo_anterior_a_la_primera_version_no_se_reparte(self):
        """Si la única versión empieza en noviembre, un recibo de mayo no tiene
        ninguna que le valga: se queda entero, que es lo que se pidió."""
        nov = self.mov('-360', 11)
        self.dividir(nov, self.de_cuatro())
        self.aprender(nov, accion='solo_regla', ambito='adelante')

        _importar_analizados(self.hogar, self.user, 'Banco', None, [{
            'nombre': 'x.csv',
            'resultado': {'movimientos': [
                {'fecha': '2026-05-04', 'concepto': 'MAPFRE recibo', 'concepto_raw': '',
                 'importe': Decimal('-300'), 'saldo': None},
            ], 'filas_error': [], 'filas_omitidas': []},
        }])
        mayo = MovimientoBancario.objects.get(
            hogar=self.hogar, fecha=date(2026, 5, 4), dividido_de__isnull=True)
        self.assertEqual(mayo.partes.count(), 0)

    def test_se_puede_fechar_aunque_no_haya_recibos_posteriores(self):
        """El caso exacto: el recibo cambia de forma HOY y todavía no ha llegado
        ninguno más. No hay nada que repartir hacia adelante, pero es justo
        cuando hace falta fechar la regla, así que la opción se ofrece igual.

        Sin esto la única salida era «Todos», que reescribe el pasado."""
        marzo = self.mov('-300', 3)
        nov = self.mov('-360', 11)
        self.dividir(marzo, self.de_tres())
        self.aprender(marzo, ambito='todos', recordar='1')

        sug = self.dividir(nov, self.de_cuatro())['sugerencia']
        self.assertEqual(sug['n_adelante'], 0)      # no hay ninguno posterior
        self.assertEqual(sug['n_similares'], 1)     # pero sí uno anterior

        datos = self.aprender(nov, ambito='adelante', recordar='1')
        self.assertEqual(datos['aplicados'], 0)             # no se toca nada
        self.assertEqual(datos['desde'], '2026-11-10')      # y la regla queda fechada
        self.assertEqual(marzo.partes.count(), 3)

    def test_la_pantalla_ofrece_siempre_el_alcance_por_fecha(self):
        """El botón se pinta en el navegador a partir de la sugerencia; lo que
        se comprueba aquí es que el dato para pintarlo viaja siempre."""
        self.mov('-300', 3)
        nov = self.mov('-360', 11)
        sug = self.dividir(nov, self.de_cuatro())['sugerencia']

        self.assertIn('fecha_texto', sug)
        self.assertEqual(sug['fecha_texto'], '10/11/2026')
        self.assertEqual(sug['fecha'], '2026-11-10')

    def test_la_pantalla_de_reglas_ensena_desde_cuando_vale_cada_una(self):
        junio = self.mov('-300', 6)
        nov = self.mov('-360', 11)
        self.dividir(junio, self.de_tres())
        self.aprender(junio, accion='solo_regla', ambito='todos')
        self.dividir(nov, self.de_cuatro())
        self.aprender(nov, accion='solo_regla', ambito='adelante')

        respuesta = self.client.get(reverse('extractos:reglas'))
        self.assertContains(respuesta, 'Vale desde')
        self.assertContains(respuesta, '10/11/2026')
        self.assertContains(respuesta, 'siempre')


class RepartoEnLaImportacionTests(TestCase):
    """Un reparto aprendido se aplica solo al importar, como una categoría.

    Es la mitad que faltaba: sin esto hay que acordarse de entrar a repartir el
    recibo del taller cada vez que llega, que es justo lo que no pasa.
    """

    def setUp(self):
        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        self.user = User.objects.create_user(username='tester', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        self.mantenimiento = CategoriaGasto.objects.get(
            hogar=self.hogar, nombre='Mantenimiento vehicular')
        self.itv = CategoriaGasto.objects.get(hogar=self.hogar, nombre='ITV')

    def regla(self, patron, pesos):
        """pesos: lista de (proporción, categoría)."""
        from extractos.models import ParteDeDivision, ReglaDivision

        regla = ReglaDivision.objects.create(hogar=self.hogar, patron=patron)
        for i, (proporcion, categoria) in enumerate(pesos, start=1):
            ParteDeDivision.objects.create(
                regla=regla, orden=i, proporcion=Decimal(proporcion), categoria=categoria,
            )
        return regla

    def importar(self, filas):
        analizado = {
            'nombre': 'extracto.csv',
            'resultado': {
                'movimientos': [{
                    'fecha': f'2026-08-{dia:02d}', 'concepto': concepto,
                    'concepto_raw': concepto, 'importe': Decimal(importe), 'saldo': None,
                } for dia, concepto, importe in filas],
                'filas_error': [], 'filas_omitidas': [],
            },
        }
        return _importar_analizados(self.hogar, self.user, 'Banco', None, [analizado])

    def partes_de(self, concepto):
        padre = MovimientoBancario.objects.get(
            hogar=self.hogar, concepto=concepto, dividido_de__isnull=True,
        )
        return [(p.importe, p.categoria.nombre if p.categoria else None)
                for p in padre.partes.order_by('orden_parte')]

    def test_un_recibo_conocido_llega_ya_repartido(self):
        self.regla('norauto', [('0.75', self.mantenimiento), ('0.25', self.itv)])
        totales = self.importar([(5, 'Norauto Sevilla', '-800')])

        self.assertEqual(totales['total_divididos'], 1)
        self.assertEqual(self.partes_de('Norauto Sevilla'), [
            (Decimal('-600.00'), 'Mantenimiento vehicular'),
            (Decimal('-200.00'), 'ITV'),
        ])

    def test_las_partes_no_cuentan_como_movimientos_importados(self):
        """No vienen del banco: salen de un criterio que puso el usuario, y
        sumarlas diría que el extracto traía el triple de apuntes."""
        self.regla('norauto', [('0.75', self.mantenimiento), ('0.25', self.itv)])
        totales = self.importar([(5, 'Norauto', '-800'), (6, 'Mercadona', '-50')])

        self.assertEqual(totales['total_creados'], 2)
        self.assertEqual(totales['total_divididos'], 1)

    def test_lo_que_no_encaja_se_queda_entero(self):
        self.regla('norauto', [('0.75', self.mantenimiento), ('0.25', self.itv)])
        totales = self.importar([(6, 'Mercadona', '-50')])

        self.assertEqual(totales['total_divididos'], 0)
        mov = MovimientoBancario.objects.get(hogar=self.hogar, concepto='Mercadona')
        self.assertEqual(mov.partes.count(), 0)

    def test_gana_el_patron_mas_especifico(self):
        """Igual que en las reglas de categoría: si hay una para «norauto» y
        otra para «norauto sevilla», manda la segunda."""
        self.regla('norauto', [('0.50', self.mantenimiento), ('0.50', self.itv)])
        self.regla('norauto sevilla', [('0.90', self.mantenimiento), ('0.10', self.itv)])
        self.importar([(5, 'Norauto Sevilla', '-1000')])

        self.assertEqual(self.partes_de('Norauto Sevilla'), [
            (Decimal('-900.00'), 'Mantenimiento vehicular'),
            (Decimal('-100.00'), 'ITV'),
        ])

    def test_una_regla_desactivada_no_reparte(self):
        regla = self.regla('norauto', [('0.75', self.mantenimiento), ('0.25', self.itv)])
        regla.activo = False
        regla.save(update_fields=['activo'])

        self.assertEqual(self.importar([(5, 'Norauto', '-800')])['total_divididos'], 0)

    def test_el_total_del_extracto_no_cambia_al_repartir(self):
        """Lo peor que puede pasar es que la forma del reparto no acierte: el
        dinero es siempre el del banco."""
        self.regla('norauto', [('0.6666', self.mantenimiento), ('0.3334', self.itv)])
        self.importar([(5, 'Norauto', '-733.27')])

        padre = MovimientoBancario.objects.get(hogar=self.hogar, concepto='Norauto')
        self.assertEqual(
            sum(p.importe for p in padre.partes.all()), Decimal('-733.27'),
        )
        # Y el padre deja de contar, para no sumar el mismo dinero dos veces.
        self.assertFalse(padre.cuenta_como_gasto)

    def test_cuenta_las_veces_que_se_ha_aplicado(self):
        regla = self.regla('norauto', [('0.75', self.mantenimiento), ('0.25', self.itv)])
        self.importar([(5, 'Norauto', '-800'), (9, 'Norauto Nervion', '-400')])

        regla.refresh_from_db()
        self.assertEqual(regla.veces_aplicada, 2)

    def test_el_aviso_de_la_importacion_lo_cuenta(self):
        self.regla('norauto', [('0.75', self.mantenimiento), ('0.25', self.itv)])
        totales = self.importar([(5, 'Norauto', '-800')])
        self.assertEqual(totales['total_divididos'], 1)


class PreguntarLaReglaSiempreTests(TestCase):
    """Cambiar una categoría a mano y que no se ofrezca recordarla es perder el
    único momento en el que el usuario tiene el criterio en la cabeza.

    Este test recorre las formas de cambiar una categoría que hay en la
    pantalla, para que ninguna se quede sin preguntar otra vez.
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
        self.restaurantes = CategoriaGasto.objects.get(
            hogar=self.hogar, nombre='Restaurantes')

    def mov(self, concepto, importe='-30', dia=5):
        return MovimientoBancario.objects.create(
            extracto=self.extracto, hogar=self.hogar, fecha=date(2026, 9, dia),
            concepto=concepto, importe=Decimal(importe),
        )

    def test_en_la_fila_suelta_se_ofrece(self):
        self.mov('Taberna Pepe', dia=4)
        mov = self.mov('Taberna Pepe', dia=9)

        datos = self.client.post(
            reverse('extractos:actualizar_movimiento', args=[mov.id]),
            {'categoria_id': self.restaurantes.id},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        ).json()
        self.assertEqual(datos['sugerencia']['n_similares'], 1)

    def test_en_el_cambio_en_bloque_tambien(self):
        """Era el único que no preguntaba, y es el gesto con MÁS criterio
        detrás: veinte apuntes marcados a conciencia."""
        uno = self.mov('Taberna Pepe')
        dos = self.mov('Kebab Estambul', dia=7)

        datos = self.client.post(reverse('extractos:accion_lote'), {
            'accion': 'categoria', 'categoria_id': self.restaurantes.id,
            'ids': [uno.id, dos.id],
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest').json()

        # Una regla por COMERCIO, no por apunte ni una sola para todos.
        self.assertEqual(
            sorted(datos['sugerencia']['patrones']), ['kebab estambul', 'taberna pepe'],
        )
        self.assertEqual(datos['sugerencia']['categoria'], 'Restaurantes')

    def test_el_bloque_no_ofrece_lo_que_ya_es_regla(self):
        ReglaCategorizacion.objects.create(
            hogar=self.hogar, patron='taberna pepe', categoria=self.restaurantes,
        )
        uno = self.mov('Taberna Pepe')
        dos = self.mov('Kebab Estambul', dia=7)

        datos = self.client.post(reverse('extractos:accion_lote'), {
            'accion': 'categoria', 'categoria_id': self.restaurantes.id,
            'ids': [uno.id, dos.id],
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest').json()
        self.assertEqual(datos['sugerencia']['patrones'], ['kebab estambul'])

    def test_quitar_la_categoria_en_bloque_no_pregunta_nada(self):
        """No hay criterio que recordar: dejar algo en blanco no es un criterio."""
        uno = self.mov('Taberna Pepe')

        datos = self.client.post(reverse('extractos:accion_lote'), {
            'accion': 'categoria', 'categoria_id': '', 'ids': [uno.id],
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest').json()
        self.assertIsNone(datos.get('sugerencia'))

    def test_el_si_del_bloque_crea_una_regla_por_comercio(self):
        uno = self.mov('Taberna Pepe')
        dos = self.mov('Kebab Estambul', dia=7)
        self.client.post(reverse('extractos:accion_lote'), {
            'accion': 'categoria', 'categoria_id': self.restaurantes.id,
            'ids': [uno.id, dos.id],
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

        self.client.post(reverse('extractos:aprender_regla'), {
            'patron': ['taberna pepe', 'kebab estambul'],
            'categoria_id': self.restaurantes.id, 'accion': 'solo_regla',
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

        self.assertEqual(
            sorted(ReglaCategorizacion.objects.filter(
                hogar=self.hogar, categoria=self.restaurantes,
            ).values_list('patron', flat=True)),
            ['kebab estambul', 'taberna pepe'],
        )

    def test_al_repartir_un_cobro_tambien_se_ofrece(self):
        """La división era el otro sitio donde se pone criterio a mano y no se
        preguntaba nada."""
        self.mov('Norauto', '-400', dia=4)
        mov = self.mov('Norauto', '-800', dia=9)

        datos = self.client.post(
            reverse('extractos:dividir_movimiento', args=[mov.id]),
            {
                'importe': ['-600', '-200'],
                'categoria_id': [str(self.restaurantes.id), ''],
                'concepto': ['', ''],
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        ).json()
        self.assertEqual(datos['sugerencia']['n_similares'], 1)
        self.assertFalse(datos['sugerencia']['ya_hay_regla'])


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
        datos = respuesta.json()
        self.assertTrue(datos['ok'])
        self.assertEqual(datos['partes'], 2)

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
        datos = respuesta.json()
        self.assertTrue(datos['ok'])
        self.assertEqual(datos['partes'], 3)

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
        self.alimentacion = CategoriaGasto.objects.get(
            hogar=self.hogar, nombre='Alimentacion')
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
        anuales y lo despliegue, me salga reflejado ahí que me excedí».

        Lo que carga el mes son los 272 que la hucha no cubrió. El pago entero
        no desaparece: se guarda al lado para poder contarlo."""
        pago = self.mov('-1200', 12, self.mantenimiento, provision=self.revision)
        self.emparejar(self.mov('928', 10, concepto='De la reserva'), pago)

        panel = self.panel(anio=2026, mes=9)
        bloques = {b['tipo']: b for b in panel['bloques']}
        self.assertEqual(bloques['anual']['importe'], Decimal('272'))
        self.assertEqual(bloques['anual']['bruto'], Decimal('1200'))
        self.assertEqual(bloques['anual']['cubierto'], Decimal('928'))
        self.assertEqual(panel['kpi_gastos'], Decimal('-272'))
        self.assertEqual(panel['cubierto_reserva'], Decimal('928'))

    def test_los_anuales_se_miden_contra_el_presupuesto_del_año(self):
        """«Fijos anuales no puede tener ahí una asignación de 253 €: es más
        claro decir del presupuesto anual X, este mes has pagado Y.»

        Los 100 €/mes que apartas para la revisión no son un tope de
        septiembre: son la doceava parte de los 1.200 que te va a costar el
        año. Contra el mes, el pago solo podía salir en rojo siempre."""
        pago = self.mov('-1200', 12, self.mantenimiento, provision=self.revision)
        self.emparejar(self.mov('928', 10, concepto='De la reserva'), pago)

        bloques = {b['tipo']: b for b in self.panel(anio=2026, mes=9)['bloques']}
        anual = bloques['anual']
        self.assertTrue(anual['es_anual'])
        self.assertEqual(anual['limite'], Decimal('1200'))
        # Y lo que consume esa provisión es el recibo entero, lo pagues con la
        # hucha o no: 1.200 de 1.200.
        self.assertEqual(anual['medido'], Decimal('1200'))
        self.assertTrue(anual['dentro'])

    def test_la_barra_de_un_bloque_separa_lo_que_puso_la_reserva(self):
        """«No hay ninguna indicación ahí al respecto»: la fila decía 1 € y no
        se veía ni el coste ni de dónde había salido."""
        pago = self.mov('-1200', 12, self.mantenimiento, provision=self.revision)
        self.emparejar(self.mov('928', 10, concepto='De la reserva'), pago)

        anual = {b['tipo']: b for b in self.panel(anio=2026, mes=9)['bloques']}['anual']
        # 272 de 1.200 cargan el mes; 928 más los puso la hucha. Entre los dos
        # tramos, la barra se llena entera.
        self.assertEqual(anual['pct_barra'], 22.7)
        self.assertEqual(anual['pct_cubierto'], 77.3)

    def test_la_categoria_tambien_lleva_su_pago_y_su_reserva(self):
        pago = self.mov('-1200', 12, self.mantenimiento, provision=self.revision)
        self.emparejar(self.mov('928', 10, concepto='De la reserva'), pago)

        anual = {b['tipo']: b for b in self.panel(anio=2026, mes=9)['bloques']}['anual']
        fila = {c['nombre']: c for c in anual['categorias']}['Mantenimiento vehicular']
        self.assertEqual(fila['importe'], Decimal('272'))
        self.assertEqual(fila['bruto'], Decimal('1200'))
        self.assertEqual(fila['cubierto'], Decimal('928'))
        self.assertEqual(fila['limite'], Decimal('1200'))

    def test_la_pantalla_cuenta_lo_que_puso_la_reserva_en_el_bloque(self):
        pago = self.mov('-1200', 12, self.mantenimiento, provision=self.revision)
        self.emparejar(self.mov('928', 10, concepto='De la reserva'), pago)

        respuesta = self.client.get(reverse('extractos:listar'), {'anio': 2026, 'mes': 9})
        self.assertContains(respuesta, 'los puso la reserva')
        self.assertContains(respuesta, 'que provisionas')

    def test_el_rayado_se_ve_tambien_sin_presupuesto_declarado(self):
        """«El rayado indica lo que cubrió una reserva, sin embargo no aparece
        reflejado: sí en texto, no en gráfica.»

        Sin límite no hay contra qué medir, pero la barra sigue teniendo algo
        que contar: qué parte del pago puso la reserva. Repartida sobre el pago
        se ve, y antes salía un rayado del 0%."""
        pago = self.mov('-1000', 12, self.alimentacion, concepto='Super')
        self.emparejar(self.mov('904', 10, concepto='De la reserva'), pago)

        bloques = {b['tipo']: b for b in self.panel(anio=2026, mes=9)['bloques']}
        variable = bloques['variable']
        self.assertEqual(variable['limite'], Decimal('0'))     # nada declarado
        self.assertEqual(variable['cubierto'], Decimal('904'))
        self.assertEqual(variable['pct_cubierto'], 90.4)
        self.assertEqual(variable['pct_barra'], 9.6)
        # Y entre los dos tramos la barra se llena entera, como sin reserva.
        self.assertEqual(variable['pct_barra'] + variable['pct_cubierto'], 100)

    def test_el_tramo_de_la_reserva_no_se_pinta_como_gasto(self):
        """Iba escrito antes que `.dentro i` en la hoja de estilos, con la misma
        especificidad, así que el verde lo pisaba y el rayado no se veía."""
        pago = self.mov('-1200', 12, self.mantenimiento, provision=self.revision)
        self.emparejar(self.mov('928', 10, concepto='De la reserva'), pago)

        css = self.client.get(reverse('extractos:listar'), {'anio': 2026, 'mes': 9})
        css = css.content.decode()
        rayado = css.index('i.de-reserva')
        self.assertLess(css.index('.ext-pilar-barra.dentro i'), rayado)
        self.assertLess(css.index('.ext-pilar-barra.fuera i'), rayado)

    def test_el_gasto_de_la_cabecera_y_el_del_reparto_son_el_mismo_numero(self):
        """Se veían tres cifras —balance -1.390,89, gastos -1.409,13 y un
        reparto de 1.409— que parecían tres cosas distintas cuando son dos."""
        self.mov('-1000', 12, self.alimentacion, concepto='Super')
        self.mov('18.24', 13, concepto='Devolución', categoria=CategoriaGasto.objects.get(
            hogar=self.hogar, nombre='Otros ingresos'))

        panel = self.panel(anio=2026, mes=9)
        self.assertEqual(panel['kpi_gasto_abs'], -panel['kpi_gastos'])
        self.assertEqual(
            panel['kpi_neto'], panel['kpi_ingresos'] - panel['kpi_gasto_abs'],
        )

        # Y la resta se lee en pantalla, que es lo que faltaba.
        respuesta = self.client.get(reverse('extractos:listar'), {'anio': 2026, 'mes': 9})
        self.assertContains(respuesta, 'de ingresos')
        self.assertContains(respuesta, 'el gasto de arriba, repartido')

    def test_un_bloque_normal_no_se_mide_contra_el_año(self):
        """Solo los anuales cambian de unidad: alimentación se sigue juzgando
        contra su límite del mes."""
        PartidaGasto.objects.create(
            hogar=self.hogar, categoria=self.alimentacion, nombre='Compra',
            importe=Decimal('400'), periodicidad='mensual',
        )
        self.mov('-500', 5, self.alimentacion, concepto='Super')

        bloques = {b['tipo']: b for b in self.panel(anio=2026, mes=9)['bloques']}
        self.assertFalse(bloques['variable']['es_anual'])
        self.assertEqual(bloques['variable']['limite'], Decimal('400'))
        self.assertFalse(bloques['variable']['dentro'])

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
            reverse('extractos:candidatos_reserva'), {'mov': reposicion.id},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        ).json()
        ids = {p['id'] for p in datos['candidatos']}
        self.assertIn(cercano.id, ids)
        self.assertNotIn(lejano.id, ids)
        self.assertNotIn(reposicion.id, ids)

    def test_el_candidato_dice_cuanto_le_falta_por_cubrir(self):
        pago = self.mov('-1200', 12, self.mantenimiento)
        self.emparejar(self.mov('400', 9, concepto='Primera hucha'), pago)
        otra = self.mov('528', 11, concepto='Segunda hucha')

        datos = self.client.get(
            reverse('extractos:candidatos_reserva'), {'mov': otra.id},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        ).json()
        fila = next(p for p in datos['candidatos'] if p['id'] == pago.id)
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

    def test_el_pago_anual_sigue_en_la_lista_aunque_no_cuente(self):
        """El fallo que dejó todo esto sin usar: el pago se sacaba de la
        PANTALLA, no solo de los totales. En septiembre no había ninguna fila
        de la revisión, así que no había nada que pulsar para decir que la
        había pagado la hucha."""
        pago = self.mov('-1200', 12, self.mantenimiento, provision=self.revision)

        panel = self.panel(anio=2026, mes=9)
        filas = [m.pk for g in panel['grupos'] for m in g['movimientos']]
        self.assertIn(pago.pk, filas)
        # Pero no suma, y la cabecera lo dice aparte.
        self.assertEqual(panel['kpi_gastos'], Decimal('0'))
        self.assertEqual(panel['grupos'][0]['gastos'], Decimal('0'))
        self.assertEqual(panel['grupos'][0]['provisiones'], Decimal('-1200'))

    def test_y_desde_esa_fila_se_puede_emparejar(self):
        pago = self.mov('-1200', 12, self.mantenimiento, provision=self.revision)
        self.mov('928', 10, concepto='TRASPASO DESDE HUCHA')

        respuesta = self.client.get(reverse('extractos:listar'), {'anio': 2026, 'mes': 9})
        self.assertContains(respuesta, f'data-mov-id="{pago.pk}"')
        self.assertContains(respuesta, '¿Pagaste parte de esto con dinero que tenías apartado?')

    # ── Un cobro repartido ───────────────────────────────────────────────

    def repartir(self, pago, partes):
        """partes: lista de (importe, concepto)."""
        return self.client.post(
            reverse('extractos:dividir_movimiento', args=[pago.id]), {
                'importe': [p[0] for p in partes],
                'concepto': [p[1] for p in partes],
                'categoria_id': [str(self.mantenimiento.id)] * len(partes),
                'activo': [''] * len(partes),
            }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

    def test_la_parte_de_un_cobro_repartido_si_admite_reserva(self):
        """El caso real: el Norauto llevaba los neumáticos Y la revisión, así
        que está repartido, y era justo el pago que había que marcar. Ni el
        cobro ni sus partes ofrecían el botón: la reserva no se podía declarar
        en ninguna parte."""
        pago = self.mov('-928.63', 5, self.mantenimiento, provision=self.revision)
        self.repartir(pago, [('-543.00', 'Neumáticos'), ('-385.63', 'Revisión')])
        revision = pago.partes.get(concepto='Revisión')

        respuesta = self.emparejar(self.mov('300', 4, concepto='Hucha'), revision)
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(revision.impacto_real, Decimal('85.63'))

    def test_cubrir_el_cobro_entero_reparte_el_dinero_entre_sus_partes(self):
        """Es lo natural: la hucha se empareja con el recibo de Norauto, que es
        lo que hay en el banco. Pero el que cuenta en el presupuesto es cada
        línea de dentro, así que el dinero tiene que bajar hasta ellas."""
        pago = self.mov('-928.63', 5, self.mantenimiento)
        self.repartir(pago, [('-543.00', 'Neumáticos'), ('-385.63', 'Revisión')])
        self.emparejar(self.mov('928.63', 4, concepto='Hucha'), pago)

        neumaticos = pago.partes.get(concepto='Neumáticos')
        revision = pago.partes.get(concepto='Revisión')
        self.assertEqual(neumaticos.cubierto_por_reserva, Decimal('543.00'))
        self.assertEqual(revision.cubierto_por_reserva, Decimal('385.63'))
        self.assertEqual(neumaticos.impacto_real, Decimal('0'))
        self.assertEqual(revision.impacto_real, Decimal('0'))
        self.assertEqual(self.panel(anio=2026, mes=9)['kpi_gastos'], Decimal('0'))

    def test_una_cobertura_parcial_del_cobro_se_prorratea(self):
        """500 € sobre un recibo de 928,63 dejan el 53,8 % de cada parte
        cubierto, no la primera entera y la segunda a cero."""
        pago = self.mov('-928.63', 5, self.mantenimiento)
        self.repartir(pago, [('-543.00', 'Neumáticos'), ('-385.63', 'Revisión')])
        self.emparejar(self.mov('500', 4, concepto='Hucha'), pago)

        neumaticos = pago.partes.get(concepto='Neumáticos')
        revision = pago.partes.get(concepto='Revisión')
        self.assertEqual(neumaticos.cubierto_por_reserva, Decimal('292.37'))
        self.assertEqual(revision.cubierto_por_reserva, Decimal('207.63'))
        # Y las dos porciones suman exactamente lo que metiste: ni un céntimo
        # perdido por el redondeo.
        self.assertEqual(
            neumaticos.cubierto_por_reserva + revision.cubierto_por_reserva,
            Decimal('500.00'))
        # Y lo que pesa el mes es lo que quedó sin cubrir, entero.
        self.assertEqual(
            neumaticos.impacto_real + revision.impacto_real, Decimal('428.63'))

    def test_una_parte_puede_llevar_ademas_su_propia_cobertura(self):
        pago = self.mov('-928.63', 5, self.mantenimiento)
        self.repartir(pago, [('-543.00', 'Neumáticos'), ('-385.63', 'Revisión')])
        revision = pago.partes.get(concepto='Revisión')
        self.emparejar(self.mov('200', 4, concepto='Hucha del recibo'), pago)
        self.emparejar(self.mov('100', 3, concepto='Hucha de la revisión'), revision)

        # 200 × 385,63/928,63 = 83,05, más los 100 suyos.
        self.assertEqual(revision.cubierto_por_reserva, Decimal('183.05'))

    def test_el_cobro_repartido_se_ofrece_junto_a_sus_partes(self):
        pago = self.mov('-928.63', 5, self.mantenimiento)
        self.repartir(pago, [('-543.00', 'Neumáticos'), ('-385.63', 'Revisión')])
        hucha = self.mov('300', 4, concepto='Hucha')

        ids = {c['id'] for c in self.candidatos(hucha)['candidatos']}
        self.assertIn(pago.pk, ids)
        self.assertTrue({p.pk for p in pago.partes.all()} <= ids)

    def test_el_cobro_repartido_no_se_cuenta_tres_veces(self):
        """Lo que se ve en pantalla —el cobro y sus dos partes— parecen tres
        gastos. Solo suman las partes."""
        pago = self.mov('-928.63', 5, self.mantenimiento)
        self.repartir(pago, [('-543.00', 'Neumáticos'), ('-385.63', 'Revisión')])

        panel = self.panel(anio=2026, mes=9)
        self.assertEqual(panel['kpi_gastos'], Decimal('-928.63'))
        bloques = {b['tipo']: b for b in panel['bloques']}
        self.assertEqual(bloques['anual']['importe'], Decimal('928.63'))

    def test_ni_en_la_cifra_de_pagos_anuales_de_la_cabecera(self):
        """El cobro repartido es «anual» y sus partes también: contarlo a él
        además de a ellas metía el dinero dos veces."""
        pago = self.mov('-928.63', 5, self.mantenimiento, provision=self.revision)
        self.repartir(pago, [('-543.00', 'Neumáticos'), ('-385.63', 'Revisión')])
        for parte in pago.partes.all():
            parte.partida_conciliada = self.revision
            parte.save(update_fields=['partida_conciliada'])

        panel = self.panel(anio=2026, mes=9)
        self.assertEqual(panel['total_provisiones'], Decimal('928.63'))

    def test_el_cobro_repartido_dice_que_su_reserva_va_a_las_partes(self):
        """En su fila, «pesó X» no significaría nada: el cobro no cuenta."""
        pago = self.mov('-928.63', 5, self.mantenimiento, concepto='Norauto')
        self.repartir(pago, [('-543.00', 'Neumáticos'), ('-385.63', 'Revisión')])
        self.emparejar(self.mov('928.63', 4, concepto='Hucha'), pago)

        respuesta = self.client.get(reverse('extractos:listar'), {'anio': 2026, 'mes': 9})
        self.assertContains(respuesta, 'va a las partes')

    def test_las_partes_salen_pegadas_a_su_cobro(self):
        """Ordenadas solo por fecha, una parte salía tres filas más arriba y la
        otra más abajo, con apuntes ajenos en medio."""
        self.mov('-45.43', 5, self.alimentacion, concepto='Mercadona')
        pago = self.mov('-928.63', 5, self.mantenimiento, concepto='Norauto')
        self.mov('1.16', 5, concepto='Interés')
        self.repartir(pago, [('-543.00', 'Neumáticos'), ('-385.63', 'Revisión')])

        filas = [m for g in self.panel(anio=2026, mes=9)['grupos'] for m in g['movimientos']]
        donde = filas.index(next(m for m in filas if m.pk == pago.pk))
        siguientes = [m.concepto for m in filas[donde + 1:donde + 3]]
        self.assertEqual(siguientes, ['Neumáticos', 'Revisión'])

    def test_el_cobro_repartido_dice_en_pantalla_que_no_cuenta(self):
        pago = self.mov('-928.63', 5, self.mantenimiento, concepto='Norauto')
        self.repartir(pago, [('-543.00', 'Neumáticos'), ('-385.63', 'Revisión')])

        respuesta = self.client.get(reverse('extractos:listar'), {'anio': 2026, 'mes': 9})
        self.assertContains(respuesta, 'repartido en 2 · no cuenta')
        self.assertContains(respuesta, 'ext-importe-repartido')

    # ── Entrar desde el gasto, que es donde se mira ──────────────────────

    def candidatos(self, mov):
        return self.client.get(
            reverse('extractos:candidatos_reserva'), {'mov': mov.id},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        ).json()

    def test_desde_el_gasto_se_ofrecen_los_ingresos_cercanos(self):
        """Nadie piensa «voy a la fila del traspaso»: mira la revisión de 1.200
        y quiere decir ahí que 928 salieron de la hucha."""
        pago = self.mov('-1200', 12, self.mantenimiento)
        hucha = self.mov('928', 10, concepto='Traspaso de la hucha')
        otro_gasto = self.mov('-40', 11, self.mantenimiento, concepto='Otra cosa')

        datos = self.candidatos(pago)
        self.assertEqual(datos['sentido'], 'gasto')
        ids = {c['id'] for c in datos['candidatos']}
        self.assertIn(hucha.id, ids)
        self.assertNotIn(otro_gasto.id, ids)
        self.assertNotIn(pago.id, ids)
        self.assertEqual(datos['pendiente'], 1200)

    def test_desde_el_ingreso_se_siguen_ofreciendo_los_gastos(self):
        pago = self.mov('-1200', 12, self.mantenimiento)
        hucha = self.mov('928', 10, concepto='Hucha')

        datos = self.candidatos(hucha)
        self.assertEqual(datos['sentido'], 'ingreso')
        self.assertEqual({c['id'] for c in datos['candidatos']}, {pago.id})

    def test_un_ingreso_ya_puesto_en_otro_pago_no_se_ofrece(self):
        """Si no, emparejarlo aquí se lo robaría al otro pago sin avisar."""
        otro_pago = self.mov('-500', 3, self.mantenimiento, concepto='Otro pago')
        hucha = self.mov('928', 10, concepto='Hucha')
        self.emparejar(hucha, otro_pago)

        pago = self.mov('-1200', 12, self.mantenimiento)
        ids = {c['id'] for c in self.candidatos(pago)['candidatos']}
        self.assertNotIn(hucha.id, ids)

    def test_el_gasto_ve_lo_que_ya_tiene_puesto_para_poder_quitarlo(self):
        pago = self.mov('-1200', 12, self.mantenimiento)
        hucha = self.mov('928', 10, concepto='Hucha')
        self.emparejar(hucha, pago)

        datos = self.candidatos(pago)
        self.assertEqual([c['id'] for c in datos['puestos']], [hucha.id])
        self.assertEqual(datos['pendiente'], 272)
        # Y sigue ofreciéndose, para poder cambiarlo de sitio sin soltarlo antes.
        self.assertIn(hucha.id, {c['id'] for c in datos['candidatos']})

    def test_el_gasto_lleva_su_propio_boton(self):
        """El error de la primera versión: el control existía solo en la fila
        del ingreso, que es la única en la que no se te ocurre buscarlo."""
        self.mov('-1200', 12, self.mantenimiento, concepto='Norauto revisión')

        respuesta = self.client.get(reverse('extractos:listar'), {'anio': 2026, 'mes': 9})
        self.assertContains(respuesta, 'ext-cubre-boton')
        self.assertContains(respuesta, '¿Pagaste parte de esto con dinero que tenías apartado?')

    def test_el_gasto_ya_cubierto_lo_dice_en_su_boton(self):
        pago = self.mov('-1200', 12, self.mantenimiento)
        self.emparejar(self.mov('928', 10, concepto='Hucha'), pago)

        respuesta = self.client.get(reverse('extractos:listar'), {'anio': 2026, 'mes': 9})
        self.assertContains(respuesta, 'reserva puesta')

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


class SubirUnXlsTests(TestCase):
    """La pantalla de importación acepta el .xls, no solo el CSV y el .xlsx.

    Muchas bancas online españolas siguen sin ofrecer otra descarga que un
    «.xls», así que el recorrido completo —elegir el archivo, subirlo y llegar
    a la revisión— tiene que funcionar con él."""

    def setUp(self):
        self.hogar = Hogar.objects.create(nombre='Hogar de prueba')
        self.user = User.objects.create_user(username='tester', password='clave-de-prueba')
        perfil = self.user.userprofile
        perfil.hogar = self.hogar
        perfil.save()
        _crear_categorias_predefinidas(self.hogar)
        self.client.force_login(self.user)

    def test_el_formulario_deja_elegir_un_xls(self):
        respuesta = self.client.get(reverse('extractos:subir'))
        self.assertContains(respuesta, '.xls,')

    def test_subir_un_xls_lleva_a_la_revision_con_sus_movimientos(self):
        datos = (FIXTURES / 'caixabank.xls').read_bytes()
        archivo = SimpleUploadedFile(
            'Movimientos.xls', datos, content_type='application/vnd.ms-excel')

        respuesta = self.client.post(reverse('extractos:subir'), {
            'nombre_banco': 'CaixaBank', 'archivos': archivo,
        })
        self.assertRedirects(respuesta, reverse('extractos:revisar'))

        revision = self.client.get(reverse('extractos:revisar'))
        self.assertEqual(revision.context['total_ok'], 3)
        self.assertEqual(revision.context['total_error'], 0)
        self.assertEqual(revision.context['archivos'][0]['nombre'], 'Movimientos.xls')

    def test_un_xls_ilegible_avisa_en_vez_de_romper(self):
        archivo = SimpleUploadedFile(
            'roto.xls', b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' + b'\x00' * 64,
            content_type='application/vnd.ms-excel')

        respuesta = self.client.post(reverse('extractos:subir'), {'archivos': archivo})
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, 'No se pudo leer')
        self.assertNotIn('extractos_pendientes', self.client.session)
