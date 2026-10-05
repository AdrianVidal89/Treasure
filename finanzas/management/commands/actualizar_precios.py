"""Actualiza las cotizaciones de todas las inversiones con ticker.

    python manage.py actualizar_precios            # una pasada y termina
    python manage.py actualizar_precios --cada 3600 # en bucle, cada hora

El servicio `precios` del docker-compose lo ejecuta en bucle, así los precios
están al día aunque nadie abra la aplicación. En cada pasada, además del
precio actual, completa el histórico de cierres que usa la gráfica de
evolución de Inversiones.
"""
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.db.models import Min

from finanzas.cotizaciones import actualizar_precios, asegurar_historico
from finanzas.models import Inversion


class Command(BaseCommand):
    help = "Actualiza los precios de mercado (Yahoo Finance) de todas las inversiones."

    def add_arguments(self, parser):
        parser.add_argument(
            '--cada', type=int, default=0,
            help="Segundos entre pasadas. 0 (por defecto): una sola pasada.",
        )

    def handle(self, *args, cada=0, **options):
        while True:
            close_old_connections()
            espera = cada
            try:
                self.pasada()
            except Exception as e:  # la base aún migrando, red caída...
                self.stderr.write(f"Error actualizando precios: {e}")
                espera = min(cada, 300)  # reintento pronto, no dentro de una hora
            if cada <= 0:
                return
            time.sleep(espera)

    def pasada(self):
        inversiones = list(
            Inversion.objects.filter(actualizable=True)
            .exclude(tipo='DEPOSITO')
            .exclude(ticker__isnull=True).exclude(ticker='')
            .select_related('valor_actual')
        )
        actualizados, errores = actualizar_precios(inversiones)
        stamp = time.strftime('%Y-%m-%d %H:%M:%S')
        self.stdout.write(f"[{stamp}] Precios actualizados: {', '.join(actualizados) or '—'}")
        if errores:
            self.stderr.write(f"[{stamp}] Errores: {', '.join(errores)}")

        # Histórico de cierres desde el primer movimiento de cada ticker.
        primeros = {}
        for inv in (Inversion.objects.filter(pk__in=[i.pk for i in inversiones])
                    .annotate(primera=Min('movimientos__fecha'))):
            ticker = inv.ticker.strip().upper()
            if inv.primera and (ticker not in primeros or inv.primera < primeros[ticker]):
                primeros[ticker] = inv.primera
        for ticker, desde in primeros.items():
            nuevos = asegurar_historico(ticker, desde, forzar=True)
            if nuevos:
                self.stdout.write(f"[{stamp}] {ticker}: {nuevos} cierres históricos nuevos")
