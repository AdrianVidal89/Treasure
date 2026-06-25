"""
Motor de distribución de Treasure.

Flujo:
  1. Ingresos: base mensual + puntuales del mes + override AjusteIngresoMensual
  2. Gastos individuales: restan del libre personal
  3. Reglas de reparto → aportaciones a fondos
  4. Subsobres / cascada entre fondos (filtrados por solo_mes)
  5. Categorización por tipo_fondo:
     - comun: cubre gastos asignados, resto → libre
     - ahorro: suma a ahorro total
     - inversion: suma a inversión total
  6. KPIs y transferencias
"""
from decimal import Decimal
import datetime

from .models import (
    FuenteIngreso, PartidaGasto, ReglaReparto,
    FondoFamiliar, SubsobreFondo, AjusteIngresoMensual,
)
from .fiscal import calcular_neto_anual


# ---------------------------------------------------------------------------
# Helpers de ingreso
# ---------------------------------------------------------------------------

def _neto_fuente_mes(f, mes, anio):
    bruto_anual = f.importe_anual_bruto
    estimado_anual = f.importe_anual_estimado

    if f.es_bruto and bruto_anual > 0:
        resultado = calcular_neto_anual(bruto_anual, f.pais_fiscal)
        ratio = resultado['neto'] / bruto_anual
    else:
        ratio = Decimal('1')

    pond = round(estimado_anual * ratio / Decimal('12'), 2)

    ajuste = AjusteIngresoMensual.objects.filter(
        fuente=f, mes=mes, **{'año': anio}
    ).first()
    if ajuste and ajuste.importe_real is not None:
        return ajuste.importe_real, pond, True

    if f.es_mensual_recurrente and f.incluir_en_mensual:
        base_mensual = round(f.importe_mensual_base * ratio, 2)
    else:
        base_mensual = Decimal('0')

    extra_mes = Decimal('0')
    if f.modo_entrada == 'anual' and f.num_pagas > 12:
        if mes in f.pagas_extras_meses:
            extra_mes += round(f.importe_paga_extra * ratio, 2)
    elif f.modo_entrada == 'periodo' and f.periodicidad != 'mensual':
        if mes in f.cobro_meses:
            extra_mes += round(f.importe_declarado * ratio, 2)

    return base_mensual + extra_mes, pond, False


def _neto_fuente_base(f):
    bruto_anual = f.importe_anual_bruto
    estimado_anual = f.importe_anual_estimado

    if f.es_bruto and bruto_anual > 0:
        resultado = calcular_neto_anual(bruto_anual, f.pais_fiscal)
        ratio = resultado['neto'] / bruto_anual
        base = round(f.importe_mensual_base * ratio, 2) if f.es_mensual_recurrente and f.incluir_en_mensual else Decimal('0')
        pond = round(estimado_anual * ratio / Decimal('12'), 2)
    else:
        base = f.importe_mensual_base if f.es_mensual_recurrente and f.incluir_en_mensual else Decimal('0')
        pond = f.importe_mensual_ponderado

    return base, pond


# ---------------------------------------------------------------------------
# Motor principal
# ---------------------------------------------------------------------------

def clasificar_salud(tasa):
    """Devuelve (semaforo, texto) a partir de una tasa de ahorro (en %)."""
    if tasa >= 20:
        return 'verde', 'Excelente salud financiera'
    elif tasa >= 10:
        return 'amarillo', 'Salud financiera aceptable'
    elif tasa >= 0:
        return 'naranja', 'Margen ajustado'
    return 'rojo', 'Gastas más de lo que ingresas'


