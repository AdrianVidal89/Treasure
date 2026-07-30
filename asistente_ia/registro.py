"""Registro de esquema — única fuente de verdad sobre qué entidades puede ver
(y, para un subconjunto curado, escribir) el asistente de IA.

Nada en el resto de la app debe declarar una tool ad-hoc que consulte un
modelo directamente: toda lectura pasa por `ENTIDADES` + las funciones de
este módulo, así que un campo nuevo en un modelo se vuelve visible
automáticamente (se serializa solo) y una entidad nueva solo requiere una
línea aquí.
"""
from datetime import date, datetime
from decimal import Decimal

from django.apps import apps
from django.db.models import Q

# ─── Registro de entidades consultables ──────────────────────────────────
# nombre_expuesto -> (app, modelo, ruta_de_filtrado_por_hogar | None)
# None = tabla de referencia global (no contiene datos de ningún hogar).

ENTIDADES = {
    # Núcleo
    'hogar': ('core', 'Hogar', 'id'),
    'miembros': ('core', 'UserProfile', 'hogar'),

    # Cuentas, tarjetas y préstamos
    'cuentas_bancarias': ('finanzas', 'CuentaBancaria', 'usuario__userprofile__hogar'),
    'saldos_cuentas': ('finanzas', 'SaldoMensualCuenta', 'cuenta__usuario__userprofile__hogar'),
    'cuentas_credito': ('finanzas', 'CuentaCredito', 'usuario__userprofile__hogar'),
    'deudas_credito': ('finanzas', 'DeudaMensualCredito', 'cuenta__usuario__userprofile__hogar'),
    'tarjetas': ('finanzas', 'TarjetaCredito', 'usuario__userprofile__hogar'),
    'saldos_tarjetas': ('finanzas', 'SaldoMensualTarjeta', 'tarjeta__usuario__userprofile__hogar'),
    'prestamos': ('finanzas', 'PrestamoSimple', 'usuario__userprofile__hogar'),

    # Inversiones
    'inversiones': ('finanzas', 'Inversion', 'usuario__userprofile__hogar'),
    'movimientos_inversion': ('finanzas', 'MovimientoInversion', 'inversion__usuario__userprofile__hogar'),
    'valores_actuales_inversion': ('finanzas', 'ValorActualInversion', 'inversion__usuario__userprofile__hogar'),
    'historial_valores_inversion': ('finanzas', 'HistorialValorInversion', 'inversion__usuario__userprofile__hogar'),
    'resumenes_inversiones': ('finanzas', 'ResumenInversionesMensual', 'usuario__userprofile__hogar'),

    # Ingresos
    'fuentes_ingreso': ('finanzas', 'FuenteIngreso', 'hogar'),
    'ajustes_ingreso': ('finanzas', 'AjusteIngresoMensual', 'fuente__hogar'),
    'ingresos_extraordinarios': ('finanzas', 'IngresoExtraordinario', 'hogar'),
    'ingresos_reales': ('finanzas', 'IngresoRealMes', 'hogar'),
    'destinos_ingreso': ('finanzas', 'DestinoIngreso', 'hogar'),

    # Gastos
    'categorias_gasto': ('finanzas', 'CategoriaGasto', 'hogar'),
    'partidas_gasto': ('finanzas', 'PartidaGasto', 'hogar'),

    # Distribución
    'fondos': ('finanzas', 'FondoFamiliar', 'hogar'),
    'reglas_reparto': ('finanzas', 'ReglaReparto', 'hogar'),
    'subsobres': ('finanzas', 'SubsobreFondo', 'fondo__hogar'),
    'saldos_fondos': ('finanzas', 'SaldoRealFondo', 'fondo__hogar'),

    # Patrimonio inmobiliario
    'propiedades': ('finanzas', 'Propiedad', 'hogar'),
    'historial_propiedades': ('finanzas', 'HistorialPropiedad', 'propiedad__hogar'),

    # Registros mensuales
    'registros_mensuales': ('finanzas', 'RegistroMensual', 'usuario__userprofile__hogar'),

    # Extractos bancarios
    'extractos_bancarios': ('extractos', 'ExtractoBancario', 'hogar'),
    'movimientos_bancarios': ('extractos', 'MovimientoBancario', 'hogar'),

    # Tablas de referencia (sin datos personales)
    'tablas_irpf': ('finanzas', 'TablaIRPF', None),
    'cotizaciones_ss': ('finanzas', 'CotizacionSS', None),
    'catalogo_tickers': ('finanzas', 'TickerCatalogo', None),
}

# Campos que NUNCA se exponen al modelo — ni como valor devuelto, ni como
# clave de filtro/orden — aunque el objeto pertenezca al hogar del usuario.
CAMPOS_OCULTOS = {
    'password', 'api_key', 'avatar',
    'anthropic_api_key_cifrada', 'openai_api_key_cifrada', 'gemini_api_key_cifrada',
    'anthropic_api_key_hint', 'openai_api_key_hint', 'gemini_api_key_hint',
}

# Lookups de Django que pueden aparecer como último segmento de un filtro
# (p.ej. "importe__gte"): no son nombres de campo, así que se validan aparte.
LOOKUPS_CONOCIDOS = {
    'exact', 'iexact', 'contains', 'icontains', 'gt', 'gte', 'lt', 'lte', 'in',
    'startswith', 'istartswith', 'endswith', 'iendswith', 'range', 'isnull',
    'regex', 'iregex', 'year', 'month', 'day', 'week_day', 'date',
}


