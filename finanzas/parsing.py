"""
Utilidades de parseo compartidas para importaciones CSV.

Extraídas de finanzas.views.importar_movimientos_csv para poder reutilizarlas
tanto en la importación de movimientos de inversión como en la nueva sección de
extractos bancarios (app `extractos`), sin duplicar la lógica.
"""

import datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser


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
    elif '.' in s:
        # Mismo criterio que con la coma, por simetría: en formato español el
        # punto agrupa los miles, así que «18.000» son dieciocho mil y no
        # dieciocho. Un importe de 18 € con céntimos se escribe «18,00».
        partes = s.split('.')
        if len(partes) == 2 and len(partes[1]) == 3 and partes[1].isdigit():
            s = s.replace('.', '')   # miles: "18.000" → "18000"
        elif len(partes) > 2 and all(len(t) == 3 and t.isdigit() for t in partes[1:]):
            s = s.replace('.', '')   # miles repetidos: "1.234.567"
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


def _celda_a_texto(valor):
    """Serializa una celda de Excel a texto para el CSV intermedio."""
    import datetime as _dt
    if valor is None:
        return ''
    if isinstance(valor, (_dt.datetime, _dt.date)):
        return valor.isoformat()
    if isinstance(valor, float) and valor.is_integer():
        return str(int(valor))
    return str(valor)


def _filas_a_csv(filas):
    """Serializa una lista de filas (list[list]) al texto CSV intermedio."""
    import csv
    import io

    salida = io.StringIO()
    escritor = csv.writer(salida)
    for fila in filas:
        escritor.writerow([_celda_a_texto(c) for c in fila])
    return salida.getvalue()


def _xlsx_a_filas(datos):
    """Primera hoja de un .xlsx/.xlsm (formato OOXML, un zip)."""
    import io
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(datos), read_only=True, data_only=True)
    try:
        return list(wb.active.iter_rows(values_only=True))
    finally:
        wb.close()


def _xls_a_filas(datos):
    """Primera hoja de un .xls clásico (BIFF/OLE2), el que siguen exportando
    muchas bancas online españolas. openpyxl no lo lee; xlrd 2.x sí (y solo
    este formato, que es justo el reparto que queremos)."""
    import datetime as _dt
    import xlrd

    libro = xlrd.open_workbook(file_contents=datos)
    hoja = libro.sheet_by_index(0)
    filas = []
    for i in range(hoja.nrows):
        fila = []
        for celda in hoja.row(i):
            valor = celda.value
            if celda.ctype == xlrd.XL_CELL_DATE:
                try:
                    partes = xlrd.xldate_as_tuple(valor, libro.datemode)
                    valor = _dt.datetime(*partes) if any(partes[:3]) else valor
                except (ValueError, xlrd.XLDateError):
                    pass
            fila.append(valor)
        filas.append(fila)
    return filas


def _declara_dtd(datos):
    """¿Trae el XML un `<!DOCTYPE ...>` delante del elemento raíz?

    ElementTree expande las entidades internas que declare un DTD, así que un
    XML amañado («billion laughs») agotaría la memoria al importarlo. Ningún
    banco exporta con DTD, de modo que preferimos rechazarlo. Se recorre el
    prólogo de verdad (espacios, comentarios e instrucciones de proceso) en vez
    de buscar la cadena a ciegas, para no confundirla con el contenido."""
    i = 3 if datos[:3] == b'\xef\xbb\xbf' else 0
    while i < len(datos):
        while i < len(datos) and datos[i:i + 1].isspace():
            i += 1
        if datos[i:i + 9].lower() == b'<!doctype':
            return True
        if datos[i:i + 4] == b'<!--':
            cierre = b'-->'
        elif datos[i:i + 2] == b'<?':
            cierre = b'?>'
        else:
            return False  # aquí empieza ya el elemento raíz
        fin = datos.find(cierre, i)
        if fin == -1:
            return False  # XML truncado: que falle el parser, no esta criba
        i = fin + len(cierre)
    return False


