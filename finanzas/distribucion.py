from decimal import Decimal
from .models import FuenteIngreso, PartidaGasto, ReglaReparto, FondoFamiliar
from .fiscal import calcular_neto_anual


def _calcular_neto_fuente(f):
    bruto_anual = f.importe_anual_bruto
    estimado_anual = f.importe_anual_estimado

    if f.es_bruto and bruto_anual > 0:
        resultado = calcular_neto_anual(bruto_anual, f.pais_fiscal)
        ratio = resultado['neto'] / bruto_anual
        base = round(f.importe_mensual_base * ratio, 2) if f.es_mensual_recurrente and f.incluir_en_mensual else Decimal('0')
        pond = round(estimado_anual * ratio / Decimal('12'), 2)
        anual = round(estimado_anual * ratio, 2)
    else:
        base = f.importe_mensual_base if f.es_mensual_recurrente and f.incluir_en_mensual else Decimal('0')
        pond = f.importe_mensual_ponderado
        anual = estimado_anual

    return base, pond, anual


def calcular_flujos(hogar):
    """
    Motor de flujos corregido:
    - Gastos individuales: se restan del disponible (son compromisos fijos)
    - Gastos del hogar: SOLO informativos — cada usuario decide cómo contribuir
      via reglas de reparto
    - Reglas: el usuario define a donde va su dinero libre (fondos, inversión, etc.)
    - Lo que queda tras reglas = libre personal real
    """
    miembros = hogar.miembros.select_related('user').all()
    partidas = PartidaGasto.objects.filter(hogar=hogar, activo=True).select_related('categoria', 'responsable')
    reglas = ReglaReparto.objects.filter(hogar=hogar, activo=True).select_related('fondo', 'usuario')
    fondos = FondoFamiliar.objects.filter(hogar=hogar, activo=True)

    # === PASO 1: Ingresos por miembro ===
    datos_miembros = {}
    total_base_hogar = Decimal('0')
    total_pond_hogar = Decimal('0')
    total_anual_hogar = Decimal('0')

    for m in miembros:
        fuentes = FuenteIngreso.objects.filter(usuario=m.user, hogar=hogar, activo=True)
        ing_base = Decimal('0')
        ing_pond = Decimal('0')
        ing_anual = Decimal('0')

        for f in fuentes:
            b, p, a = _calcular_neto_fuente(f)
            ing_base += b
            ing_pond += p
            ing_anual += a

        datos_miembros[m.user.id] = {
            'miembro': m,
            'ingreso_base': ing_base,
            'ingreso_ponderado': ing_pond,
            'ingreso_anual': ing_anual,
            'gastos_individuales': Decimal('0'),
            # disponible = ingreso - gastos_individuales
            'disponible_base': ing_base,
            'disponible_pond': ing_pond,
            # aportacion_hogar: informativo, cuanto le corresponde de los gastos del hogar
            'aportacion_hogar_informativa': Decimal('0'),
            'proporcion': Decimal('0'),
            'aportaciones_fondos': [],
            # libre = disponible - lo que el usuario decide mover via reglas
            'libre_base': ing_base,
            'libre_ponderado': ing_pond,
        }
        total_base_hogar += ing_base
        total_pond_hogar += ing_pond
        total_anual_hogar += ing_anual

    # === PASO 2: Gastos individuales (se restan del disponible) ===
    gastos_hogar_fijos = Decimal('0')
    gastos_hogar_provision = Decimal('0')
    gastos_hogar_variables = Decimal('0')

    for p in partidas:
        mensual = p.importe_mensual
        tipo = p.categoria.tipo

        if p.responsable_id and p.responsable_id in datos_miembros:
            # Gasto individual — se resta del disponible
            datos_miembros[p.responsable_id]['gastos_individuales'] += mensual
            datos_miembros[p.responsable_id]['disponible_base'] -= mensual
            datos_miembros[p.responsable_id]['disponible_pond'] -= mensual
            datos_miembros[p.responsable_id]['libre_base'] -= mensual
            datos_miembros[p.responsable_id]['libre_ponderado'] -= mensual
        else:
            # Gasto del hogar — solo contabilizamos el total
            if tipo == 'fijo':
                gastos_hogar_fijos += mensual
            elif tipo == 'anual':
                gastos_hogar_provision += mensual
            else:
                gastos_hogar_variables += mensual

    total_gastos_hogar = gastos_hogar_fijos + gastos_hogar_provision + gastos_hogar_variables

    # Calcular aportacion proporcional informativa de cada miembro
    for uid, dm in datos_miembros.items():
        if total_base_hogar > 0:
            prop = dm['ingreso_base'] / total_base_hogar
        else:
            prop = Decimal('1') / Decimal(str(len(datos_miembros))) if datos_miembros else Decimal('0')
        dm['proporcion'] = round(prop * 100, 1)
        dm['aportacion_hogar_informativa'] = round(total_gastos_hogar * prop, 2)

    # === PASO 3: Aplicar reglas de reparto ===
    fondos_aportaciones = {f.id: {
        'fondo': f,
        'total_aportacion_base': Decimal('0'),
        'total_aportacion_pond': Decimal('0'),
        'aportantes': [],
    } for f in fondos}

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

        else:
            # Regla global — reparto proporcional entre todos
            libre_b_total = sum(dm['libre_base'] for dm in datos_miembros.values())
            libre_p_total = sum(dm['libre_ponderado'] for dm in datos_miembros.values())

            if regla.tipo_regla == 'porcentaje':
                total_b = round(libre_b_total * regla.porcentaje / 100, 2) if libre_b_total > 0 else Decimal('0')
                total_p = round(libre_p_total * regla.porcentaje / 100, 2) if libre_p_total > 0 else Decimal('0')
            else:
                total_b = regla.importe_fijo
                total_p = regla.importe_fijo

            # Repartir entre todos los miembros
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
                    fondos_aportaciones[regla.fondo_id]['total_aportacion_base'] += importe_b
                    fondos_aportaciones[regla.fondo_id]['total_aportacion_pond'] += importe_p
                    fondos_aportaciones[regla.fondo_id]['aportantes'].append({
                        'miembro': dm['miembro'],
                        'importe_base': importe_b,
                        'importe_pond': importe_p,
                    })
            continue  # Skip individual processing below

        # Apply individual rule
        dm = datos_miembros[regla.usuario_id]
        dm['libre_base'] -= importe_b
        dm['libre_ponderado'] -= importe_p
        dm['aportaciones_fondos'].append({
            'regla': regla,
            'fondo': regla.fondo,
            'importe_base': importe_b,
            'importe_pond': importe_p,
        })

        if regla.fondo_id and regla.fondo_id in fondos_aportaciones:
            fondos_aportaciones[regla.fondo_id]['total_aportacion_base'] += importe_b
            fondos_aportaciones[regla.fondo_id]['total_aportacion_pond'] += importe_p
            fondos_aportaciones[regla.fondo_id]['aportantes'].append({
                'miembro': dm['miembro'],
                'importe_base': importe_b,
                'importe_pond': importe_p,
            })

    # === PASO 4: Transferencias ===
    transferencias = []

    for uid, dm in datos_miembros.items():
        nombre = dm['miembro'].user.first_name or dm['miembro'].user.username

        for ap in dm['aportaciones_fondos']:
            if ap['importe_base'] > 0:
                destino = ap['fondo'].nombre if ap['fondo'] else ap['regla'].nombre
                cuenta = ap['fondo'].cuenta_asociada if ap['fondo'] and ap['fondo'].cuenta_asociada else ''
                transferencias.append({
                    'origen': nombre,
                    'destino': destino,
                    'cuenta': cuenta,
                    'importe_base': ap['importe_base'],
                    'importe_pond': ap['importe_pond'],
                    'concepto': ap['regla'].nombre,
                    'tipo': 'fondo',
                    'color': ap['regla'].color,
                })

    # === PASO 5: KPIs globales ===
    total_gastos_ind = sum(dm['gastos_individuales'] for dm in datos_miembros.values())
    total_gastos_all = total_gastos_hogar + total_gastos_ind
    total_libre_base = sum(dm['libre_base'] for dm in datos_miembros.values())
    total_libre_pond = sum(dm['libre_ponderado'] for dm in datos_miembros.values())

    tasa_base = round(total_libre_base / total_base_hogar * 100, 1) if total_base_hogar > 0 else Decimal('0')
    tasa_pond = round(total_libre_pond / total_pond_hogar * 100, 1) if total_pond_hogar > 0 else Decimal('0')

    if tasa_base >= 20:
        semaforo, semaforo_texto = 'verde', 'Excelente salud financiera'
    elif tasa_base >= 10:
        semaforo, semaforo_texto = 'amarillo', 'Salud financiera aceptable'
    elif tasa_base >= 0:
        semaforo, semaforo_texto = 'naranja', 'Margen ajustado'
    else:
        semaforo, semaforo_texto = 'rojo', 'Gastas mas de lo que ingresas'

    # === PASO 6: Datos Sankey ===
    sankey_nodes = []
    sankey_links = []
    node_index = {}

    def get_node(name, group):
        key = f"{group}:{name}"
        if key not in node_index:
            node_index[key] = len(sankey_nodes)
            sankey_nodes.append({'name': name, 'group': group})
        return node_index[key]

    for uid, dm in datos_miembros.items():
        nombre = dm['miembro'].user.first_name or dm['miembro'].user.username
        ing = float(dm['ingreso_base'])
        if ing <= 0:
            continue

        src = get_node(nombre, 'ingreso')

        # Gastos individuales
        if dm['gastos_individuales'] > 0:
            dst = get_node('Gastos propios', 'gasto')
            sankey_links.append({'source': src, 'target': dst,
                'value': float(dm['gastos_individuales']), 'color': '#ff4d4d'})

        # Aportaciones a fondos via reglas
        for ap in dm['aportaciones_fondos']:
            if ap['importe_base'] > 0:
                fondo_nombre = ap['fondo'].nombre if ap['fondo'] else ap['regla'].nombre
                dst = get_node(fondo_nombre, 'fondo')
                sankey_links.append({'source': src, 'target': dst,
                    'value': float(ap['importe_base']), 'color': ap['regla'].color})

        # Libre restante
        if dm['libre_base'] > 0:
            dst = get_node(f"Libre {nombre}", 'libre')
            sankey_links.append({'source': src, 'target': dst,
                'value': float(dm['libre_base']), 'color': '#00ff88'})

    # Porcentajes barra (sobre ingreso base total)
    def pct(val):
        return round(float(val / total_base_hogar * 100), 1) if total_base_hogar > 0 else 0

    return {
        'datos_miembros': list(datos_miembros.values()),
        'fondos_aportaciones': list(fondos_aportaciones.values()),
        'transferencias': transferencias,

        'ingreso_base_hogar': total_base_hogar,
        'ingreso_pond_hogar': total_pond_hogar,
        'ingreso_anual_hogar': total_anual_hogar,

        'gastos_hogar_fijos': gastos_hogar_fijos,
        'gastos_hogar_provision': gastos_hogar_provision,
        'gastos_hogar_variables': gastos_hogar_variables,
        'total_gastos_hogar': total_gastos_hogar,
        'total_gastos_individuales': total_gastos_ind,
        'total_gastos_all': total_gastos_all,

        'libre_base_hogar': total_libre_base,
        'libre_pond_hogar': total_libre_pond,
        'tasa_ahorro_base': tasa_base,
        'tasa_ahorro_pond': tasa_pond,
        'semaforo': semaforo,
        'semaforo_texto': semaforo_texto,

        'pct_ind': pct(total_gastos_ind),
        'pct_hogar': pct(total_gastos_hogar),
        'pct_libre': pct(total_libre_base),

        'sankey_nodes': sankey_nodes,
        'sankey_links': sankey_links,
    }