class ErrorRegistro(Exception):
    """Error al resolver una entidad o un lookup contra el registro de esquema."""


def obtener_modelo(entidad):
    """(modelo_cls, ruta_hogar) para una entidad registrada, o ErrorRegistro."""
    if entidad not in ENTIDADES:
        raise ErrorRegistro(
            f"Entidad '{entidad}' no disponible. Entidades válidas: {', '.join(sorted(ENTIDADES))}"
        )
    nombre_app, nombre_modelo, ruta_hogar = ENTIDADES[entidad]
    return apps.get_model(nombre_app, nombre_modelo), ruta_hogar


def filtrar_por_hogar(queryset, hogar, ruta_hogar):
    """Aplica el filtro de tenant declarado en el registro. `ruta_hogar=None`
    deja pasar la consulta sin filtrar (solo válido para tablas de referencia)."""
    if not ruta_hogar:
        return queryset
    valor = hogar.pk if ruta_hogar in ('id', 'pk') else hogar
    return queryset.filter(**{ruta_hogar: valor})


def campos_validos(modelo):
    return {c.name for c in modelo._meta.fields if c.name not in CAMPOS_OCULTOS}


def validar_lookup(modelo, lookup):
    """Valida un lookup estilo ORM ("categoria__nombre__icontains") recorriendo
    TODOS sus segmentos — no solo el primero — para que no se pueda atravesar
    una relación fuera del esquema permitido ni tocar un campo oculto en
    ningún punto del camino. Nunca lanza: devuelve True/False."""
    segmentos = (lookup or '').split('__')
    if not segmentos or not segmentos[0]:
        return False
    modelo_actual = modelo
    total = len(segmentos)
    indice = 0
    while indice < total:
        segmento = segmentos[indice]
        if segmento in CAMPOS_OCULTOS:
            return False
        try:
            campo = modelo_actual._meta.get_field(segmento)
        except Exception:
            # No es un campo real de este modelo: solo es válido si, a partir
            # de aquí, todo lo que queda son operadores de lookup conocidos
            # (p.ej. el "gte" de "importe__gte" ya consumido en el hop previo
            # no debería llegar aquí, pero cubre "fecha__year__gte" etc.).
            return all(s in LOOKUPS_CONOCIDOS for s in segmentos[indice:])
        es_ultimo = indice == total - 1
        if getattr(campo, 'is_relation', False):
            if es_ultimo:
                return True  # filtrar por la FK (id o instancia) está permitido
            modelo_actual = campo.related_model
            indice += 1
            continue
        if es_ultimo:
            return True  # campo escalar como último segmento: exact implícito
        # Campo escalar que no es el último segmento: lo que quede detrás
        # solo puede ser una cadena de operadores de lookup (p.ej. "gte").
        return all(s in LOOKUPS_CONOCIDOS for s in segmentos[indice + 1:])
    return True


def _serializar_valor(valor):
    if isinstance(valor, Decimal):
        return float(valor)
    if isinstance(valor, (datetime, date)):
        return valor.isoformat()
    if isinstance(valor, (bytes, memoryview)):
        return '<binario>'
    return valor


def serializar_instancia(obj):
    """Serialización DERIVADA de los campos reales del modelo: un campo nuevo
    se vuelve visible automáticamente sin tocar este módulo."""
    fila = {'id': obj.pk}
    for campo in obj._meta.fields:
        if campo.name == 'id' or campo.name in CAMPOS_OCULTOS:
            continue
        if campo.is_relation:
            relacionado = getattr(obj, campo.name, None)
            fila[campo.name] = str(relacionado) if relacionado else None
            fila[f'{campo.name}_id'] = getattr(obj, f'{campo.name}_id', None)
        else:
            fila[campo.name] = _serializar_valor(getattr(obj, campo.name, None))
    return fila


def campos_texto_de(modelo):
    """Nombres de campo Char/Text no ocultos — para búsqueda de texto libre."""
    return [
        c.name for c in modelo._meta.fields
        if c.get_internal_type() in ('CharField', 'TextField') and c.name not in CAMPOS_OCULTOS
    ]


def construir_q_texto(modelo, texto):
    q = Q()
    for campo in campos_texto_de(modelo):
        q |= Q(**{f'{campo}__icontains': texto})
    return q


def listar_entidades():
    return sorted(ENTIDADES)


# ─── Permisos ──────────────────────────────────────────────────────────────
# Household-level hoy (todo miembro del hogar ve los mismos datos); el hook
# queda aquí para poder afinar por rol en el futuro sin tocar las tools.

def puede_leer(usuario, entidad=None):
    profile = getattr(usuario, 'userprofile', None)
    return bool(profile and profile.hogar)


def puede_escribir(usuario):
    """Cualquier miembro no-viewer del hogar puede proponer escrituras. Se usa
    tanto al proponer como, de nuevo, al confirmar — el rol pudo cambiar."""
    profile = getattr(usuario, 'userprofile', None)
    return bool(profile and profile.hogar and not profile.es_viewer)