def _spreadsheetml_a_filas(datos):
    """Primera hoja de un «.xls» que en realidad es SpreadsheetML 2003 (XML).

    Es el tercer disfraz habitual del .xls: no es BIFF ni una tabla HTML, sino
    el XML de Excel 2003 (`<Workbook xmlns="urn:schemas-microsoft-com:office:
    spreadsheet">`). Varias bancas online españolas exportan así. Hay que
    respetar `ss:Index`, que salta celdas vacías en vez de escribirlas, o las
    columnas se desplazan."""
    import xml.etree.ElementTree as ET

    if _declara_dtd(datos):
        raise ValueError('El XML declara un DTD; no se importa por seguridad.')

    NS = '{urn:schemas-microsoft-com:office:spreadsheet}'
    raiz = ET.fromstring(datos)
    hoja = raiz.find(f'{NS}Worksheet')
    if hoja is None:
        return []
    tabla = hoja.find(f'{NS}Table')
    if tabla is None:
        return []

    filas = []
    for fila_xml in tabla.findall(f'{NS}Row'):
        fila = []
        for celda in fila_xml.findall(f'{NS}Cell'):
            indice = celda.get(f'{NS}Index')
            if indice:
                try:
                    # ss:Index es 1-based y apunta a dónde va ESTA celda.
                    fila.extend([''] * (int(indice) - 1 - len(fila)))
                except ValueError:
                    pass
            dato = celda.find(f'{NS}Data')
            fila.append('' if dato is None else ''.join(dato.itertext()))
        filas.append(fila)
    return filas


class _TablaHTML(HTMLParser):
    """Extrae las filas de la primera tabla de un HTML.

    Varios bancos exportan un «.xls» que en realidad es una tabla HTML; sin
    esto se lee como texto plano y el parseo de columnas falla."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.filas = []
        self._fila = None
        self._celda = None

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self._fila = []
        elif tag in ('td', 'th') and self._fila is not None:
            self._celda = []

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self._celda is not None:
            self._fila.append(' '.join(''.join(self._celda).split()))
            self._celda = None
        elif tag == 'tr' and self._fila is not None:
            self.filas.append(self._fila)
            self._fila = None

    def handle_data(self, data):
        if self._celda is not None:
            self._celda.append(data)


def _html_a_filas(datos):
    try:
        texto = datos.decode('utf-8-sig')
    except UnicodeDecodeError:
        texto = datos.decode('latin-1', errors='replace')
    parser = _TablaHTML()
    parser.feed(texto)
    return parser.filas


def _cabeza(datos):
    """Primeros bytes en minúsculas y sin BOM, para olfatear el formato."""
    return datos[:2048].lstrip(b'\xef\xbb\xbf').lstrip().lower()


def _parece_spreadsheetml(cabeza):
    return (cabeza.startswith(b'<')
            and b'urn:schemas-microsoft-com:office:spreadsheet' in cabeza)


def _parece_html(cabeza):
    return cabeza.startswith(b'<') and (b'<table' in cabeza or b'<html' in cabeza)


def _excel_a_csv(archivo):
    """Convierte la primera hoja de un Excel a texto CSV, para que el resto del
    pipeline de importación (detección de cabecera, mapeo de columnas,
    revisión) funcione igual que con un CSV.

    El formato se decide por los bytes iniciales y no por la extensión, porque
    lo que un banco llama «.xls» puede ser un xlsx, un BIFF antiguo, un XML de
    Excel 2003 (SpreadsheetML) o una tabla HTML."""
    datos = archivo.read() if hasattr(archivo, 'read') else archivo
    if isinstance(datos, str):
        datos = datos.encode('utf-8')

    cabeza = _cabeza(datos)
    if datos[:2] == b'PK':
        filas = _xlsx_a_filas(datos)
    elif datos[:8] == b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1':
        filas = _xls_a_filas(datos)
    elif _parece_spreadsheetml(cabeza):
        # Antes que el HTML: un SpreadsheetML lleva `<Table>` y colaba por el
        # lector de tablas HTML, que no encuentra ni un `<tr>` y devolvía el
        # archivo vacío sin decir por qué.
        filas = _spreadsheetml_a_filas(datos)
    elif _parece_html(cabeza):
        filas = _html_a_filas(datos)
    else:
        # No es ninguno de los formatos binarios conocidos: probablemente sea
        # un CSV con extensión equivocada.
        return leer_csv(datos)
    return _filas_a_csv(filas)


def es_excel(nombre):
    return (nombre or '').lower().endswith(('.xlsx', '.xlsm', '.xls'))


def leer_tabla(archivo):
    """Lee un fichero subido y devuelve texto CSV, aceptando tanto CSV como
    Excel (.xlsx/.xlsm/.xls, incluidos los «.xls» que en realidad son una
    tabla HTML o un XML de Excel 2003).
    Punto de entrada único para la importación de extractos: el resto del
    código sigue trabajando con texto CSV."""
    nombre = getattr(archivo, 'name', '') or ''
    if es_excel(nombre):
        return _excel_a_csv(archivo)
    return leer_csv(archivo)