def calcular_flujos(hogar, mes=None, anio=None):
    hoy = datetime.date.today()
    mes = mes or hoy.month
    anio = anio or hoy.year

    miembros = hogar.miembros.select_related('user').all()
    partidas = PartidaGasto.objects.filter(
        hogar=hogar, activo=True
    ).select_related('categoria', 'responsable', 'fondo_asignado')
    reglas_qs = ReglaReparto.objects.filter(
        hogar=hogar, activo=True
    ).select_related('fondo', 'usuario').order_by('orden')
    reglas = [r for r in reglas_qs if r.solo_mes is None or r.solo_mes == mes]
    fondos = list(FondoFamiliar.objects.filter(hogar=hogar, activo=True))

    # Subsobres: filtrar por solo_mes igual que las reglas
    subsobres_qs = SubsobreFondo.objects.filter(
        fondo__hogar=hogar, activo=True
    ).select_related('fondo', 'fondo_destino')
    subsobres = [s for s in subsobres_qs if s.solo_mes is None or s.solo_mes == mes]

    # =========================================================
    # PASO 1: Ingresos
    # =========================================================
    datos_miembros = {}
    total_base_hogar = Decimal('0')
    total_pond_hogar = Decimal('0')
    total_base_puro_hogar = Decimal('0')

    for m in miembros:
        fuentes = FuenteIngreso.objects.filter(usuario=m.user, hogar=hogar, activo=True)
        ing_base = Decimal('0')
        ing_pond = Decimal('0')
        ing_base_puro = Decimal('0')
        extras_mes = []
        ajustes_mes = []

        for f in fuentes:
            b, p, tiene_ajuste = _neto_fuente_mes(f, mes, anio)
            b_base, _ = _neto_fuente_base(f)

            if tiene_ajuste:
                ajustes_mes.append({
                    'fuente_id': f.id, 'fuente': f.nombre,
                    'importe_ajustado': b, 'importe_base': b_base,
                })
            else:
                extra = b - b_base
                if extra > 0:
                    extras_mes.append({'fuente': f.nombre, 'importe': extra})

            ing_base += b
            ing_pond += p
            ing_base_puro += b_base

        fuentes_lista = [{'id': f.id, 'nombre': f.nombre} for f in fuentes]

        datos_miembros[m.user.id] = {
            'miembro': m,
            'ingreso_base': ing_base,
            'ingreso_ponderado': ing_pond,
            'extras_mes': extras_mes,
            'ajustes_mes': ajustes_mes,
            'fuentes': fuentes_lista,
            'gastos_individuales': Decimal('0'),
            'aportacion_hogar_informativa': Decimal('0'),
            'proporcion': Decimal('0'),
            'aportaciones_fondos': [],
            'libre_base': ing_base,
            'libre_ponderado': ing_pond,
        }
        total_base_hogar += ing_base
        total_pond_hogar += ing_pond
        total_base_puro_hogar += ing_base_puro

    # =========================================================
    # PASO 2: Gastos
    # =========================================================
    gastos_hogar_total = Decimal('0')
    total_gastos_ind = Decimal('0')
    gastos_por_fondo = {}

    for p in partidas:
        mensual = p.importe_mensual

        if p.responsable_id and p.responsable_id in datos_miembros:
            dm = datos_miembros[p.responsable_id]
            dm['gastos_individuales'] += mensual
            dm['libre_base'] -= mensual
            dm['libre_ponderado'] -= mensual
            total_gastos_ind += mensual
        else:
            gastos_hogar_total += mensual
            if p.fondo_asignado_id:
                gastos_por_fondo.setdefault(p.fondo_asignado_id, []).append({
                    'id': p.id,
                    'nombre': p.nombre,
                    'importe_mensual': mensual,
                    'categoria': p.categoria.nombre if p.categoria else '',
                })

    total_gastos_all = total_gastos_ind + gastos_hogar_total

    for uid, dm in datos_miembros.items():
        if total_base_hogar > 0:
            prop = dm['ingreso_base'] / total_base_hogar
        else:
            prop = Decimal('1') / Decimal(str(len(datos_miembros))) if datos_miembros else Decimal('0')
        dm['proporcion'] = round(prop * 100, 1)
        dm['aportacion_hogar_informativa'] = round(gastos_hogar_total * prop, 2)

    # =========================================================
    # PASO 3: Reglas de reparto → fondos
    # =========================================================
    fondos_aportaciones = {f.id: {
        'fondo': f,
        'total_aportacion_base': Decimal('0'),
        'total_aportacion_pond': Decimal('0'),
        'aportantes': [],
        'total_cascada_saliente': Decimal('0'),
        'subsobres': [],
        'gastos_asignados': gastos_por_fondo.get(f.id, []),
        'gastos_cubiertos': sum(g['importe_mensual'] for g in gastos_por_fondo.get(f.id, [])),
    } for f in fondos}

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
                if total_base_hogar > 0:
                    prop = dm['ingreso_base'] / total_base_hogar
                else:
                    prop = Decimal('1') / Decimal(str(len(datos_miembros)))

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

    # =========================================================
    # PASO 4: Subsobres / cascada (ya filtrados por solo_mes)
    # =========================================================
    transferencias_cascada = []

    for ss in subsobres:
        if ss.fondo_id not in fondos_aportaciones:
            continue
        fa_origen = fondos_aportaciones[ss.fondo_id]
        importe = ss.importe_manual or Decimal('0')
        if importe <= 0:
            continue

        fa_origen['total_cascada_saliente'] += importe
        fa_origen['subsobres'].append({
            'id': ss.id,
            'nombre': ss.nombre,
            'tipo': ss.tipo,
            'importe': importe,
            'fondo_destino': ss.fondo_destino,
            'solo_mes': ss.solo_mes,
            'solo_mes_display': ss.get_solo_mes_display() if ss.solo_mes else '',
        })

        if ss.fondo_destino_id and ss.fondo_destino_id in fondos_aportaciones:
            fa_destino = fondos_aportaciones[ss.fondo_destino_id]
            fa_destino['total_aportacion_base'] += importe
            fa_destino['total_aportacion_pond'] += importe

        transferencias_cascada.append({
            'subsobre_id': ss.id,
            'origen': ss.fondo.nombre,
            'destino': ss.fondo_destino.nombre if ss.fondo_destino else ss.nombre,
            'importe': importe, 'nombre': ss.nombre,
            'tipo': ss.tipo, 'color': ss.fondo.color,
        })

    for fid, fa in fondos_aportaciones.items():
        fa['total_aportacion_neta'] = fa['total_aportacion_base'] - fa['total_cascada_saliente']

    # =========================================================
    # PASO 5: Categorización por tipo_fondo
    # =========================================================
    total_ahorro = Decimal('0')
    total_inversion = Decimal('0')
    total_gastos_cubiertos = Decimal('0')
    libre_comun = Decimal('0')

    for fid, fa in fondos_aportaciones.items():
        tipo = fa['fondo'].tipo_fondo
        neta = fa['total_aportacion_neta']

        if tipo == 'ahorro':
            total_ahorro += neta
            fa['destino_tipo'] = 'ahorro'
        elif tipo == 'inversion':
            total_inversion += neta
            fa['destino_tipo'] = 'inversión'
        elif tipo == 'comun':
            cubiertos = fa['gastos_cubiertos']
            total_gastos_cubiertos += cubiertos
            resto = neta - cubiertos
            libre_comun += max(resto, Decimal('0'))
            fa['libre_fondo'] = resto
            fa['destino_tipo'] = 'gastos'
        else:
            total_ahorro += neta
            fa['destino_tipo'] = 'ahorro'

    # =========================================================
    # PASO 6: KPIs
    # =========================================================
    libre_personal = sum(dm['libre_base'] for dm in datos_miembros.values())
    libre_total = libre_personal + libre_comun
    total_dedicado = total_ahorro + total_inversion

    tasa_ahorro = round(total_dedicado / total_base_hogar * 100, 1) if total_base_hogar > 0 else Decimal('0')

    semaforo, semaforo_texto = clasificar_salud(tasa_ahorro)

    def pct(val):
        return round(float(val / total_base_hogar * 100), 1) if total_base_hogar > 0 else 0

    # =========================================================
    # PASO 7: Transferencias
    # =========================================================
    transferencias = []
    for uid, dm in datos_miembros.items():
        nombre = dm['miembro'].user.first_name or dm['miembro'].user.username
        for ap in dm['aportaciones_fondos']:
            if ap['importe_base'] > 0:
                destino = ap['fondo'].nombre if ap['fondo'] else ap['regla'].nombre
                cuenta = ap['fondo'].cuenta_asociada if ap['fondo'] and ap['fondo'].cuenta_asociada else ''
                transferencias.append({
                    'origen': nombre, 'destino': destino, 'cuenta': cuenta,
                    'importe_base': ap['importe_base'], 'importe_pond': ap['importe_pond'],
                    'concepto': ap['regla'].nombre, 'tipo': 'fondo', 'color': ap['regla'].color,
                })
    for tc in transferencias_cascada:
        transferencias.append({
            'origen': f"Fondo: {tc['origen']}", 'destino': tc['destino'], 'cuenta': '',
            'importe_base': tc['importe'], 'importe_pond': tc['importe'],
            'concepto': tc['nombre'], 'tipo': 'cascada', 'color': tc['color'],
        })

    # Total aportado a fondos por cada miembro (para la barra del resumen).
    for uid, dm in datos_miembros.items():
        dm['aportaciones_total'] = sum(
            ap['importe_base'] for ap in dm['aportaciones_fondos']
        )

    return {
        'mes': mes, 'anio': anio, 'mes_nombre': _nombre_mes(mes),

        'datos_miembros': list(datos_miembros.values()),
        'fondos_aportaciones': list(fondos_aportaciones.values()),
        'transferencias': transferencias,
        'transferencias_cascada': transferencias_cascada,

        'ingreso_base_hogar': total_base_hogar,
        'ingreso_base_puro_hogar': total_base_puro_hogar,
        'ingreso_pond_hogar': total_pond_hogar,

        'gastos_hogar_total': gastos_hogar_total,
        'total_gastos_individuales': total_gastos_ind,
        'total_gastos_all': total_gastos_all,
        'total_gastos_cubiertos': total_gastos_cubiertos,

        'total_ahorro': total_ahorro,
        'total_inversion': total_inversion,
        'libre_personal': libre_personal,
        'libre_comun': libre_comun,
        'libre_total': libre_total,

        'tasa_ahorro': tasa_ahorro,
        'semaforo': semaforo,
        'semaforo_texto': semaforo_texto,

        'pct_gastos': pct(total_gastos_all),
        'pct_ahorro': pct(total_ahorro),
        'pct_inversion': pct(total_inversion),
        'pct_libre': pct(libre_total),
    }


