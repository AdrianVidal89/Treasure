"""
finanzas/distribucion.py — Motor de flujos financieros del hogar v2.

Cambios vs v1:
  1. Exporta total_gastos_individuales al resultado (Bug #2)
  2. Ingreso anual y extraordinarios con asignación a fondos (Bug #1, #5)
  3. Subsobres alimentan el Sankey (Bug #6)
  4. Fondos tipados (ahorro/inversion) para reporting (Bug #3)
  5. _neto_estimado_de_base() para mostrar neto en ajuste variable (Bug #4)
  6. Doble vista: mensual y anual simultánea
"""

import datetime
from decimal import Decimal

from .models import (
    AjusteIngresoMensual,
    FondoFamiliar,
    FuenteIngreso,
    IngresoExtraordinario,
    PartidaGasto,
    ReglaReparto,
    SubsobreFondo,
)
from .fiscal import calcular_neto_anual


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _ratio_neto(fuente):
    """Ratio neto/bruto para una fuente. 1.0 si no es bruto."""
    if fuente.es_bruto and fuente.importe_anual_bruto > 0:
        res = calcular_neto_anual(fuente.importe_anual_bruto, fuente.pais_fiscal)
        return res['neto'] / fuente.importe_anual_bruto
    return Decimal('1')


def _calcular_neto_fuente(fuente, año, mes):
    """
    Devuelve (neto_base_mensual, neto_ponderado_mensual, neto_anual_estimado).
    Aplica AjusteIngresoMensual si existe.
    """
    ratio = _ratio_neto(fuente)

    # Override mensual
    override = AjusteIngresoMensual.objects.filter(
        fuente=fuente, año=año, mes=mes
    ).first()

    if override:
        neto_base = override.importe_real
    else:
        if fuente.es_mensual_recurrente and fuente.incluir_en_mensual:
            neto_base = round(fuente.importe_mensual_base * ratio, 2)
        else:
            neto_base = Decimal('0')

    neto_pond = round(fuente.importe_anual_estimado * ratio / Decimal('12'), 2)
    neto_anual = round(fuente.importe_anual_estimado * ratio, 2)

    return neto_base, neto_pond, neto_anual


def neto_estimado_de_base(fuente):
    """
    Devuelve el neto estimado de importe_mensual_base.
    Usado en el formulario de ajuste variable para mostrar referencia neta, no bruta.
    """
    ratio = _ratio_neto(fuente)
    return round(fuente.importe_mensual_base * ratio, 2)


def _ingresos_del_mes(hogar, miembros, año, mes):
    """
    Detecta ingresos no-mensuales que caen este mes:
    - Pagas extra (modo anual, num_pagas > 12, mes en meses_pagas_extras)
    - Ingresos periódicos no mensuales (modo periodo, mes en cobro_meses)
    - IngresoExtraordinario puntuales
    """
    resultado = []

    for m in miembros:
        fuentes = FuenteIngreso.objects.filter(
            usuario=m.user, hogar=hogar, activo=True
        )
        for f in fuentes:
            ratio = _ratio_neto(f)

            # Pagas extra
            if f.modo_entrada == 'anual' and f.num_pagas > 12:
                if mes in f.pagas_extras_meses:
                    bruto_paga = f.importe_declarado / Decimal(str(f.num_pagas))
                    resultado.append({
                        'tipo': 'paga_extra',
                        'label': f"Paga extra — {f.nombre}",
                        'miembro': m,
                        'fuente': f,
                        'importe': round(bruto_paga * ratio, 2),
                        'fondo_destino': None,
                    })

            # Ingresos periódicos no mensuales
            elif f.modo_entrada == 'periodo' and not f.es_mensual_recurrente:
                if mes in f.cobro_meses:
                    resultado.append({
                        'tipo': 'ingreso_periodico',
                        'label': f"Ingreso periódico — {f.nombre}",
                        'miembro': m,
                        'fuente': f,
                        'importe': round(f.importe_declarado * ratio, 2),
                        'fondo_destino': None,
                    })

    # Ingresos extraordinarios puntuales
    extras = IngresoExtraordinario.objects.filter(
        hogar=hogar, año=año, mes=mes
    ).select_related('usuario', 'fondo_destino')

    for e in extras:
        miembro = next((m for m in miembros if m.user_id == e.usuario_id), None)
        resultado.append({
            'tipo': 'extraordinario',
            'label': e.concepto,
            'miembro': miembro,
            'fuente': None,
            'importe': e.importe,
            'fondo_destino': e.fondo_destino,
            'nota': e.nota,
        })

    return resultado


