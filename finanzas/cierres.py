"""Cierre de meses: Evolución es un registro histórico, no una vista en vivo.

El motor de distribución (`calcular_flujos`) calcula siempre con la
configuración de HOY: sueldos, gastos y reglas tal y como están ahora mismo.
Eso está bien para el mes en curso y para proyectar, pero no para el pasado:
si hoy subes el sueldo, julio no puede empezar a ingresar más de lo que
ingresó.

Aquí se congelan las cifras de los meses ya cerrados en `CierreMensual`. A
partir de ese momento Evolución las lee de la foto y deja de recalcularlas.

Cuándo se congela un mes:
  · Al abrir Evolución, si el mes ya está cerrado y aún no tiene foto.
  · ANTES de tocar los ingresos, los gastos o las reglas de reparto (señales
    en `signals.py`), que es lo que de verdad evita el problema: la foto se
    hace con los valores de antes del cambio, no con los nuevos.

Un mes está cerrado cuando ya ha pasado: (año, mes) < (año, mes) de hoy.

Además de los totales, el cierre guarda la foto COMPLETA del reparto
(`detalle`): ingresos de cada uno fuente a fuente, reglas tal y como se
aplicaron, transferencias y fondos. Un mes cerrado se enseña siempre desde
esa foto: cambiar hoy una regla, un fondo o un sueldo vale de ahora en
adelante, nunca hacia atrás.
"""

import datetime
import re
from decimal import Decimal
from types import SimpleNamespace

from django.db import IntegrityError

from .models import CierreMensual

# Claves del motor de distribución que Evolución trata como histórico. El
# resto de claves siguen siendo en vivo: no forman parte del registro.
CAMPOS_CONGELADOS = {
    'ingreso': 'ingreso_base_hogar',
    'ingreso_previsto': 'ingreso_base_puro_hogar',
    'gastos': 'total_gastos_all',
    'inversion': 'total_inversion',
    'ahorro': 'total_ahorro',
    'libre': 'libre_total',
}

# Lo que se guarda del flujo en la foto del mes, además de los totales.
CLAVES_DETALLE = (
    'datos_miembros', 'fondos_aportaciones', 'transferencias_cascada',
    'calculos_cubrir', 'reglas_aplicadas', 'ingresos_fuera_reparto',
    'total_fuera_reparto', 'puntuales', 'aporte_puntuales', 'puntuales_en_aporte',
    'gastos_hogar_total', 'total_gastos_individuales', 'total_gastos_cubiertos',
    'libre_personal', 'libre_comun',
)
_IMPORTE = re.compile(r'^-?\d+\.\d{2}$')
_CENT = Decimal('0.01')


def _plano(v):
    """El flujo a JSON. Los importes van como texto con dos decimales (para
    no perder céntimos) y los objetos de la base de datos se copian con lo que
    la pantalla necesita: si mañana se borra un fondo, la foto no se rompe."""
    from django.contrib.auth.models import User
    from .models import FondoFamiliar, ReglaReparto

    if isinstance(v, Decimal):
        return str(v.quantize(_CENT))
    if isinstance(v, dict):
        return {k: _plano(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plano(x) for x in v]
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, User):
        return {'__obj': 1, 'id': v.id, 'first_name': v.first_name, 'username': v.username}
    if isinstance(v, FondoFamiliar):
        return {'__obj': 1, 'id': v.id, 'nombre': v.nombre, 'color': v.color,
                'tipo_fondo': v.tipo_fondo, 'cuenta_asociada': v.cuenta_asociada}
    if isinstance(v, ReglaReparto):
        return {'__obj': 1, 'id': v.id, 'nombre': v.nombre, 'color': v.color}
    if isinstance(getattr(v, 'user', None), User):
        return {'__obj': 1, 'user': _plano(v.user)}
    return str(v)


def _vivo(v):
    """La foto de vuelta a algo que la pantalla y Evolución leen igual que un
    flujo en vivo: importes como Decimal y objetos con atributos."""
    if isinstance(v, dict):
        datos = {k: _vivo(x) for k, x in v.items() if k != '__obj'}
        return SimpleNamespace(**datos) if v.get('__obj') else datos
    if isinstance(v, list):
        return [_vivo(x) for x in v]
    if isinstance(v, str) and _IMPORTE.match(v):
        return Decimal(v)
    return v


def _detalle_de(flujo):
    return _plano({k: flujo[k] for k in CLAVES_DETALLE if k in flujo})


