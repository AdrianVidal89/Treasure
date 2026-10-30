"""El ahorro del mes es todo lo que no se gasta, y se desglosa por destino."""

from decimal import Decimal as D

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .ahorro import desglose_ahorro


class DesgloseAhorroTests(TestCase):
    def setUp(self):
        from core.models import Hogar, UserProfile
        from .models import (CategoriaGasto, FondoFamiliar, FuenteIngreso,
                             PartidaGasto, ReglaReparto, SubsobreFondo)

        self.user = User.objects.create_user('adri', password='x', first_name='Adri')
        self.hogar = Hogar.objects.create(nombre='Casa', creado_por=self.user)
        perfil, _ = UserProfile.objects.get_or_create(user=self.user)
        perfil.hogar = self.hogar
        perfil.save()
        FuenteIngreso.objects.create(
            hogar=self.hogar, usuario=self.user, nombre='Nómina', tipo='fijo',
            modo_entrada='anual', importe_declarado=D('60000'), es_bruto=False,
            num_pagas=12, activo=True)  # 5.000 €/mes
        cat = CategoriaGasto.objects.create(hogar=self.hogar, nombre='Casa', tipo='fijo')
        self.conjunta = FondoFamiliar.objects.create(hogar=self.hogar, nombre='Conjunta', tipo_fondo='comun')
        # Una cuenta «común» que no paga nada: lo que entra es ahorro, no «libre».
        self.bankinter = FondoFamiliar.objects.create(hogar=self.hogar, nombre='Bankinter', tipo_fondo='comun')
        self.sp500 = FondoFamiliar.objects.create(hogar=self.hogar, nombre='SP500', tipo_fondo='inversion')
        PartidaGasto.objects.create(hogar=self.hogar, categoria=cat, nombre='Súper',
                                    importe=D('2000'), periodicidad='mensual', fondo_asignado=self.conjunta)
        # Un gasto que no paga ningún fondo.
        PartidaGasto.objects.create(hogar=self.hogar, categoria=cat, nombre='Garaje',
                                    importe=D('100'), periodicidad='mensual')
        ReglaReparto.objects.create(hogar=self.hogar, fondo=self.conjunta, nombre='Común',
                                    tipo_regla='cubrir_fondo', usuario=self.user, orden=1)
        ReglaReparto.objects.create(hogar=self.hogar, fondo=self.bankinter, nombre='Resto',
                                    tipo_regla='resto', usuario=self.user, orden=2)
        SubsobreFondo.objects.create(fondo=self.conjunta, nombre='Inversión', tipo='libre',
                                     importe_manual=D('300'), fondo_destino=self.sp500)

    def _desglose(self):
        from .distribucion import calcular_flujos
        return desglose_ahorro(calcular_flujos(self.hogar))

    def test_ahorro_es_todo_lo_que_no_se_gasta(self):
        ah = self._desglose()
        self.assertEqual(ah['ingreso'], D('5000'))
        self.assertEqual(ah['gastos'], D('2100'))
        self.assertEqual(ah['ahorro'], D('2900'))
        self.assertEqual(ah['tasa'], 58.0)
        self.assertEqual(ah['inversion'], D('300'))

    def test_los_destinos_suman_el_ahorro(self):
        ah = self._desglose()
        total = sum(x['importe'] for x in ah['destinos'] + ah['restas'])
        self.assertEqual(total, ah['ahorro'])
        nombres = {x['nombre']: x for x in ah['destinos']}
        self.assertEqual(nombres['Bankinter']['clase'], 'ahorro')
        self.assertEqual(nombres['SP500']['clase'], 'inversion')

    def test_el_gasto_sin_fondo_resta(self):
        ah = self._desglose()
        sin_fondo = [x for x in ah['restas'] if x['clase'] == 'sin_fondo']
        self.assertEqual(len(sin_fondo), 1)
        self.assertEqual(sin_fondo[0]['importe'], D('-100'))

    def test_la_pantalla_ya_no_habla_de_libre(self):
        self.client.force_login(self.user)
        resp = self.client.get(reverse('finanzas:vista_distribucion'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Este mes ahorráis')
        self.assertContains(resp, 'A dónde va lo que ahorráis')
        self.assertNotContains(resp, 'stat-tile-label">Libre')

    def test_el_panel_enseña_el_patrimonio(self):
        self.client.force_login(self.user)
        resp = self.client.get(reverse('dashboard'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Lo que tenéis')
        self.assertEqual(resp.context['ah']['ahorro'], D('2900'))


class SaldoPrevistoYSaludTests(DesgloseAhorroTests):
    """Distribución dice cómo queda cada fondo partiendo de su saldo real, y el
    inicio enseña los indicadores básicos de salud."""

    def test_el_saldo_previsto_parte_del_mes_anterior(self):
        import datetime
        from .models import SaldoRealFondo
        from .distribucion import calcular_flujos
        from .saldo_previsto import saldos_previstos
        hoy = datetime.date.today()
        anio, mes = (hoy.year - 1, 12) if hoy.month == 1 else (hoy.year, hoy.month - 1)
        SaldoRealFondo.objects.create(fondo=self.bankinter, año=anio, mes=mes, saldo=D('80000'))
        d = calcular_flujos(self.hogar, hoy.month, hoy.year)
        pv = saldos_previstos(self.hogar, hoy.year, hoy.month, d['fondos_aportaciones'])
        b = pv[self.bankinter.id]
        self.assertEqual(b['base'], D('80000'))
        self.assertEqual(b['final'], D('80000') + b['movimiento'])
        self.assertGreater(b['movimiento'], 0)

    def test_la_pantalla_enseña_el_saldo_previsto(self):
        import datetime
        from .models import SaldoRealFondo
        hoy = datetime.date.today()
        anio, mes = (hoy.year - 1, 12) if hoy.month == 1 else (hoy.year, hoy.month - 1)
        SaldoRealFondo.objects.create(fondo=self.bankinter, año=anio, mes=mes, saldo=D('80000'))
        self.client.force_login(self.user)
        resp = self.client.get(reverse('finanzas:vista_distribucion'))
        self.assertContains(resp, 'Quedará en')
        self.assertContains(resp, '80.000 €')

    def test_indicadores_de_salud(self):
        from .salud import indicadores
        k = {x['clave']: x for x in indicadores(
            self.hogar, liquidez=D('30000'), inversiones=D('10000'),
            ingreso_mensual=D('5000'), gasto_mensual=D('2500'), tasa_ahorro=D('50'))}
        self.assertEqual(k['colchon']['valor'], '12')
        self.assertEqual(k['colchon']['estado'], 'bien')
        self.assertEqual(k['ahorro']['estado'], 'bien')
        self.assertEqual(k['invertido']['valor'], '25')
