"""
Parseo flexible de extractos bancarios en CSV.

Los bancos españoles exportan con cabeceras y delimitadores muy distintos
(coma o punto y coma; columna única de "Importe" o dos columnas "Debe"/"Haber").
Este parser detecta la cabecera y mapea las columnas por palabras clave,
reutilizando los helpers de finanzas.parsing para números y fechas.
"""

import csv
import io

from finanzas.parsing import parse_decimal, parse_fecha

# Palabras clave por columna (se comparan en minúsculas, sin acentos relevantes).
_CLAVES = {
    'fecha': ('fecha valor', 'fecha operac', 'fecha', 'date'),
    'concepto': ('concepto', 'descripci', 'detalle', 'movimiento', 'description'),
    'importe': ('importe', 'monto', 'cantidad', 'amount'),
    'saldo': ('saldo', 'balance'),
    'debe': ('debe', 'cargo', 'debito', 'débito'),
    'haber': ('haber', 'abono', 'credito', 'crédito'),
}


def _detectar_delimitador(texto):
    primera = ''
    for linea in texto.splitlines():
        if linea.strip():
            primera = linea
            break
    return ';' if primera.count(';') > primera.count(',') else ','


def _norm(s):
    return (s or '').strip().lower()


def _mapear_columnas(cabecera):
    """Devuelve {campo: indice} a partir de la fila de cabecera."""
    mapa = {}
    for i, celda in enumerate(cabecera):
        texto = _norm(celda)
        if not texto:
            continue
        for campo, claves in _CLAVES.items():
            if campo in mapa:
                continue
            if any(clave in texto for clave in claves):
                mapa[campo] = i
                break
    return mapa


def _localizar_cabecera(filas):
    """La cabecera es la primera fila que contiene una columna de fecha y otra reconocible."""
    for idx, fila in enumerate(filas):
        mapa = _mapear_columnas(fila)
        if 'fecha' in mapa and ('importe' in mapa or ('debe' in mapa and 'haber' in mapa)
                                or 'haber' in mapa or 'debe' in mapa):
            return idx, mapa
    return None, {}


def parse_extracto(texto):
    """
    Recibe el texto de un CSV y devuelve (movimientos, errores).

    movimientos: lista de dicts {fecha (date), concepto (str), importe (Decimal), saldo (Decimal|None)}
    errores: lista de mensajes legibles.
    """
    errores = []
    if not texto or not texto.strip():
        return [], ['El archivo está vacío.']

    delimitador = _detectar_delimitador(texto)
    reader = csv.reader(io.StringIO(texto), delimiter=delimitador)
    filas = [f for f in reader if any((c or '').strip() for c in f)]
    if not filas:
        return [], ['No se encontraron filas con datos.']

    cabecera_idx, mapa = _localizar_cabecera(filas)
    if cabecera_idx is None:
        return [], [
            'No se reconoció la cabecera. El CSV debe incluir al menos columnas de '
            'Fecha e Importe (o Debe/Haber).'
        ]

    movimientos = []
    for n, fila in enumerate(filas[cabecera_idx + 1:], start=cabecera_idx + 2):
        def col(campo):
            i = mapa.get(campo)
            if i is None or i >= len(fila):
                return ''
            return (fila[i] or '').strip()

        fecha_raw = col('fecha')
        if not fecha_raw:
            continue
        fecha = parse_fecha(fecha_raw)
        if not fecha:
            errores.append(f"Fila {n}: fecha no reconocida '{fecha_raw}'.")
            continue

        # Importe: columna única o Debe/Haber
        if 'importe' in mapa:
            importe = parse_decimal(col('importe'))
        else:
            haber = parse_decimal(col('haber')) or 0
            debe = parse_decimal(col('debe')) or 0
            importe = haber - abs(debe) if (col('haber') or col('debe')) else None

        if importe is None:
            errores.append(f"Fila {n}: importe no reconocido.")
            continue

        concepto = col('concepto') or '(Sin concepto)'
        saldo = parse_decimal(col('saldo')) if 'saldo' in mapa else None

        movimientos.append({
            'fecha': fecha,
            'concepto': concepto,
            'importe': importe,
            'saldo': saldo,
        })

    if not movimientos and not errores:
        errores.append('No se encontraron movimientos válidos en el archivo.')

    return movimientos, errores
