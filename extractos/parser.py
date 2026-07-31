"""
Parseo flexible de extractos bancarios en CSV.

Los bancos exportan con cabeceras, delimitadores y formatos de fecha muy
distintos (coma o punto y coma; columna única de "Importe" o dos columnas
"Debe"/"Haber"; fechas con u sin hora...). Este módulo detecta la cabecera y
mapea las columnas por palabras clave, reutilizando los helpers de
finanzas.parsing para números y fechas.

`analizar_extracto` expone el resultado completo (cabecera detectada, mapeo
columna→campo, movimientos parseados y filas con error) para que la vista de
importación pueda mostrar un paso previo de revisión: el usuario ve qué
columna se ha asignado a cada dato y puede corregirla a mano si el formato no
se ha reconocido bien, en vez de que la importación falle en silencio.
"""

import csv
import io
from decimal import Decimal

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

CAMPOS_OBLIGATORIOS_ALT = ('importe', 'haber', 'debe')

# Etiquetas legibles para mostrar en el selector de mapeo manual.
CAMPO_LABELS = {
    'fecha': 'Fecha',
    'concepto': 'Concepto',
    'importe': 'Importe',
    'saldo': 'Saldo',
    'debe': 'Debe (cargo)',
    'haber': 'Haber (abono)',
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
        if 'fecha' in mapa and any(c in mapa for c in CAMPOS_OBLIGATORIOS_ALT):
            return idx, mapa
    return None, {}


def analizar_extracto(texto, mapeo_manual=None, cabecera_idx_manual=None):
    """
    Analiza el texto de un CSV y devuelve un dict con todo el detalle del
    parseo, pensado para alimentar tanto la importación directa como un paso
    previo de revisión/corrección en la UI:

      - delimitador: separador detectado.
      - filas: todas las filas crudas (list[list[str]]).
      - cabecera_idx: índice de fila usado como cabecera.
      - cabecera: lista de nombres de columna de esa fila.
      - mapa_auto: {campo: indice} detectado automáticamente por palabras clave.
      - mapa: {campo: indice} realmente usado (mapeo_manual tiene prioridad).
      - movimientos: [{fila, fecha, concepto, importe, saldo}].
      - filas_error: [{fila, motivo, valores}] filas descartadas con el motivo.
      - errores_generales: problemas que impiden el parseo (cabecera no
        encontrada, archivo vacío...).

    `mapeo_manual` permite forzar a qué columna corresponde cada campo
    (campo -> índice de columna, o None/'' para "no usar esta columna"),
    para corregir una detección automática incorrecta sin tener que arreglar
    el CSV a mano.
    """
    resultado = {
        'delimitador': ',', 'filas': [], 'cabecera_idx': None, 'cabecera': [],
        'mapa': {}, 'mapa_auto': {}, 'movimientos': [], 'filas_error': [],
        'errores_generales': [],
    }
    if not texto or not texto.strip():
        resultado['errores_generales'].append('El archivo está vacío.')
        return resultado

    delimitador = _detectar_delimitador(texto)
    resultado['delimitador'] = delimitador
    reader = csv.reader(io.StringIO(texto), delimiter=delimitador)
    filas = [f for f in reader if any((c or '').strip() for c in f)]
    resultado['filas'] = filas
    if not filas:
        resultado['errores_generales'].append('No se encontraron filas con datos.')
        return resultado

    if cabecera_idx_manual is not None and 0 <= cabecera_idx_manual < len(filas):
        cabecera_idx = cabecera_idx_manual
        mapa_auto = _mapear_columnas(filas[cabecera_idx])
    else:
        cabecera_idx, mapa_auto = _localizar_cabecera(filas)

    cabecera_no_reconocida = cabecera_idx is None
    if cabecera_no_reconocida:
        cabecera_idx = 0
        mapa_auto = {}
        resultado['errores_generales'].append(
            'No se reconoció automáticamente la cabecera. Indica manualmente '
            'qué columna corresponde a cada dato.'
        )

    cabecera = filas[cabecera_idx]
    resultado['cabecera_idx'] = cabecera_idx
    resultado['cabecera'] = cabecera
    resultado['mapa_auto'] = mapa_auto

    mapa = dict(mapa_auto)
    if mapeo_manual:
        for campo in _CLAVES:
            if campo not in mapeo_manual:
                continue
            valor = mapeo_manual[campo]
            if valor in (None, ''):
                mapa.pop(campo, None)
            else:
                try:
                    mapa[campo] = int(valor)
                except (TypeError, ValueError):
                    continue
    resultado['mapa'] = mapa

    if not cabecera_no_reconocida:
        if 'fecha' not in mapa or not any(c in mapa for c in CAMPOS_OBLIGATORIOS_ALT):
            resultado['errores_generales'].append(
                'Faltan columnas obligatorias: se necesita al menos Fecha e '
                'Importe (o Debe/Haber). Corrige el mapeo de columnas.'
            )
    if 'fecha' not in mapa or not any(c in mapa for c in CAMPOS_OBLIGATORIOS_ALT):
        resultado['movimientos'] = []
        resultado['filas_error'] = []
        return resultado

    movimientos = []
    filas_error = []
    for n, fila in enumerate(filas[cabecera_idx + 1:], start=cabecera_idx + 2):
        def col(campo):
            i = mapa.get(campo)
            if i is None or i >= len(fila):
                return ''
            return (fila[i] or '').strip()

        fecha_raw = col('fecha')
        importe_raw = col('importe')
        haber_raw = col('haber')
        debe_raw = col('debe')
        if not fecha_raw and not importe_raw and not haber_raw and not debe_raw:
            continue  # fila vacía en las columnas que nos importan

        fecha = parse_fecha(fecha_raw) if fecha_raw else None
        if not fecha:
            filas_error.append({
                'fila': n, 'motivo': f"Fecha no reconocida: '{fecha_raw}'.", 'valores': fila,
            })
            continue

        if 'importe' in mapa:
            importe = parse_decimal(importe_raw)
        elif haber_raw or debe_raw:
            haber = parse_decimal(haber_raw) or Decimal('0')
            debe = parse_decimal(debe_raw) or Decimal('0')
            importe = haber - abs(debe)
        else:
            importe = None

        if importe is None:
            filas_error.append({
                'fila': n, 'motivo': 'Importe no reconocido.', 'valores': fila,
            })
            continue

        concepto = col('concepto') or '(Sin concepto)'
        saldo = parse_decimal(col('saldo')) if 'saldo' in mapa else None

        movimientos.append({
            'fila': n, 'fecha': fecha, 'concepto': concepto,
            'importe': importe, 'saldo': saldo,
        })

    resultado['movimientos'] = movimientos
    resultado['filas_error'] = filas_error
    if not movimientos and not filas_error and not resultado['errores_generales']:
        resultado['errores_generales'].append('No se encontraron movimientos en el archivo.')

    return resultado


def parse_extracto(texto):
    """
    Compatibilidad con el uso anterior: recibe el texto de un CSV y devuelve
    (movimientos, errores) usando solo la detección automática.

    movimientos: lista de dicts {fecha (date), concepto (str), importe (Decimal), saldo (Decimal|None)}
    errores: lista de mensajes legibles.
    """
    r = analizar_extracto(texto)
    errores = list(r['errores_generales'])
    errores += [f"Fila {e['fila']}: {e['motivo']}" for e in r['filas_error']]
    movimientos = [
        {k: v for k, v in m.items() if k != 'fila'} for m in r['movimientos']
    ]
    return movimientos, errores
