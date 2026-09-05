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
"""

import datetime
from decimal import Decimal

from django.db import IntegrityError

from .models import CierreMensual

# Claves del motor de distribución que Evolución trata como histórico. El
# resto de claves siguen siendo en vivo: no forman parte del registro.
CAMPOS_CONGELADOS = {
    'ingreso': 'ingreso_base_hogar',
    'gastos': 'total_gastos_all',
    'inversion': 'total_inversion',
    'ahorro': 'total_ahorro',
    'libre': 'libre_total',
}


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

    if not forzar and CierreMensual.objects.filter(hogar=hogar, año=año, mes=mes).exists():
        return None

    if flujo is None:
        from .distribucion import calcular_flujos
        flujo = calcular_flujos(hogar, mes=mes, anio=año)

    valores = _valores_de(flujo)
    if _mes_vacio(valores):
        return None

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
        CierreMensual.objects.filter(hogar=hogar, año=año).values_list('mes', flat=True)
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

    cierre = (congelar_mes(hogar, anio, mes, flujo=flujo)
              or CierreMensual.objects.filter(hogar=hogar, año=anio, mes=mes).first())
    if cierre is None:
        # Mes cerrado sin nada que registrar (hogar aún sin configurar).
        return flujo
    return _aplicar_cierre_a_flujo(flujo, cierre)
