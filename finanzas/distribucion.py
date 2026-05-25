"""
finanzas/distribucion.py
Motor de flujos financieros del hogar.

Cambios respecto a versión anterior:
  1. _calcular_neto_fuente(fuente, año, mes) — aplica AjusteIngresoMensual
     si existe para ese mes (inmutabilidad: no toca FuenteIngreso).
  2. calcular_flujos(hogar, año, mes) — acepta contexto temporal; por defecto
     usa el mes en curso.
  3. fondos_aportaciones enriquecido con sub-distribución (SubsobreFondo).
  4. resultado incluye ingresos_extraordinarios (pagas extra + periódicos).
"""

import datetime
from decimal import Decimal

from .models import (
    FuenteIngreso,
    PartidaGasto,
    ReglaReparto,
    FondoFamiliar,
    AjusteIngresoMensual,
    SubsobreFondo,
)
from .fiscal import calcular_neto_anual


# ─────────────────────────────────────────────────────────────────────────────
# HELPER PRIVADO
# ─────────────────────────────────────────────────────────────────────────────

def _calcular_neto_fuente(fuente: FuenteIngreso, año: int, mes: int):
    """
    Devuelve (neto_base_mensual, neto_ponderado_mensual, neto_anual_estimado).

    Si existe un AjusteIngresoMensual para este mes, ese importe sustituye
    al base mensual sin alterar el histórico del modelo ni el ponderado anual.

    El ponderado anual nunca se toca con overrides mensuales: refleja
    la estimación original y sirve para planificación a largo plazo.
    """
    bruto_anual = fuente.importe_anual_bruto
    estimado_anual = fuente.importe_anual_estimado

    # ── Comprobar override mensual ────────────────────────────────────────────
    override = AjusteIngresoMensual.objects.filter(
        fuente=fuente, año=año, mes=mes
    ).first()

    if fuente.es_bruto and bruto_anual > 0:
        resultado_fiscal = calcular_neto_anual(bruto_anual, fuente.pais_fiscal)
        ratio = resultado_fiscal['neto'] / bruto_anual

        if override:
            # El usuario declaró el neto real: lo usamos directamente
            neto_base = override.importe_real
        else:
            neto_base = (
                round(fuente.importe_mensual_base * ratio, 2)
                if fuente.es_mensual_recurrente and fuente.incluir_en_mensual
                else Decimal('0')
            )

        neto_pond = round(estimado_anual * ratio / Decimal('12'), 2)
        neto_anual = round(estimado_anual * ratio, 2)

    else:
        if override:
            neto_base = override.importe_real
        else:
            neto_base = (
                fuente.importe_mensual_base
                if fuente.es_mensual_recurrente and fuente.incluir_en_mensual
                else Decimal('0')
            )

        neto_pond = fuente.importe_mensual_ponderado
        neto_anual = estimado_anual

    return neto_base, neto_pond, neto_anual


def _ingresos_extraordinarios_mes(hogar, miembros, año: int, mes: int) -> list:
    """
    Detecta ingresos que caen en este mes pero no son mensuales recurrentes:
      - Pagas extra (mes en meses_pagas_extras de una fuente anual)
      - Ingresos de periodo no mensual cuyo mes de cobro coincide
    """
    resultado = []

    for m in miembros:
        fuentes = FuenteIngreso.objects.filter(
            usuario=m.user, hogar=hogar, activo=True
        )
        for f in fuentes:
            # Pagas extra — solo aplica a modo_entrada='anual' con num_pagas > 12
            if f.modo_entrada == 'anual' and f.num_pagas > 12:
                if mes in f.pagas_extras_meses:
                    # Calcular neto de la paga extra aplicando retenciones
                    bruto_paga = f.importe_declarado / Decimal(str(f.num_pagas))
                    if f.es_bruto and f.importe_anual_bruto > 0:
                        res = calcular_neto_anual(f.importe_anual_bruto, f.pais_fiscal)
                        ratio = res['neto'] / f.importe_anual_bruto
                        neto_paga = round(bruto_paga * ratio, 2)
                    else:
                        neto_paga = bruto_paga

                    resultado.append({
                        'tipo': 'paga_extra',
                        'label': f"Paga extra — {f.nombre}",
                        'miembro': m,
                        'fuente': f,
                        'importe': neto_paga,
                    })

            # Ingresos de periodo no mensual que se cobran este mes
            elif f.modo_entrada == 'periodo' and not f.es_mensual_recurrente:
                if mes in f.cobro_meses:
                    # El importe ya viene neto si es_bruto=False
                    if f.es_bruto and f.importe_anual_bruto > 0:
                        res = calcular_neto_anual(f.importe_anual_bruto, f.pais_fiscal)
                        ratio = res['neto'] / f.importe_anual_bruto
                        neto = round(f.importe_declarado * ratio, 2)
                    else:
                        neto = f.importe_declarado

                    resultado.append({
                        'tipo': 'ingreso_periodico',
                        'label': f"Ingreso periódico — {f.nombre}",
                        'miembro': m,
                        'fuente': f,
                        'importe': neto,
                    })

    return resultado


