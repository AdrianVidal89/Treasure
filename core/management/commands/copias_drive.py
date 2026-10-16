"""Copia automática de la base de datos a Google Drive.

    python manage.py copias_drive              # copia si toca y termina
    python manage.py copias_drive --ahora      # copia ya, toque o no
    python manage.py copias_drive --cada 300   # en bucle: mira cada 5 min si toca

El servicio `copias` del docker-compose lo ejecuta en bucle. La periodicidad,
la carpeta y la cuenta se configuran en Panel Admin > Copias de seguridad.
"""
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from core import copias_drive
from core.models import CopiaDriveConfig


class Command(BaseCommand):
    help = "Hace la copia de la base de datos a Google Drive cuando toca."

    def add_arguments(self, parser):
        parser.add_argument('--cada', type=int, default=0,
                            help="Segundos entre comprobaciones. 0: una sola pasada.")
        parser.add_argument('--ahora', action='store_true',
                            help="Hace la copia ya, sin mirar la programación.")

    def handle(self, *args, cada=0, ahora=False, **options):
        while True:
            close_old_connections()
            try:
                self.pasada(forzar=ahora)
            except Exception as e:  # base migrando, red caída...
                self.stderr.write(f"Error comprobando copias: {e}")
            if cada <= 0:
                return
            time.sleep(cada)

    def pasada(self, forzar=False):
        cfg = CopiaDriveConfig.obtener()
        if not forzar and not copias_drive.debe_copiar(cfg):
            return
        stamp = timezone.localtime().strftime('%Y-%m-%d %H:%M:%S')
        try:
            reg = copias_drive.hacer_copia(origen='manual' if forzar else 'auto')
        except copias_drive.ErrorCopia as e:
            self.stderr.write(f"[{stamp}] {e}")
            return
        if reg.ok:
            self.stdout.write(f"[{stamp}] Copia subida: {reg.nombre_archivo} ({reg.tamano} bytes)")
        else:
            self.stderr.write(f"[{stamp}] Copia fallida: {reg.mensaje}")
