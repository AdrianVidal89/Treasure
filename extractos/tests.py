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
from django.test import TestCase
from django.urls import reverse

from core.models import Hogar
from finanzas.models import CategoriaGasto
from finanzas.parsing import es_excel, leer_tabla
from finanzas.views_gastos import _crear_categorias_predefinidas

from .categorizacion import categorizar_por_codigo
from .models import ExtractoBancario, MovimientoBancario, ReglaCategorizacion
from .normalizacion import (
    contiene_patron, es_traspaso_interno, normalizar_comercio, normalizar_texto,
)
from .parser import analizar_extracto
from .views import _importar_analizados

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
        self.assertIsNone(traspaso.categoria)
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