# ─────────────────────────────────────────────────────────────────────────────
# MOTOR PRINCIPAL
# ─────────────────────────────────────────────────────────────────────────────

def calcular_flujos(hogar, año: int = None, mes: int = None) -> dict:
    """
    Motor de flujos financieros del hogar para un mes concreto.

    Flujo:
      1. Ingresos por miembro (con overrides mensuales si los hay)
      2. Gastos individuales → se restan del disponible de cada uno
      3. Gastos del hogar → solo informativos
      4. Reglas de reparto → mueven dinero libre a fondos
      5. Sub-distribución interna de fondos (SubsobreFondo)
      6. Ingresos extraordinarios del mes (pagas extra, periódicos)
      7. Transferencias para Sankey
    """
    hoy = datetime.date.today()
    if not año:
        año = hoy.year
    if not mes:
        mes = hoy.month

    miembros = hogar.miembros.select_related('user').all()
    partidas = PartidaGasto.objects.filter(
        hogar=hogar, activo=True
    ).select_related('categoria', 'responsable')
    reglas = ReglaReparto.objects.filter(
        hogar=hogar, activo=True
    ).select_related('fondo', 'usuario')
    fondos = FondoFamiliar.objects.filter(hogar=hogar, activo=True)

    # ── PASO 1: Ingresos por miembro ─────────────────────────────────────────
    datos_miembros = {}
    total_base_hogar = Decimal('0')
    total_pond_hogar = Decimal('0')
    total_anual_hogar = Decimal('0')

    for m in miembros:
        fuentes = FuenteIngreso.objects.filter(
            usuario=m.user, hogar=hogar, activo=True
        )
        ing_base = Decimal('0')
        ing_pond = Decimal('0')
        ing_anual = Decimal('0')

        # Cargar overrides de este mes de una vez (evitar N+1)
        override_ids = set(
            AjusteIngresoMensual.objects.filter(
                fuente__in=fuentes, año=año, mes=mes
            ).values_list('fuente_id', flat=True)
        )

        for f in fuentes:
            b, p, a = _calcular_neto_fuente(f, año, mes)
            ing_base += b
            ing_pond += p
            ing_anual += a

        datos_miembros[m.user.id] = {
            'miembro': m,
            'ingreso_base': ing_base,
            'ingreso_ponderado': ing_pond,
            'ingreso_anual': ing_anual,
            'tiene_override': bool(override_ids),  # para mostrar badge en UI
            'gastos_individuales': Decimal('0'),
            'disponible_base': ing_base,
            'disponible_pond': ing_pond,
            'aportacion_hogar_informativa': Decimal('0'),
            'proporcion': Decimal('0'),
            'aportaciones_fondos': [],
            'libre_base': ing_base,
            'libre_ponderado': ing_pond,
        }
        total_base_hogar += ing_base
        total_pond_hogar += ing_pond
        total_anual_hogar += ing_anual

    # ── PASO 2: Gastos ───────────────────────────────────────────────────────
    gastos_hogar_fijos = Decimal('0')
    gastos_hogar_provision = Decimal('0')
    gastos_hogar_variables = Decimal('0')

    for p in partidas:
        mensual = p.importe_mensual
        tipo = p.categoria.tipo

        if p.responsable_id and p.responsable_id in datos_miembros:
            dm = datos_miembros[p.responsable_id]
            dm['gastos_individuales'] += mensual
            dm['disponible_base'] -= mensual
            dm['disponible_pond'] -= mensual
            dm['libre_base'] -= mensual
            dm['libre_ponderado'] -= mensual
        else:
            if tipo == 'fijo':
                gastos_hogar_fijos += mensual
            elif tipo == 'anual':
                gastos_hogar_provision += mensual
            else:
                gastos_hogar_variables += mensual

    total_gastos_hogar = gastos_hogar_fijos + gastos_hogar_provision + gastos_hogar_variables

    # Proporción informativa por miembro
    for uid, dm in datos_miembros.items():
        if total_base_hogar > 0:
            prop = dm['ingreso_base'] / total_base_hogar
        else:
            prop = (
                Decimal('1') / Decimal(str(len(datos_miembros)))
                if datos_miembros else Decimal('0')
            )
        dm['proporcion'] = round(prop * 100, 1)
        dm['aportacion_hogar_informativa'] = round(total_gastos_hogar * prop, 2)

    # ── PASO 3: Reglas de reparto ────────────────────────────────────────────
    fondos_aportaciones = {
        f.id: {
            'fondo': f,
            'total_aportacion_base': Decimal('0'),
            'total_aportacion_pond': Decimal('0'),
            'aportantes': [],
            'subsobres': [],           # se rellena en PASO 4
            'total_asignado': Decimal('0'),
            'remanente': Decimal('0'),
        }
        for f in fondos
    }

    for regla in reglas:
        if regla.usuario_id and regla.usuario_id in datos_miembros:
            # Regla individual
            dm = datos_miembros[regla.usuario_id]
            libre_b = dm['libre_base']
            libre_p = dm['libre_ponderado']

            if regla.tipo_regla == 'porcentaje':
                importe_b = round(libre_b * regla.porcentaje / 100, 2) if libre_b > 0 else Decimal('0')
                importe_p = round(libre_p * regla.porcentaje / 100, 2) if libre_p > 0 else Decimal('0')
            else:
                importe_b = regla.importe_fijo
                importe_p = regla.importe_fijo

            dm['libre_base'] -= importe_b
            dm['libre_ponderado'] -= importe_p
            dm['aportaciones_fondos'].append({
                'regla': regla,
                'fondo': regla.fondo,
                'importe_base': importe_b,
                'importe_pond': importe_p,
            })

            if regla.fondo_id and regla.fondo_id in fondos_aportaciones:
                fa = fondos_aportaciones[regla.fondo_id]
                fa['total_aportacion_base'] += importe_b
                fa['total_aportacion_pond'] += importe_p
                fa['aportantes'].append({
                    'miembro': dm['miembro'],
                    'importe_base': importe_b,
                    'importe_pond': importe_p,
                })

        else:
            # Regla global — reparto proporcional
            libre_b_total = sum(dm['libre_base'] for dm in datos_miembros.values())
            libre_p_total = sum(dm['libre_ponderado'] for dm in datos_miembros.values())

            if regla.tipo_regla == 'porcentaje':
                total_b = round(libre_b_total * regla.porcentaje / 100, 2) if libre_b_total > 0 else Decimal('0')
                total_p = round(libre_p_total * regla.porcentaje / 100, 2) if libre_p_total > 0 else Decimal('0')
            else:
                total_b = regla.importe_fijo
                total_p = regla.importe_fijo

            for uid, dm in datos_miembros.items():
                if total_base_hogar > 0:
                    prop = dm['ingreso_base'] / total_base_hogar
                else:
                    prop = Decimal('1') / Decimal(str(len(datos_miembros)))

                importe_b = round(total_b * prop, 2)
                importe_p = round(total_p * prop, 2)

                dm['libre_base'] -= importe_b
                dm['libre_ponderado'] -= importe_p
                dm['aportaciones_fondos'].append({
                    'regla': regla,
                    'fondo': regla.fondo,
                    'importe_base': importe_b,
                    'importe_pond': importe_p,
                })

                if regla.fondo_id and regla.fondo_id in fondos_aportaciones:
                    fa = fondos_aportaciones[regla.fondo_id]
                    fa['total_aportacion_base'] += importe_b
                    fa['total_aportacion_pond'] += importe_p
                    fa['aportantes'].append({
                        'miembro': dm['miembro'],
                        'importe_base': importe_b,
                        'importe_pond': importe_p,
                    })

    # ── PASO 4: Sub-distribución interna de fondos ───────────────────────────
    for fid, fa in fondos_aportaciones.items():
        fondo_obj = fa['fondo']
        subsobres_qs = SubsobreFondo.objects.filter(
            fondo=fondo_obj, activo=True
        ).prefetch_related('partidas_vinculadas')

        total_fondo = fa['total_aportacion_base']
        total_asignado = Decimal('0')
        subsobres_detalle = []

        for s in subsobres_qs:
            imp = s.importe_calculado
            total_asignado += imp
            pct = round(imp / total_fondo * 100, 1) if total_fondo > 0 else Decimal('0')
            subsobres_detalle.append({
                'subsobre': s,
                'importe': imp,
                'porcentaje_del_fondo': pct,
                'deficit': max(Decimal('0'), imp - total_fondo),  # si el fondo no cubre
            })

        fa['subsobres'] = subsobres_detalle
        fa['total_asignado'] = total_asignado
        fa['remanente'] = total_fondo - total_asignado

    # ── PASO 5: Ingresos extraordinarios del mes ─────────────────────────────
    ingresos_extraordinarios = _ingresos_extraordinarios_mes(hogar, miembros, año, mes)
    total_extraordinario = sum(i['importe'] for i in ingresos_extraordinarios)

    # ── PASO 6: Sankey (transferencias visuales) ──────────────────────────────
    transferencias = []
    sankey_nodes = []
    sankey_links = []
    node_index = {}

    def get_node(name, group, color='#a259ff'):
        if name not in node_index:
            node_index[name] = len(sankey_nodes)
            sankey_nodes.append({'name': name, 'group': group, 'color': color})
        return node_index[name]

    for uid, dm in datos_miembros.items():
        nombre = dm['miembro'].user.first_name or dm['miembro'].user.username
        src = get_node(nombre, 'ingreso', '#00ff88')

        for ap in dm['aportaciones_fondos']:
            fondo = ap['fondo']
            if fondo and ap['importe_base'] > 0:
                dst = get_node(fondo.nombre, 'fondo', fondo.color)
                sankey_links.append({
                    'source': src, 'target': dst,
                    'value': float(ap['importe_base']),
                })
                transferencias.append({
                    'origen': nombre,
                    'concepto': ap['regla'].nombre,
                    'destino': fondo.nombre,
                    'cuenta': fondo.cuenta_asociada,
                    'color': fondo.color,
                    'importe_base': ap['importe_base'],
                    'importe_pond': ap['importe_pond'],
                })

        if dm['libre_base'] > 0:
            dst_libre = get_node(f"Libre {nombre}", 'libre', '#555')
            sankey_links.append({
                'source': src, 'target': dst_libre,
                'value': float(dm['libre_base']),
            })

    # ── RESULTADO FINAL ───────────────────────────────────────────────────────
    return {
        # Contexto temporal
        'año': año,
        'mes': mes,

        # Ingresos
        'datos_miembros': list(datos_miembros.values()),
        'total_base_hogar': total_base_hogar,
        'total_pond_hogar': total_pond_hogar,
        'total_anual_hogar': total_anual_hogar,

        # Gastos
        'gastos_hogar_fijos': gastos_hogar_fijos,
        'gastos_hogar_provision': gastos_hogar_provision,
        'gastos_hogar_variables': gastos_hogar_variables,
        'total_gastos_hogar': total_gastos_hogar,

        # Fondos con sub-distribución
        'fondos_aportaciones': list(fondos_aportaciones.values()),

        # Extraordinarios
        'ingresos_extraordinarios': ingresos_extraordinarios,
        'total_extraordinario': total_extraordinario,

        # Sankey
        'transferencias': transferencias,
        'sankey_nodes': sankey_nodes,
        'sankey_links': sankey_links,
    }