# ---------------------------------------------------------------------------
# Resumen anual
# ---------------------------------------------------------------------------

def calcular_resumen_anual(hogar, anio=None):
    anio = anio or datetime.date.today().year
    resumen = {
        'anio': anio, 'meses': [],
        'total_ingresos': Decimal('0'), 'total_gastos': Decimal('0'),
        'total_ahorro': Decimal('0'), 'total_inversion': Decimal('0'),
        'total_libre': Decimal('0'),
    }

    for mes in range(1, 13):
        d = calcular_flujos(hogar, mes=mes, anio=anio)
        resumen['meses'].append({
            'mes': mes, 'nombre': _nombre_mes(mes),
            'ingresos': d['ingreso_base_hogar'],
            'gastos': d['total_gastos_all'],
            'ahorro': d['total_ahorro'],
            'inversion': d['total_inversion'],
            'libre': d['libre_total'],
            'semaforo': d['semaforo'],
        })
        resumen['total_ingresos'] += d['ingreso_base_hogar']
        resumen['total_gastos'] += d['total_gastos_all']
        resumen['total_ahorro'] += d['total_ahorro']
        resumen['total_inversion'] += d['total_inversion']
        resumen['total_libre'] += d['libre_total']

    return resumen


def _nombre_mes(mes):
    nombres = [
        '', 'Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio',
        'Julio', 'Agosto', 'Septiembre', 'Octubre', 'Noviembre', 'Diciembre'
    ]
    return nombres[mes] if 1 <= mes <= 12 else ''