# ─────────────────────────────────────────────────────────────────────────────
# MOTOR PRINCIPAL
# ─────────────────────────────────────────────────────────────────────────────

def calcular_flujos(hogar, año=None, mes=None):
    """
    Motor de flujos financieros.

    Flujo:
      1. Ingresos mensuales por miembro (con overrides)
      2. Gastos individuales → se restan del disponible
      3. Gastos del hogar → informativos por tipo (fijo/variable/discrecional/anual)
      4. Reglas de reparto → mueven dinero libre a fondos
      5. Sub-distribución interna de fondos (SubsobreFondo)
      6. Ingresos extraordinarios del mes
      7. Resumen ahorro / inversión por tipo_fondo
      8. Sankey con subsobres
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
            'tiene_override': bool(override_ids),
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
    total_gastos_individuales = Decimal('0')

    # Desglose para UI
    gastos_por_tipo = {
        'fijo': {'items': [], 'total': Decimal('0')},
        'anual': {'items': [], 'total': Decimal('0')},
        'variable': {'items': [], 'total': Decimal('0')},
    }

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
            total_gastos_individuales += mensual
        else:
            # Gasto del hogar
            gastos_por_tipo.setdefault(tipo, {'items': [], 'total': Decimal('0')})
            gastos_por_tipo[tipo]['items'].append({
                'partida': p,
                'mensual': mensual,
                'anual': p.importe_anual,
            })
            gastos_por_tipo[tipo]['total'] += mensual

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
            'subsobres': [],
            'total_asignado': Decimal('0'),
            'remanente': Decimal('0'),
            'ingresos_extra_asignados': [],
            'total_extra': Decimal('0'),
        }
        for f in fondos
    }

    for regla in reglas:
        if regla.usuario_id and regla.usuario_id in datos_miembros:
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

    # ── PASO 4: Ingresos extraordinarios del mes ─────────────────────────────
    ingresos_extraordinarios = _ingresos_del_mes(hogar, miembros, año, mes)
    total_extraordinario = sum(i['importe'] for i in ingresos_extraordinarios)

    # Asignar extraordinarios a fondos si tienen fondo_destino
    for ext in ingresos_extraordinarios:
        fd = ext.get('fondo_destino')
        if fd and fd.id in fondos_aportaciones:
            fa = fondos_aportaciones[fd.id]
            fa['ingresos_extra_asignados'].append(ext)
            fa['total_extra'] += ext['importe']

    # ── PASO 5: Sub-distribución interna de fondos ───────────────────────────
    for fid, fa in fondos_aportaciones.items():
        fondo_obj = fa['fondo']
        subsobres_qs = SubsobreFondo.objects.filter(
            fondo=fondo_obj, activo=True
        ).prefetch_related('partidas_vinculadas')

        total_fondo = fa['total_aportacion_base'] + fa['total_extra']
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
            })

        fa['subsobres'] = subsobres_detalle
        fa['total_asignado'] = total_asignado
        fa['remanente'] = total_fondo - total_asignado

    # ── PASO 6: Resumen ahorro / inversión ───────────────────────────────────
    resumen_destinos = {
        'ahorro': Decimal('0'),
        'inversion': Decimal('0'),
        'emergencia': Decimal('0'),
    }
    for fid, fa in fondos_aportaciones.items():
        tipo = fa['fondo'].tipo_fondo if hasattr(fa['fondo'], 'tipo_fondo') else 'comun'
        total_fondo_mes = fa['total_aportacion_base'] + fa['total_extra']
        if tipo in resumen_destinos:
            resumen_destinos[tipo] += total_fondo_mes

    total_ahorro_inversion = resumen_destinos['ahorro'] + resumen_destinos['inversion'] + resumen_destinos['emergencia']

    # Tasa de ahorro
    tasa_ahorro = round(total_ahorro_inversion / total_base_hogar * 100, 1) if total_base_hogar > 0 else Decimal('0')

    # Semáforo
    if tasa_ahorro >= 20:
        semaforo = 'verde'
        semaforo_texto = 'Excelente capacidad de ahorro'
    elif tasa_ahorro >= 10:
        semaforo = 'amarillo'
        semaforo_texto = 'Ahorro moderado — margen de mejora'
    elif tasa_ahorro > 0:
        semaforo = 'naranja'
        semaforo_texto = 'Ahorro bajo — revisar gastos'
    else:
        semaforo = 'rojo'
        semaforo_texto = 'Sin ahorro — atención'

    # ── PASO 7: Presupuesto anual ────────────────────────────────────────────
    presupuesto_anual = {
        'ingresos_netos': total_anual_hogar,
        'gastos_hogar_anuales': total_gastos_hogar * 12,
        'gastos_individuales_anuales': total_gastos_individuales * 12,
        'ahorro_inversion_anual': total_ahorro_inversion * 12,
        'libre_anual': (total_base_hogar - total_gastos_hogar - total_gastos_individuales - total_ahorro_inversion) * 12,
    }

    # ── PASO 8: Sankey ───────────────────────────────────────────────────────
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

        # Gastos individuales en Sankey
        if dm['gastos_individuales'] > 0:
            dst_gi = get_node(f"Gastos {nombre}", 'gasto', '#ff4d4d')
            sankey_links.append({
                'source': src, 'target': dst_gi,
                'value': float(dm['gastos_individuales']),
            })

        # Aportaciones a fondos
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
                    'tipo_fondo': getattr(fondo, 'tipo_fondo', 'comun'),
                    'importe_base': ap['importe_base'],
                    'importe_pond': ap['importe_pond'],
                })

        # Libre personal
        if dm['libre_base'] > 0:
            dst_libre = get_node(f"Libre {nombre}", 'libre', '#00d1ff')
            sankey_links.append({
                'source': src, 'target': dst_libre,
                'value': float(dm['libre_base']),
            })

    # Subsobres en Sankey: fondo → subsobre
    for fid, fa in fondos_aportaciones.items():
        fondo = fa['fondo']
        total_fondo = fa['total_aportacion_base'] + fa['total_extra']
        if total_fondo <= 0:
            continue

        fondo_node = node_index.get(fondo.nombre)
        if fondo_node is None:
            continue

        for ss_data in fa['subsobres']:
            ss = ss_data['subsobre']
            imp = float(ss_data['importe'])
            if imp > 0:
                dst_ss = get_node(f"{ss.nombre}", 'subsobre', fondo.color)
                sankey_links.append({
                    'source': fondo_node, 'target': dst_ss,
                    'value': imp,
                })

        # Remanente del fondo
        rem = float(fa['remanente'])
        if rem > 0 and fa['subsobres']:
            dst_rem = get_node(f"Libre {fondo.nombre}", 'libre', '#555')
            sankey_links.append({
                'source': fondo_node, 'target': dst_rem,
                'value': rem,
            })

    # ── RESULTADO ─────────────────────────────────────────────────────────────
    return {
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
        'total_gastos_individuales': total_gastos_individuales,
        'gastos_por_tipo': gastos_por_tipo,

        # Fondos con sub-distribución
        'fondos_aportaciones': list(fondos_aportaciones.values()),

        # Extraordinarios
        'ingresos_extraordinarios': ingresos_extraordinarios,
        'total_extraordinario': total_extraordinario,

        # Ahorro / Inversión
        'resumen_destinos': resumen_destinos,
        'total_ahorro_inversion': total_ahorro_inversion,
        'tasa_ahorro': tasa_ahorro,
        'semaforo': semaforo,
        'semaforo_texto': semaforo_texto,

        # Presupuesto anual
        'presupuesto_anual': presupuesto_anual,

        # Sankey
        'transferencias': transferencias,
        'sankey_nodes': sankey_nodes,
        'sankey_links': sankey_links,
    }