def _cuadrar_ingresos(flujo, total):
    """Ajusta lo que ingresó cada uno para que sume el total registrado.

    Solo para fotos de meses que se cerraron antes de que se guardara el
    detalle: el total es el de entonces y el reparto por persona se calcula
    hoy. Lo DECLARADO de cada fuente es un dato y no se toca; la diferencia
    se reparte solo entre lo que se calculó por previsión, y lo que se suma o
    resta a alguien se le suma o resta también de lo que le quedó libre."""
    datos = flujo['datos_miembros']
    declarado = {id(dm): sum((f['importe'] for f in dm.get('por_fuente', []) if f['declarado']), Decimal('0'))
                 for dm in datos}
    previsto = {id(dm): dm['ingreso_base'] - declarado[id(dm)] for dm in datos}
    suma_prev = sum(previsto.values(), Decimal('0'))
    diferencia = Decimal(total) - sum((dm['ingreso_base'] for dm in datos), Decimal('0'))
    if not diferencia or suma_prev <= 0:
        return
    for dm in datos:
        delta = (diferencia * previsto[id(dm)] / suma_prev).quantize(_CENT)
        dm['ingreso_base'] += delta
        if 'libre_base' in dm:
            dm['libre_base'] += delta
    total = Decimal(total)
    for dm in datos:
        dm['pct_ingreso'] = round(dm['ingreso_base'] / total * 100, 2) if total > 0 else Decimal('0')


def detalle_vivo(cierre):
    return _vivo(cierre.detalle) if cierre and cierre.detalle else None


def mes_cerrado(año, mes, hoy=None):
    """True si ese mes ya pasó (el mes en curso NO está cerrado)."""
    hoy = hoy or datetime.date.today()
    return (año, mes) < (hoy.year, hoy.month)


def meses_cerrados_de(año, hoy=None):
    """Meses de `año` que ya están cerrados, en orden."""
    hoy = hoy or datetime.date.today()
    if año > hoy.year:
        return []
    if año < hoy.year:
        return list(range(1, 13))
    return list(range(1, hoy.month))


def _valores_de(flujo):
    return {campo: flujo[clave] for campo, clave in CAMPOS_CONGELADOS.items()}


def _mes_vacio(valores):
    """Un mes sin ingresos, sin gastos y sin inversión no es un registro: es un
    hogar que todavía no ha configurado nada. Congelarlo dejaría meses a cero
    para siempre en cuanto alguien diera de alta su primera nómina."""
    return all(not v for v in valores.values())


def congelar_mes(hogar, año, mes, flujo=None, forzar=False):
    """Congela un mes cerrado. Si ya tiene foto no la toca, salvo `forzar`
    (que es lo que hace una corrección explícita del usuario sobre ese mes).
    Devuelve el CierreMensual, o None si el mes aún no está cerrado."""
    if not mes_cerrado(año, mes):
        return None

    existente = CierreMensual.objects.filter(hogar=hogar, año=año, mes=mes).first()
    if existente and existente.detalle and not forzar:
        return None

    if flujo is None:
        from .distribucion import calcular_flujos
        flujo = calcular_flujos(hogar, mes=mes, anio=año)

    if existente and not forzar:
        # Foto antigua, solo con totales: se le añade el detalle sin tocar
        # las cifras que ya quedaron registradas, cuadrando con ellas.
        _cuadrar_ingresos(flujo, existente.ingreso)
        existente.detalle = _detalle_de(flujo)
        existente.save(update_fields=['detalle', 'actualizado_en'])
        return None

    valores = _valores_de(flujo)
    if _mes_vacio(valores):
        return None
    valores['detalle'] = _detalle_de(flujo)

    try:
        cierre, _ = CierreMensual.objects.update_or_create(
            hogar=hogar, año=año, mes=mes, defaults=valores,
        )
    except IntegrityError:
        # Otra petición congeló este mes a la vez (la vista y su llamada AJAX
        # se solapan en la primera carga). Su foto vale igual que la nuestra.
        return CierreMensual.objects.filter(hogar=hogar, año=año, mes=mes).first()
    return cierre


def congelar_meses_cerrados(hogar, año=None, flujos_por_mes=None):
    """Congela los meses ya cerrados de `año` que todavía no tengan foto.

    `flujos_por_mes` evita recalcular si quien llama ya tiene los flujos del
    año a mano (el caso de la vista de Evolución)."""
    año = año or datetime.date.today().year
    pendientes = set(meses_cerrados_de(año)) - set(
        CierreMensual.objects.filter(hogar=hogar, año=año, detalle__isnull=False)
        .values_list('mes', flat=True)
    )
    for mes in sorted(pendientes):
        congelar_mes(hogar, año, mes,
                     flujo=(flujos_por_mes or {}).get(mes))


def _aplicar_cierre_a_flujo(flujo, cierre):
    """Mete en un flujo las cifras registradas de su cierre y recalcula lo que
    se deriva de ellas (tasa de ahorro, semáforo, porcentajes), para que la
    pantalla no mezcle cifras congeladas con porcentajes en vivo.

    Un campo a None es un cierre tomado antes de que ese campo existiera: se
    respeta el valor en vivo en vez de inventar un cero."""
    from .distribucion import clasificar_salud

    for campo, clave in CAMPOS_CONGELADOS.items():
        valor = getattr(cierre, campo, None)
        if valor is not None:
            flujo[clave] = valor

    detalle = detalle_vivo(cierre)
    if detalle:
        flujo.update(detalle)
    flujo['detalle_congelado'] = bool(detalle)

    ingreso = flujo['ingreso_base_hogar']
    gastos = flujo['total_gastos_all']

    def pct(val):
        return round(float(val / ingreso * 100), 1) if ingreso > 0 else 0

    tasa = round((ingreso - gastos) / ingreso * 100, 1) if ingreso > 0 else Decimal('0')
    semaforo, semaforo_texto = clasificar_salud(tasa)
    flujo['tasa_ahorro'] = tasa
    flujo['semaforo'] = semaforo
    flujo['semaforo_texto'] = semaforo_texto
    flujo['pct_gastos'] = pct(gastos)
    flujo['pct_ahorro'] = pct(flujo['total_ahorro'])
    flujo['pct_inversion'] = pct(flujo['total_inversion'])
    flujo['pct_libre'] = pct(flujo['libre_total'])
    flujo['mes_cerrado'] = True
    return flujo


