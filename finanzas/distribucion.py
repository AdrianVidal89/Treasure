from decimal import Decimal
from .models import FuenteIngreso, PartidaGasto, ReglaReparto, FondoFamiliar
from .fiscal import calcular_neto_anual


def _calcular_neto_fuente(f):
    """Devuelve (neto_base, neto_ponderado, neto_anual) para una fuente."""
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
    Calcula el flujo completo del dinero en el hogar.

    Devuelve:
    - miembros_flujo: lista de dicts con el flujo completo de cada miembro
    - fondos_flujo: lista de dicts con ingresos/gastos de cada fondo
    - transferencias: lista de instrucciones claras de movimiento
    - resumen_hogar: KPIs globales
    - sankey_data: datos preparados para grafico Sankey con D3
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
            'aportaciones_fondos': [],  # [{fondo, importe_base, importe_pond, tipo}]
            'libre_base': ing_base,
            'libre_ponderado': ing_pond,
        }
        total_base_hogar += ing_base
        total_pond_hogar += ing_pond
        total_anual_hogar += ing_anual

    # === PASO 2: Gastos individuales ===
    gastos_hogar_fijos = Decimal('0')
    gastos_hogar_provision = Decimal('0')
    gastos_hogar_variables = Decimal('0')

    for p in partidas:
        mensual = p.importe_mensual
        tipo = p.categoria.tipo

        if p.responsable_id and p.responsable_id in datos_miembros:
            datos_miembros[p.responsable_id]['gastos_individuales'] += mensual
            datos_miembros[p.responsable_id]['libre_base'] -= mensual
            datos_miembros[p.responsable_id]['libre_ponderado'] -= mensual
        else:
            if tipo == 'fijo':
                gastos_hogar_fijos += mensual
            elif tipo == 'anual':
                gastos_hogar_provision += mensual
            else:
                gastos_hogar_variables += mensual

    total_gastos_hogar = gastos_hogar_fijos + gastos_hogar_provision + gastos_hogar_variables

    # === PASO 3: Calcular aportaciones a fondos por persona ===
    # Cada regla puede ser:
    # - Global (usuario=None): aplica al total del hogar, se reparte entre miembros
    # - Individual (usuario=X): aplica solo a ese miembro
    fondos_aportaciones = {}  # fondo_id -> {base, pond}
    for f in fondos:
        fondos_aportaciones[f.id] = {
            'fondo': f,
            'total_aportacion_base': Decimal('0'),
            'total_aportacion_pond': Decimal('0'),
            'aportantes': [],
        }

    for regla in reglas:
        if regla.usuario_id:
            # Regla individual - aplica solo a ese miembro
            dm = datos_miembros.get(regla.usuario_id)
            if not dm:
                continue

            libre_base = dm['libre_base']
            libre_pond = dm['libre_ponderado']

            if regla.tipo_regla == 'porcentaje':
                importe_b = round(libre_base * regla.porcentaje / 100, 2) if libre_base > 0 else Decimal('0')
                importe_p = round(libre_pond * regla.porcentaje / 100, 2) if libre_pond > 0 else Decimal('0')
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
                fondos_aportaciones[regla.fondo_id]['total_aportacion_base'] += importe_b
                fondos_aportaciones[regla.fondo_id]['total_aportacion_pond'] += importe_p
                fondos_aportaciones[regla.fondo_id]['aportantes'].append({
                    'miembro': dm['miembro'],
                    'importe_base': importe_b,
                    'importe_pond': importe_p,
                })
        else:
            # Regla global - se reparte entre todos proporcional al ingreso
            libre_base_hogar_actual = sum(dm['libre_base'] for dm in datos_miembros.values())
            libre_pond_hogar_actual = sum(dm['libre_ponderado'] for dm in datos_miembros.values())

            if regla.tipo_regla == 'porcentaje':
                total_b = round(libre_base_hogar_actual * regla.porcentaje / 100, 2) if libre_base_hogar_actual > 0 else Decimal('0')
                total_p = round(libre_pond_hogar_actual * regla.porcentaje / 100, 2) if libre_pond_hogar_actual > 0 else Decimal('0')
            else:
                total_b = regla.importe_fijo
                total_p = regla.importe_fijo

            # Repartir por proporcion de ingreso base
            for uid, dm in datos_miembros.items():
                if total_base_hogar > 0:
                    prop = dm['ingreso_base'] / total_base_hogar
                else:
                    prop = Decimal('1') / Decimal(str(len(datos_miembros)))

                aport_b = round(total_b * prop, 2)
                aport_p = round(total_p * prop, 2)

                dm['libre_base'] -= aport_b
                dm['libre_ponderado'] -= aport_p
                dm['aportaciones_fondos'].append({
                    'regla': regla,
                    'fondo': regla.fondo,
                    'importe_base': aport_b,
                    'importe_pond': aport_p,
                })

                if regla.fondo_id and regla.fondo_id in fondos_aportaciones:
                    fondos_aportaciones[regla.fondo_id]['total_aportacion_base'] += aport_b
                    fondos_aportaciones[regla.fondo_id]['total_aportacion_pond'] += aport_p
                    fondos_aportaciones[regla.fondo_id]['aportantes'].append({
                        'miembro': dm['miembro'],
                        'importe_base': aport_b,
                        'importe_pond': aport_p,
                    })

    # Descontar también la aportación proporcional a gastos del hogar
    for uid, dm in datos_miembros.items():
        if total_base_hogar > 0:
            prop = dm['ingreso_base'] / total_base_hogar
        else:
            prop = Decimal('1') / Decimal(str(len(datos_miembros)))
        aportacion_hogar = round(total_gastos_hogar * prop, 2)
        dm['aportacion_hogar_base'] = aportacion_hogar
        dm['proporcion'] = round(prop * 100, 1)
        dm['libre_base'] -= aportacion_hogar
        dm['libre_ponderado'] -= aportacion_hogar

    # === PASO 4: Construir lista de transferencias ===
    transferencias = []

    for uid, dm in datos_miembros.items():
        nombre = dm['miembro'].user.first_name or dm['miembro'].user.username

        # Aportacion a gastos del hogar
        if dm['aportacion_hogar_base'] > 0:
            fondo_conjunto = next((f for f in fondos if f.modo_aportacion in ['igual', 'proporcional', 'fijo']), None)
            destino = fondo_conjunto.nombre if fondo_conjunto else 'Cuenta conjunta del hogar'
            transferencias.append({
                'origen': nombre,
                'destino': destino,
                'importe_base': dm['aportacion_hogar_base'],
                'importe_pond': dm['aportacion_hogar_base'],
                'concepto': 'Gastos del hogar',
                'tipo': 'hogar',
                'color': '#ffaa00',
            })

        # Aportaciones a fondos
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
    total_libre_base = sum(dm['libre_base'] for dm in datos_miembros.values())
    total_libre_pond = sum(dm['libre_ponderado'] for dm in datos_miembros.values())
    total_gastos_ind = sum(dm['gastos_individuales'] for dm in datos_miembros.values())
    total_gastos_all = total_gastos_hogar + total_gastos_ind

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
            dst = get_node('Gastos individuales', 'gasto')
            sankey_links.append({'source': src, 'target': dst, 'value': float(dm['gastos_individuales']), 'color': '#ff4d4d'})

        # Aportacion al hogar
        if dm['aportacion_hogar_base'] > 0:
            dst = get_node('Gastos del hogar', 'hogar')
            sankey_links.append({'source': src, 'target': dst, 'value': float(dm['aportacion_hogar_base']), 'color': '#ffaa00'})

        # Aportaciones a fondos
        for ap in dm['aportaciones_fondos']:
            if ap['importe_base'] > 0:
                fondo_nombre = ap['fondo'].nombre if ap['fondo'] else ap['regla'].nombre
                dst = get_node(fondo_nombre, 'fondo')
                sankey_links.append({'source': src, 'target': dst, 'value': float(ap['importe_base']), 'color': ap['regla'].color})

        # Libre restante
        if dm['libre_base'] > 0:
            libre_nombre = f"Libre {nombre}"
            dst = get_node(libre_nombre, 'libre')
            sankey_links.append({'source': src, 'target': dst, 'value': float(dm['libre_base']), 'color': '#00ff88'})

    # Porcentajes barra
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
        'pct_hogar_fijos': pct(gastos_hogar_fijos),
        'pct_hogar_provision': pct(gastos_hogar_provision),
        'pct_hogar_variables': pct(gastos_hogar_variables),
        'pct_libre': pct(total_libre_base),

        'sankey_nodes': sankey_nodes,
        'sankey_links': sankey_links,
    }
