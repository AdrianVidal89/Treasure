"""
finanzas/distribucion.py — Motor de flujos financieros v3.

Cambios vs v2:
  - Paso 4b: SubsobreFondo con fondo_destino → cascada entre fondos
  - Paso 3: Reglas con periodicidad_regla='anual' operan sobre extraordinarios
  - _ratio_neto reutilizado en todos los cálculos
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
    if fuente.es_bruto and fuente.importe_anual_bruto > 0:
        res = calcular_neto_anual(fuente.importe_anual_bruto, fuente.pais_fiscal)
        return res['neto'] / fuente.importe_anual_bruto
    return Decimal('1')


def _calcular_neto_fuente(fuente, año, mes):
    ratio = _ratio_neto(fuente)
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
    ratio = _ratio_neto(fuente)
    return round(fuente.importe_mensual_base * ratio, 2)


def _ingresos_del_mes(hogar, miembros, año, mes):
    """Pagas extras + periódicos no mensuales + IngresoExtraordinario."""
    resultado = []

    for m in miembros:
        fuentes = FuenteIngreso.objects.filter(
            usuario=m.user, hogar=hogar, activo=True
        )
        for f in fuentes:
            ratio = _ratio_neto(f)

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
                        'usuario_id': m.user.id,
                    })

            elif f.modo_entrada == 'periodo' and not f.es_mensual_recurrente:
                if mes in f.cobro_meses:
                    resultado.append({
                        'tipo': 'ingreso_periodico',
                        'label': f"Ingreso periódico — {f.nombre}",
                        'miembro': m,
                        'fuente': f,
                        'importe': round(f.importe_declarado * ratio, 2),
                        'fondo_destino': None,
                        'usuario_id': m.user.id,
                    })

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
            'usuario_id': e.usuario_id,
            'extra_id': e.id,
        })

    return resultado


# ─────────────────────────────────────────────────────────────────────────────
# MOTOR PRINCIPAL
# ─────────────────────────────────────────────────────────────────────────────

def calcular_flujos(hogar, año=None, mes=None):
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

    # Separar reglas mensuales y anuales
    reglas_mensuales = [r for r in reglas if getattr(r, 'periodicidad_regla', 'mensual') == 'mensual']
    reglas_anuales = [r for r in reglas if getattr(r, 'periodicidad_regla', 'mensual') == 'anual']

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
            'aportaciones_anuales': [],
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
            gastos_por_tipo.setdefault(tipo, {'items': [], 'total': Decimal('0')})
            gastos_por_tipo[tipo]['items'].append({
                'partida': p, 'mensual': mensual, 'anual': p.importe_anual,
            })
            gastos_por_tipo[tipo]['total'] += mensual
            if tipo == 'fijo':
                gastos_hogar_fijos += mensual
            elif tipo == 'anual':
                gastos_hogar_provision += mensual
            else:
                gastos_hogar_variables += mensual

    total_gastos_hogar = gastos_hogar_fijos + gastos_hogar_provision + gastos_hogar_variables

    for uid, dm in datos_miembros.items():
        if total_base_hogar > 0:
            prop = dm['ingreso_base'] / total_base_hogar
        else:
            prop = Decimal('1') / Decimal(str(len(datos_miembros))) if datos_miembros else Decimal('0')
        dm['proporcion'] = round(prop * 100, 1)
        dm['aportacion_hogar_informativa'] = round(total_gastos_hogar * prop, 2)

    # ── PASO 3: Reglas MENSUALES de reparto ──────────────────────────────────
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
            'transferencias_entrantes': Decimal('0'),
        }
        for f in fondos
    }

    for regla in reglas_mensuales:
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
                'regla': regla, 'fondo': regla.fondo,
                'importe_base': importe_b, 'importe_pond': importe_p,
            })

            if regla.fondo_id and regla.fondo_id in fondos_aportaciones:
                fa = fondos_aportaciones[regla.fondo_id]
                fa['total_aportacion_base'] += importe_b
                fa['total_aportacion_pond'] += importe_p
                fa['aportantes'].append({
                    'miembro': dm['miembro'],
                    'importe_base': importe_b, 'importe_pond': importe_p,
                })
        else:
            libre_b_total = sum(dm['libre_base'] for dm in datos_miembros.values())
            libre_p_total = sum(dm['libre_ponderado'] for dm in datos_miembros.values())

            if regla.tipo_regla == 'porcentaje':
                total_b = round(libre_b_total * regla.porcentaje / 100, 2) if libre_b_total > 0 else Decimal('0')
                total_p = round(libre_p_total * regla.porcentaje / 100, 2) if libre_p_total > 0 else Decimal('0')
            else:
                total_b = regla.importe_fijo
                total_p = regla.importe_fijo

            for uid, dm in datos_miembros.items():
                prop = dm['ingreso_base'] / total_base_hogar if total_base_hogar > 0 else Decimal('1') / Decimal(str(len(datos_miembros)))
                importe_b = round(total_b * prop, 2)
                importe_p = round(total_p * prop, 2)

                dm['libre_base'] -= importe_b
                dm['libre_ponderado'] -= importe_p
                dm['aportaciones_fondos'].append({
                    'regla': regla, 'fondo': regla.fondo,
                    'importe_base': importe_b, 'importe_pond': importe_p,
                })

                if regla.fondo_id and regla.fondo_id in fondos_aportaciones:
                    fa = fondos_aportaciones[regla.fondo_id]
                    fa['total_aportacion_base'] += importe_b
                    fa['total_aportacion_pond'] += importe_p
                    fa['aportantes'].append({
                        'miembro': dm['miembro'],
                        'importe_base': importe_b, 'importe_pond': importe_p,
                    })

    # ── PASO 4: Ingresos extraordinarios del mes ─────────────────────────────
    ingresos_extraordinarios = _ingresos_del_mes(hogar, miembros, año, mes)
    total_extraordinario = sum(i['importe'] for i in ingresos_extraordinarios)

    # Asignar extraordinarios con fondo_destino directo
    for ext in ingresos_extraordinarios:
        fd = ext.get('fondo_destino')
        if fd and fd.id in fondos_aportaciones:
            fa = fondos_aportaciones[fd.id]
            fa['ingresos_extra_asignados'].append(ext)
            fa['total_extra'] += ext['importe']

    # ── PASO 4b: Reglas ANUALES — operan sobre extraordinarios del mes ───────
    # Agrupa extraordinarios sin fondo_destino por usuario
    extras_sin_asignar = {}
    for ext in ingresos_extraordinarios:
        if not ext.get('fondo_destino'):
            uid = ext.get('usuario_id')
            if uid:
                extras_sin_asignar.setdefault(uid, Decimal('0'))
                extras_sin_asignar[uid] += ext['importe']

    for regla in reglas_anuales:
        if regla.usuario_id and regla.usuario_id in extras_sin_asignar:
            disponible = extras_sin_asignar[regla.usuario_id]
            if disponible <= 0:
                continue

            if regla.tipo_regla == 'porcentaje':
                importe = round(disponible * regla.porcentaje / 100, 2)
            else:
                importe = min(regla.importe_fijo, disponible)

            extras_sin_asignar[regla.usuario_id] -= importe

            dm = datos_miembros.get(regla.usuario_id)
            if dm:
                dm['aportaciones_anuales'].append({
                    'regla': regla,
                    'fondo': regla.fondo,
                    'importe': importe,
                })

            if regla.fondo_id and regla.fondo_id in fondos_aportaciones:
                fa = fondos_aportaciones[regla.fondo_id]
                fa['total_extra'] += importe
                fa['ingresos_extra_asignados'].append({
                    'tipo': 'regla_anual',
                    'label': f"Regla anual: {regla.nombre}",
                    'miembro': dm['miembro'] if dm else None,
                    'importe': importe,
                    'fondo_destino': regla.fondo,
                })

        elif not regla.usuario_id:
            # Regla anual global — reparte entre todos los que tienen extras
            total_extras_global = sum(extras_sin_asignar.values())
            if total_extras_global <= 0:
                continue

            if regla.tipo_regla == 'porcentaje':
                total_asignar = round(total_extras_global * regla.porcentaje / 100, 2)
            else:
                total_asignar = min(regla.importe_fijo, total_extras_global)

            for uid in list(extras_sin_asignar.keys()):
                if extras_sin_asignar[uid] <= 0:
                    continue
                prop = extras_sin_asignar[uid] / total_extras_global
                importe = round(total_asignar * prop, 2)
                extras_sin_asignar[uid] -= importe

                dm = datos_miembros.get(uid)
                if dm:
                    dm['aportaciones_anuales'].append({
                        'regla': regla, 'fondo': regla.fondo, 'importe': importe,
                    })

                if regla.fondo_id and regla.fondo_id in fondos_aportaciones:
                    fa = fondos_aportaciones[regla.fondo_id]
                    fa['total_extra'] += importe

    # ── PASO 5: Sub-distribución interna de fondos + cascada ─────────────────
    for fid, fa in fondos_aportaciones.items():
        fondo_obj = fa['fondo']
        subsobres_qs = SubsobreFondo.objects.filter(
            fondo=fondo_obj, activo=True
        ).prefetch_related('partidas_vinculadas').select_related('fondo_destino')

        total_fondo = fa['total_aportacion_base'] + fa['total_extra'] + fa['transferencias_entrantes']
        total_asignado = Decimal('0')
        subsobres_detalle = []

        for s in subsobres_qs:
            imp = s.importe_calculado
            total_asignado += imp
            pct = round(imp / total_fondo * 100, 1) if total_fondo > 0 else Decimal('0')

            ss_data = {
                'subsobre': s,
                'importe': imp,
                'porcentaje_del_fondo': pct,
                'es_transferencia': s.es_transferencia,
                'fondo_destino': s.fondo_destino,
            }
            subsobres_detalle.append(ss_data)

            # Cascada: si tiene fondo_destino, sumar al fondo receptor
            if s.fondo_destino_id and s.fondo_destino_id in fondos_aportaciones:
                fondos_aportaciones[s.fondo_destino_id]['transferencias_entrantes'] += imp

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
        tipo = getattr(fa['fondo'], 'tipo_fondo', 'comun')
        total_fondo_mes = fa['total_aportacion_base'] + fa['total_extra'] + fa['transferencias_entrantes']
        if tipo in resumen_destinos:
            resumen_destinos[tipo] += total_fondo_mes

    total_ahorro_inversion = resumen_destinos['ahorro'] + resumen_destinos['inversion'] + resumen_destinos['emergencia']
    tasa_ahorro = round(total_ahorro_inversion / total_base_hogar * 100, 1) if total_base_hogar > 0 else Decimal('0')

    if tasa_ahorro >= 20:
        semaforo, semaforo_texto = 'verde', 'Excelente capacidad de ahorro'
    elif tasa_ahorro >= 10:
        semaforo, semaforo_texto = 'amarillo', 'Ahorro moderado — margen de mejora'
    elif tasa_ahorro > 0:
        semaforo, semaforo_texto = 'naranja', 'Ahorro bajo — revisar gastos'
    else:
        semaforo, semaforo_texto = 'rojo', 'Sin ahorro — atención'

    # Presupuesto anual
    presupuesto_anual = {
        'ingresos_netos': total_anual_hogar,
        'gastos_hogar_anuales': total_gastos_hogar * 12,
        'gastos_individuales_anuales': total_gastos_individuales * 12,
        'ahorro_inversion_anual': total_ahorro_inversion * 12,
        'libre_anual': (total_base_hogar - total_gastos_hogar - total_gastos_individuales - total_ahorro_inversion) * 12,
    }

    # ── PASO 7: Sankey ───────────────────────────────────────────────────────
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

        # Gastos individuales
        if dm['gastos_individuales'] > 0:
            dst_gi = get_node(f"Gastos {nombre}", 'gasto', '#ff4d4d')
            sankey_links.append({'source': src, 'target': dst_gi, 'value': float(dm['gastos_individuales'])})

        # Aportaciones mensuales a fondos
        for ap in dm['aportaciones_fondos']:
            fondo = ap['fondo']
            if fondo and ap['importe_base'] > 0:
                dst = get_node(fondo.nombre, 'fondo', fondo.color)
                sankey_links.append({'source': src, 'target': dst, 'value': float(ap['importe_base'])})
                transferencias.append({
                    'origen': nombre, 'concepto': ap['regla'].nombre,
                    'destino': fondo.nombre, 'cuenta': fondo.cuenta_asociada,
                    'color': fondo.color, 'tipo_fondo': getattr(fondo, 'tipo_fondo', 'comun'),
                    'importe_base': ap['importe_base'], 'importe_pond': ap['importe_pond'],
                    'periodicidad': 'mensual',
                })

        # Aportaciones anuales a fondos
        for ap in dm.get('aportaciones_anuales', []):
            fondo = ap['fondo']
            if fondo and ap['importe'] > 0:
                dst = get_node(fondo.nombre, 'fondo', fondo.color)
                # No duplicar link si ya existe — los anuales son eventuales
                sankey_links.append({'source': src, 'target': dst, 'value': float(ap['importe'])})
                transferencias.append({
                    'origen': nombre, 'concepto': f"📅 {ap['regla'].nombre}",
                    'destino': fondo.nombre, 'cuenta': fondo.cuenta_asociada,
                    'color': fondo.color, 'tipo_fondo': getattr(fondo, 'tipo_fondo', 'comun'),
                    'importe_base': ap['importe'], 'importe_pond': ap['importe'],
                    'periodicidad': 'anual',
                })

        # Libre personal
        if dm['libre_base'] > 0:
            dst_libre = get_node(f"Libre {nombre}", 'libre', '#00d1ff')
            sankey_links.append({'source': src, 'target': dst_libre, 'value': float(dm['libre_base'])})

    # Subsobres en Sankey: fondo → subsobre (o fondo → fondo si es transferencia)
    for fid, fa in fondos_aportaciones.items():
        fondo = fa['fondo']
        total_fondo = fa['total_aportacion_base'] + fa['total_extra'] + fa['transferencias_entrantes']
        if total_fondo <= 0:
            continue

        fondo_node = node_index.get(fondo.nombre)
        if fondo_node is None:
            continue

        for ss_data in fa['subsobres']:
            ss = ss_data['subsobre']
            imp = float(ss_data['importe'])
            if imp <= 0:
                continue

            if ss_data['es_transferencia'] and ss_data['fondo_destino']:
                # Cascada: fondo → otro fondo
                dst_fondo = get_node(ss_data['fondo_destino'].nombre, 'fondo', ss_data['fondo_destino'].color)
                sankey_links.append({'source': fondo_node, 'target': dst_fondo, 'value': imp})
                transferencias.append({
                    'origen': fondo.nombre, 'concepto': f"↪ {ss.nombre}",
                    'destino': ss_data['fondo_destino'].nombre,
                    'cuenta': ss_data['fondo_destino'].cuenta_asociada,
                    'color': ss_data['fondo_destino'].color,
                    'tipo_fondo': getattr(ss_data['fondo_destino'], 'tipo_fondo', 'comun'),
                    'importe_base': ss_data['importe'], 'importe_pond': ss_data['importe'],
                    'periodicidad': 'mensual',
                })
            else:
                # Sobre interno normal
                dst_ss = get_node(ss.nombre, 'subsobre', fondo.color)
                sankey_links.append({'source': fondo_node, 'target': dst_ss, 'value': imp})

        # Remanente del fondo
        rem = float(fa['remanente'])
        if rem > 0 and fa['subsobres']:
            dst_rem = get_node(f"Libre {fondo.nombre}", 'libre', '#555')
            sankey_links.append({'source': fondo_node, 'target': dst_rem, 'value': rem})

    # ── RESULTADO ─────────────────────────────────────────────────────────────
    return {
        'año': año, 'mes': mes,
        'datos_miembros': list(datos_miembros.values()),
        'total_base_hogar': total_base_hogar,
        'total_pond_hogar': total_pond_hogar,
        'total_anual_hogar': total_anual_hogar,
        'gastos_hogar_fijos': gastos_hogar_fijos,
        'gastos_hogar_provision': gastos_hogar_provision,
        'gastos_hogar_variables': gastos_hogar_variables,
        'total_gastos_hogar': total_gastos_hogar,
        'total_gastos_individuales': total_gastos_individuales,
        'gastos_por_tipo': gastos_por_tipo,
        'fondos_aportaciones': list(fondos_aportaciones.values()),
        'ingresos_extraordinarios': ingresos_extraordinarios,
        'total_extraordinario': total_extraordinario,
        'resumen_destinos': resumen_destinos,
        'total_ahorro_inversion': total_ahorro_inversion,
        'tasa_ahorro': tasa_ahorro,
        'semaforo': semaforo, 'semaforo_texto': semaforo_texto,
        'presupuesto_anual': presupuesto_anual,
        'transferencias': transferencias,
        'sankey_nodes': sankey_nodes, 'sankey_links': sankey_links,
    }


def info_extras_usuario(hogar, user, año):
    """
    Devuelve info sobre los ingresos anuales/extras de un usuario para un año.
    Usado por el modal de regla anual para mostrar cuánto hay disponible.

    Returns dict:
        detalle: list of {label, importe, meses}
        total_anual: Decimal total de extras en el año
        total_asignado: Decimal ya asignado via reglas anuales
        disponible: Decimal sin asignar
    """
    fuentes = FuenteIngreso.objects.filter(
        usuario=user, hogar=hogar, activo=True
    )
    detalle = []
    total_anual = Decimal('0')

    for f in fuentes:
        ratio = _ratio_neto(f)

        # Pagas extras
        if f.modo_entrada == 'anual' and f.num_pagas > 12:
            num_extras = f.num_pagas - 12
            bruto_paga = f.importe_declarado / Decimal(str(f.num_pagas))
            neto_paga = round(bruto_paga * ratio, 2)
            total_extras = neto_paga * num_extras

            detalle.append({
                'label': f"Paga extra — {f.nombre}",
                'importe_unitario': float(neto_paga),
                'cantidad': num_extras,
                'total': float(total_extras),
                'meses': f.pagas_extras_meses,
            })
            total_anual += total_extras

        # Ingresos periódicos no mensuales
        elif f.modo_entrada == 'periodo' and not f.es_mensual_recurrente:
            neto = round(f.importe_declarado * ratio, 2)
            num_cobros = len(f.cobro_meses)
            total_periodo = neto * num_cobros

            detalle.append({
                'label': f"Periódico — {f.nombre}",
                'importe_unitario': float(neto),
                'cantidad': num_cobros,
                'total': float(total_periodo),
                'meses': f.cobro_meses,
            })
            total_anual += total_periodo

    # Ya asignado via reglas anuales
    total_asignado = Decimal('0')
    reglas_anuales = ReglaReparto.objects.filter(
        hogar=hogar, usuario=user, activo=True
    )
    for r in reglas_anuales:
        if getattr(r, 'periodicidad_regla', 'mensual') == 'anual':
            if r.tipo_regla == 'porcentaje':
                total_asignado += round(total_anual * r.porcentaje / 100, 2)
            else:
                total_asignado += r.importe_fijo

    return {
        'detalle': detalle,
        'total_anual': total_anual,
        'total_asignado': total_asignado,
        'disponible': total_anual - total_asignado,
    }
