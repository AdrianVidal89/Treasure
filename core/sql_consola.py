"""Motor de la consola SQL del panel de administración.

Una consola SQL es la página más peligrosa de la aplicación: da acceso directo a
todo. Por eso aquí no se ejecuta nada a lo bruto, sino con cuatro reglas:

1. Una sola sentencia por ejecución. Nada de `SELECT 1; DELETE FROM ...`.
2. Nada de DDL (DROP, TRUNCATE, ALTER, CREATE...). En una base de datos de
   finanzas personales eso nunca es lo que se quiere hacer y el coste de
   equivocarse es la base entera.
3. Las lecturas se ejecutan y ya. Las escrituras van en dos pasos: primero una
   PRUEBA, que corre dentro de una transacción, cuenta las filas que tocaría y
   deshace; y solo después, si se confirma, se aplica de verdad.
4. Todo lo ejecutado queda registrado (quién, cuándo, qué y con qué resultado).

El límite de filas devueltas evita que un `SELECT *` sobre una tabla grande se
lleve por delante la memoria del proceso.
"""

import sqlparse
from django.db import connection, transaction

LIMITE_FILAS = 500

# Verbos que solo leen: se pueden ejecutar directamente.
VERBOS_LECTURA = {'SELECT', 'WITH', 'EXPLAIN', 'SHOW', 'PRAGMA'}

# Verbos que cambian datos: exigen prueba y confirmación.
VERBOS_ESCRITURA = {'INSERT', 'UPDATE', 'DELETE'}

# Todo lo demás se rechaza; estos se nombran para poder explicar por qué.
VERBOS_PROHIBIDOS = {
    'DROP', 'TRUNCATE', 'ALTER', 'CREATE', 'GRANT', 'REVOKE',
    'VACUUM', 'ATTACH', 'DETACH', 'REINDEX', 'REPLACE', 'COPY',
}


class SQLNoPermitido(Exception):
    """La sentencia no se puede ejecutar desde aquí."""


def _sentencias(sql):
    """Sentencias no vacías de `sql`, respetando comillas y comentarios."""
    return [s for s in (t.strip().rstrip(';').strip() for t in sqlparse.split(sql)) if s]


def verbo_de(sentencia):
    """Primera palabra clave real de la sentencia, en mayúsculas."""
    parsed = sqlparse.parse(sentencia)
    if not parsed:
        return ''
    for token in parsed[0].flatten():
        if token.is_whitespace or token.ttype in sqlparse.tokens.Comment:
            continue
        return token.value.upper()
    return ''


def analizar(sql):
    """Valida la consulta y devuelve (sentencia, verbo, es_escritura).

    Lanza SQLNoPermitido con un motivo entendible si no se puede ejecutar."""
    sentencias = _sentencias(sql or '')
    if not sentencias:
        raise SQLNoPermitido("Escribe una consulta.")
    if len(sentencias) > 1:
        raise SQLNoPermitido(
            f"Solo se ejecuta una sentencia cada vez, y aquí hay {len(sentencias)}. "
            "Lánzalas de una en una."
        )

    sentencia = sentencias[0]
    verbo = verbo_de(sentencia)

    if verbo in VERBOS_PROHIBIDOS:
        raise SQLNoPermitido(
            f"{verbo} no se puede ejecutar desde aquí: cambia la estructura de la "
            "base de datos, no los datos. Eso se hace con una migración."
        )
    if verbo in VERBOS_LECTURA:
        return sentencia, verbo, False
    if verbo in VERBOS_ESCRITURA:
        return sentencia, verbo, True
    raise SQLNoPermitido(f"No sé qué hace «{verbo or sql.strip()[:20]}»; no se ejecuta.")


def _leer(cursor):
    """Filas del cursor, recortadas al límite. Devuelve (columnas, filas, hay_mas)."""
    columnas = [c[0] for c in cursor.description] if cursor.description else []
    filas = cursor.fetchmany(LIMITE_FILAS + 1)
    hay_mas = len(filas) > LIMITE_FILAS
    return columnas, [list(f) for f in filas[:LIMITE_FILAS]], hay_mas


def ejecutar(sql, aplicar=False):
    """Ejecuta la consulta y devuelve un dict con el resultado.

    - Lectura: devuelve columnas y filas.
    - Escritura sin `aplicar`: la ejecuta dentro de una transacción para contar
      las filas afectadas y la deshace. Es la prueba.
    - Escritura con `aplicar`: la ejecuta de verdad.
    """
    sentencia, verbo, es_escritura = analizar(sql)

    resultado = {
        'sentencia': sentencia, 'verbo': verbo, 'es_escritura': es_escritura,
        'aplicado': False, 'columnas': [], 'filas': [], 'hay_mas': False,
        'filas_afectadas': None,
    }

    if not es_escritura:
        with connection.cursor() as cursor:
            cursor.execute(sentencia)
            resultado['columnas'], resultado['filas'], resultado['hay_mas'] = _leer(cursor)
        return resultado

    if aplicar:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(sentencia)
                resultado['filas_afectadas'] = cursor.rowcount
        resultado['aplicado'] = True
        return resultado

    # Prueba: se ejecuta y se deshace, así que cuenta filas sin tocar nada.
    class _Deshacer(Exception):
        pass

    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(sentencia)
                resultado['filas_afectadas'] = cursor.rowcount
            raise _Deshacer
    except _Deshacer:
        pass
    return resultado


def esquema():
    """Tablas de la base de datos con sus columnas, para tenerlas a mano al
    escribir la consulta (y para poder pasárselas a quien te ayude con el SQL)."""
    tablas = []
    with connection.cursor() as cursor:
        for nombre in sorted(connection.introspection.table_names(cursor)):
            try:
                columnas = connection.introspection.get_table_description(cursor, nombre)
            except Exception:
                continue
            tablas.append({
                'nombre': nombre,
                'columnas': [c.name for c in columnas],
            })
    return tablas
