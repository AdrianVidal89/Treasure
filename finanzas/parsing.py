"""
Utilidades de parseo compartidas para importaciones CSV.

Extraídas de finanzas.views.importar_movimientos_csv para poder reutilizarlas
tanto en la importación de movimientos de inversión como en la nueva sección de
extractos bancarios (app `extractos`), sin duplicar la lógica.
"""

import datetime
from decimal import Decimal, InvalidOperation


def parse_decimal(s):
    """
    Convierte cadenas como '10.20€', '1,842€', '9,30€', '0.00€', '#N/A' → Decimal o None.
    Regla de coma:
      - Si coma con exactamente 3 dígitos tras ella → separador de miles (1,842 → 1842)
      - En cualquier otro caso → separador decimal (9,30 → 9.30)
    """
    if s is None:
        return None
    s = str(s).strip()
    if s.lower() in ('', '#n/a', 'n/a', 'xx.xx€', 'xx.xx', '-', '—'):
        return None
    # Eliminar símbolos de moneda y espacios
    s = s.replace('€', '').replace('$', '').replace(' ', '').replace('\xa0', '').strip()
    if not s:
        return None
    negativo = False
    # Paréntesis contables: (12,34) → -12,34
    if s.startswith('(') and s.endswith(')'):
        negativo = True
        s = s[1:-1]
    if ',' in s and '.' in s:
        # Ambos separadores presentes: el que aparece EL ÚLTIMO es el decimal.
        # "1.842,00" (europeo) → 1842.00 ; "1,842.50" (US) → 1842.50
        if s.rfind(',') > s.rfind('.'):
            s = s.replace('.', '').replace(',', '.')  # europeo
        else:
            s = s.replace(',', '')                    # US
    elif ',' in s:
        partes = s.split(',')
        if len(partes) == 2 and len(partes[1]) == 3 and partes[1].isdigit():
            s = s.replace(',', '')   # miles: "1,842" → "1842"
        else:
            s = s.replace(',', '.')  # decimal europeo: "9,30" → "9.30"
    try:
        valor = Decimal(s)
    except InvalidOperation:
        return None
    return -valor if negativo else valor


def parse_fecha(s):
    """Parseo tolerante de fechas en los formatos habituales de bancos y hojas de cálculo.

    Muchos exportadores (Revolut, N26, Wise...) incluyen la hora junto a la
    fecha, ej. "2026-07-01 05:32:21" o "2026-07-01T05:32:21Z". Nos quedamos
    solo con la parte de fecha antes de probar los formatos conocidos.
    """
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    parte_fecha = s.split('T')[0].split(' ')[0]
    for fmt in ('%d/%m/%Y', '%m/%d/%Y', '%Y-%m-%d', '%d-%m-%Y',
                '%d/%m/%y', '%d.%m.%Y', '%Y/%m/%d'):
        try:
            return datetime.datetime.strptime(parte_fecha, fmt).date()
        except ValueError:
            continue
    return None


def leer_csv(archivo):
    """
    Lee un fichero subido (UploadedFile o bytes) y devuelve su contenido como texto.
    Elimina el BOM de Excel/Sheets (utf-8-sig) y cae a latin-1 si hace falta.
    """
    datos = archivo.read() if hasattr(archivo, 'read') else archivo
    if isinstance(datos, str):
        return datos
    try:
        return datos.decode('utf-8-sig')
    except UnicodeDecodeError:
        return datos.decode('latin-1', errors='replace')