def aplicar_cierres(hogar, año, flujos_por_mes):
    """Sustituye en los meses CERRADOS las cifras del motor por las de su foto.

    Los meses que aún no habían quedado registrados se congelan aquí mismo con
    lo que dice el motor: es lo mejor que tenemos para ellos, y a partir de
    ahora ya no se moverán. El mes en curso y los futuros se dejan en vivo."""
    congelar_meses_cerrados(hogar, año, flujos_por_mes)

    cierres = {
        c.mes: c for c in CierreMensual.objects.filter(hogar=hogar, año=año)
    }
    for mes, flujo in flujos_por_mes.items():
        cierre = cierres.get(mes)
        if cierre is None or not mes_cerrado(año, mes):
            continue
        _aplicar_cierre_a_flujo(flujo, cierre)
    return flujos_por_mes


def flujo_del_mes(hogar, mes, anio):
    """El flujo de UN mes, ya con la regla de histórico aplicada: en vivo si es
    el mes en curso o uno futuro; con las cifras registradas si ya está cerrado.

    Es el punto de entrada que deben usar las pantallas que pueden enseñar un
    mes pasado, en lugar de llamar a `calcular_flujos` directamente."""
    from .distribucion import calcular_flujos

    flujo = calcular_flujos(hogar, mes=mes, anio=anio)
    flujo['mes_cerrado'] = False
    if not mes_cerrado(anio, mes):
        return flujo

    congelar_mes(hogar, anio, mes, flujo=flujo)
    cierre = CierreMensual.objects.filter(hogar=hogar, año=anio, mes=mes).first()
    if cierre is None:
        # Mes cerrado sin nada que registrar (hogar aún sin configurar).
        return flujo
    return _aplicar_cierre_a_flujo(flujo, cierre)


def corregir_ingreso_cerrado(hogar, año, mes):
    """Declarar (o borrar) lo que se cobró en un mes ya cerrado.

    Es un dato de ese mes, así que la foto se corrige, pero SOLO en el
    ingreso: las reglas, las transferencias y los fondos fueron los que
    fueron. Lo que alguien cobró de más o de menos va a lo que le quedó libre.
    Si el cierre no tiene detalle se rehace entero, como antes."""
    from .models import AjusteIngresoMensual

    if not mes_cerrado(año, mes):
        return None
    cierre = CierreMensual.objects.filter(hogar=hogar, año=año, mes=mes).first()
    if cierre is None or not cierre.detalle:
        return congelar_mes(hogar, año, mes, forzar=True)

    ajustes = {
        a.fuente_id: a.importe_real
        for a in AjusteIngresoMensual.objects.filter(año=año, mes=mes, fuente__hogar=hogar)
    }
    detalle = cierre.detalle
    total_delta = Decimal('0')
    for dm in detalle.get('datos_miembros', []):
        nuevo = Decimal('0')
        ajustes_mes = []
        for f in dm.get('por_fuente', []):
            declarado = f['id'] in ajustes
            importe = ajustes[f['id']] if declarado else Decimal(f['previsto'])
            f['importe'], f['declarado'] = _plano(importe), declarado
            nuevo += importe
            if declarado:
                ajustes_mes.append({'fuente_id': f['id'], 'fuente': f['nombre'],
                                    'importe_ajustado': _plano(importe),
                                    'importe_base': f['previsto']})
        if not dm.get('por_fuente'):
            continue
        delta = nuevo - Decimal(dm['ingreso_base'])
        total_delta += delta
        dm['ingreso_base'] = _plano(nuevo)
        dm['libre_base'] = _plano(Decimal(dm['libre_base']) + delta)
        dm['ajustes_mes'] = ajustes_mes
        n_decl = len(ajustes_mes)
        dm['origen_ingreso'] = ('declarado' if n_decl == len(dm['por_fuente'])
                                else 'mixto' if n_decl else 'previsto')

    if not total_delta:
        cierre.detalle = detalle
        cierre.save(update_fields=['detalle', 'actualizado_en'])
        return cierre

    cierre.ingreso += total_delta
    if cierre.libre is not None:
        cierre.libre += total_delta
    detalle['libre_personal'] = _plano(Decimal(detalle.get('libre_personal') or '0') + total_delta)
    total = cierre.ingreso
    for dm in detalle.get('datos_miembros', []):
        ingreso = Decimal(dm['ingreso_base'])
        dm['pct_ingreso'] = _plano(round(ingreso / total * 100, 2) if total > 0 else Decimal('0'))
    cierre.detalle = detalle
    cierre.save()
    return cierre
