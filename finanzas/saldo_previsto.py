"""Cómo quedará cada fondo al final del mes, partiendo de su saldo real.

Distribución dice lo que entra y sale de cada fondo; Evolución sabe cuánto
tiene. Juntando las dos se ve lo que de verdad le importa a una familia: «el
depósito tiene 85.882 €, este mes le entran 200, se quedará en 86.082».

El punto de partida es el último mes ANTERIOR con saldos apuntados (o el valor
automático de un depósito), y es una aproximación: no sabe de gastos que se
salen del presupuesto ni de dinero que entre de fuera. Si el mes que se mira ya
tiene su saldo apuntado, se enseña al lado para comparar.
"""

from decimal import Decimal

from .models import SaldoRealFondo

MESES = ['', 'enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio',
         'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre']
_CERO = Decimal('0')


def _mes_anterior(anio, mes):
    return (anio - 1, 12) if mes == 1 else (anio, mes - 1)


def _ultimo_mes_con_saldos(hogar, anio, mes):
    """El último (año, mes) estrictamente anterior a `anio/mes` con algún
    saldo apuntado, o None."""
    previo = (
        SaldoRealFondo.objects
        .filter(fondo__hogar=hogar)
        .filter(_antes_de(anio, mes))
        .order_by('-año', '-mes')
        .values_list('año', 'mes')
        .first()
    )
    return previo


def _antes_de(anio, mes):
    from django.db.models import Q
    return Q(año__lt=anio) | Q(año=anio, mes__lt=mes)


def saldos_previstos(hogar, anio, mes, fondos_aportaciones):
    """{fondo_id: {...}} con el saldo de partida, el movimiento del mes y el
    saldo previsto al cierre de cada fondo que aparece en el reparto."""
    from .views_evolucion import celdas_del_mes, saldos_del_mes

    base_mes = _ultimo_mes_con_saldos(hogar, anio, mes)
    if base_mes:
        celdas, _, _, _ = celdas_del_mes(hogar, *base_mes)
    else:
        # Sin ningún saldo apuntado: solo lo que valen los depósitos.
        base_mes = _mes_anterior(anio, mes)
        celdas, _, _, _ = celdas_del_mes(hogar, *base_mes, saldos=[])
    base = {c['fondo'].id: c for c in celdas}

    # Lo apuntado este mismo mes (no lo arrastrado de meses anteriores).
    apuntado = {
        s.fondo_id: s.saldo for s in saldos_del_mes(hogar, anio, mes)
        if not getattr(s, 'arrastrado_de', None)
    }

    nombre_base = MESES[base_mes[1]] + ('' if base_mes[0] == anio else f' {base_mes[0]}')
    resultado = {}
    for fa in fondos_aportaciones:
        fondo = fa['fondo']
        fid = getattr(fondo, 'id', None)
        if fid is None:
            continue
        movimiento = (
            (fa.get('total_aportacion_base') or _CERO)
            - (fa.get('gastos_cubiertos') or _CERO)
            - (fa.get('total_cascada_saliente') or _CERO)
        )
        celda = base.get(fid) or {}
        saldo_base = celda.get('saldo_valor')
        final = (saldo_base + movimiento) if saldo_base is not None else None
        resultado[fid] = {
            'base': saldo_base,
            'base_mes': nombre_base,
            'base_arrastrado': celda.get('arrastrado_de', ''),
            'auto': bool(celda.get('auto_deposito')),
            'movimiento': movimiento,
            'final': final,
            'apuntado': apuntado.get(fid),
            'diferencia': (apuntado[fid] - final) if (fid in apuntado and final is not None) else None,
        }
    return resultado
