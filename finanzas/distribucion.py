from decimal import Decimal
from .models import FuenteIngreso, CategoriaGasto, PartidaGasto, ReglaReparto, FondoFamiliar
from .fiscal import calcular_neto_anual


def _calcular_neto_fuente(f):
    """Devuelve neto_mensual_base, neto_mensual_ponderado, neto_anual para una fuente."""
    bruto_anual = f.importe_anual_bruto
    estimado_anual = f.importe_anual_estimado

    if f.es_bruto and bruto_anual > 0:
        resultado = calcular_neto_anual(bruto_anual, f.pais_fiscal)
        ratio = resultado['neto'] / bruto_anual
        return (
            round(f.importe_mensual_base * ratio, 2) if f.es_mensual_recurrente and f.incluir_en_mensual else Decimal('0'),
            round(estimado_anual * ratio / Decimal('12'), 2),
            round(estimado_anual * ratio, 2),
        )
    else:
        return (
            f.importe_mensual_base if f.es_mensual_recurrente and f.incluir_en_mensual else Decimal('0'),
            f.importe_mensual_ponderado,
            estimado_anual,
        )


def calcular_distribucion(hogar):
    """
    Motor central: calcula distribucion global del hogar + desglose por miembro.
    """
    miembros = hogar.miembros.select_related('user').all()

    # === INGRESOS POR MIEMBRO ===
    datos_miembros = []
    total_ingreso_base_hogar = Decimal('0')
    total_ingreso_ponderado_hogar = Decimal('0')
    total_ingreso_anual_hogar = Decimal('0')

    for miembro in miembros:
        fuentes = FuenteIngreso.objects.filter(
            usuario=miembro.user, hogar=hogar, activo=True
        )
        ing_base = Decimal('0')
        ing_ponderado = Decimal('0')
        ing_anual = Decimal('0')

        for f in fuentes:
            b, p, a = _calcular_neto_fuente(f)
            ing_base += b
            ing_ponderado += p
            ing_anual += a

        datos_miembros.append({
            'miembro': miembro,
            'ingreso_base': ing_base,
            'ingreso_ponderado': ing_ponderado,
            'ingreso_anual': ing_anual,
        })
        total_ingreso_base_hogar += ing_base
        total_ingreso_ponderado_hogar += ing_ponderado
        total_ingreso_anual_hogar += ing_anual

    # === GASTOS POR RESPONSABLE ===
    partidas = PartidaGasto.objects.filter(
        hogar=hogar, activo=True
    ).select_related('categoria', 'responsable')

    gastos_hogar_fijos = Decimal('0')
    gastos_hogar_provision = Decimal('0')
    gastos_hogar_variables = Decimal('0')

    gastos_por_usuario = {}  # user.id -> {fijos, provision, variables}

    for p in partidas:
        mensual = p.importe_mensual
        tipo = p.categoria.tipo

        if p.responsable is None:
            # Gasto compartido del hogar
            if tipo == 'fijo':
                gastos_hogar_fijos += mensual
            elif tipo == 'anual':
                gastos_hogar_provision += mensual
            else:
                gastos_hogar_variables += mensual
        else:
            # Gasto individual
            uid = p.responsable_id
            if uid not in gastos_por_usuario:
                gastos_por_usuario[uid] = {
                    'fijos': Decimal('0'),
                    'provision': Decimal('0'),
                    'variables': Decimal('0'),
                }
            if tipo == 'fijo':
                gastos_por_usuario[uid]['fijos'] += mensual
            elif tipo == 'anual':
                gastos_por_usuario[uid]['provision'] += mensual
            else:
                gastos_por_usuario[uid]['variables'] += mensual

    total_gastos_hogar = gastos_hogar_fijos + gastos_hogar_provision + gastos_hogar_variables

    # === APORTACION AL FONDO COMUN ===
    # Los gastos compartidos del hogar se reparten entre miembros segun el modo
    # Para simplificar en 5A usamos proporcional al ingreso base
    # En 5B el modo viene de los FondoFamiliar

    # Calcular % de cada miembro sobre el total de ingresos base del hogar
    for dm in datos_miembros:
        uid = dm['miembro'].user.id
        gastos_ind = gastos_por_usuario.get(uid, {'fijos': Decimal('0'), 'provision': Decimal('0'), 'variables': Decimal('0')})
        total_gastos_ind = gastos_ind['fijos'] + gastos_ind['provision'] + gastos_ind['variables']

        # Proporcion del hogar segun ingreso base
        if total_ingreso_base_hogar > 0:
            proporcion = dm['ingreso_base'] / total_ingreso_base_hogar
        else:
            proporcion = Decimal('1') / Decimal(str(len(datos_miembros))) if datos_miembros else Decimal('0')

        aportacion_hogar_base = round(total_gastos_hogar * proporcion, 2)
        aportacion_hogar_ponderado = round(total_gastos_hogar * proporcion, 2)

        libre_base = dm['ingreso_base'] - total_gastos_ind - aportacion_hogar_base
        libre_ponderado = dm['ingreso_ponderado'] - total_gastos_ind - aportacion_hogar_ponderado

        tasa_base = round(libre_base / dm['ingreso_base'] * 100, 1) if dm['ingreso_base'] > 0 else Decimal('0')
        tasa_ponderado = round(libre_ponderado / dm['ingreso_ponderado'] * 100, 1) if dm['ingreso_ponderado'] > 0 else Decimal('0')

        dm['gastos_individuales'] = total_gastos_ind
        dm['gastos_ind_detalle'] = gastos_ind
        dm['aportacion_hogar'] = aportacion_hogar_base
        dm['proporcion'] = round(proporcion * 100, 1)
        dm['libre_base'] = libre_base
        dm['libre_ponderado'] = libre_ponderado
        dm['tasa_base'] = tasa_base
        dm['tasa_ponderado'] = tasa_ponderado

    # === TOTALES DEL HOGAR ===
    total_gastos_individuales = sum(
        g['fijos'] + g['provision'] + g['variables']
        for g in gastos_por_usuario.values()
    )
    total_gastos_all = total_gastos_hogar + total_gastos_individuales

    libre_base_hogar = total_ingreso_base_hogar - total_gastos_all
    libre_ponderado_hogar = total_ingreso_ponderado_hogar - total_gastos_all

    tasa_ahorro_base = round(libre_base_hogar / total_ingreso_base_hogar * 100, 1) if total_ingreso_base_hogar > 0 else Decimal('0')
    tasa_ahorro_ponderado = round(libre_ponderado_hogar / total_ingreso_ponderado_hogar * 100, 1) if total_ingreso_ponderado_hogar > 0 else Decimal('0')

    # Semaforo
    if tasa_ahorro_base >= 20:
        semaforo = 'verde'
        semaforo_texto = 'Excelente salud financiera'
    elif tasa_ahorro_base >= 10:
        semaforo = 'amarillo'
        semaforo_texto = 'Salud financiera aceptable'
    elif tasa_ahorro_base >= 0:
        semaforo = 'naranja'
        semaforo_texto = 'Margen ajustado, revisa gastos'
    else:
        semaforo = 'rojo'
        semaforo_texto = 'Gastas mas de lo que ingresas'

    # === REGLAS DE REPARTO SOBRE EL LIBRE DEL HOGAR ===
    reglas = ReglaReparto.objects.filter(hogar=hogar, activo=True).select_related('fondo')
    reparto_base = []
    reparto_ponderado = []

    total_fijo_reglas = sum(r.importe_fijo for r in reglas if r.tipo_regla == 'fijo')
    libre_base_tras_fijos = libre_base_hogar - total_fijo_reglas
    libre_ponderado_tras_fijos = libre_ponderado_hogar - total_fijo_reglas

    total_porcentaje = sum(r.porcentaje for r in reglas if r.tipo_regla == 'porcentaje')

    for r in reglas:
        if r.tipo_regla == 'porcentaje':
            importe_b = round(libre_base_tras_fijos * r.porcentaje / 100, 2) if libre_base_tras_fijos > 0 else Decimal('0')
            importe_p = round(libre_ponderado_tras_fijos * r.porcentaje / 100, 2) if libre_ponderado_tras_fijos > 0 else Decimal('0')
        else:
            importe_b = r.importe_fijo
            importe_p = r.importe_fijo

        reparto_base.append({'regla': r, 'importe': importe_b})
        reparto_ponderado.append({'regla': r, 'importe': importe_p})

    porcentaje_sin_asignar = max(Decimal('0'), Decimal('100') - total_porcentaje)
    sin_asignar_base = round(libre_base_tras_fijos * porcentaje_sin_asignar / 100, 2) if libre_base_tras_fijos > 0 else Decimal('0')
    sin_asignar_ponderado = round(libre_ponderado_tras_fijos * porcentaje_sin_asignar / 100, 2) if libre_ponderado_tras_fijos > 0 else Decimal('0')

    # === DESGLOSE BARRA ===
    pct_hogar_fijos = round(float(gastos_hogar_fijos / total_ingreso_base_hogar * 100), 1) if total_ingreso_base_hogar > 0 else 0
    pct_hogar_provision = round(float(gastos_hogar_provision / total_ingreso_base_hogar * 100), 1) if total_ingreso_base_hogar > 0 else 0
    pct_hogar_variables = round(float(gastos_hogar_variables / total_ingreso_base_hogar * 100), 1) if total_ingreso_base_hogar > 0 else 0
    pct_ind = round(float(total_gastos_individuales / total_ingreso_base_hogar * 100), 1) if total_ingreso_base_hogar > 0 else 0
    pct_libre = round(float(tasa_ahorro_base), 1) if tasa_ahorro_base > 0 else 0

    return {
        'datos_miembros': datos_miembros,

        'ingreso_base_hogar': total_ingreso_base_hogar,
        'ingreso_ponderado_hogar': total_ingreso_ponderado_hogar,
        'ingreso_anual_hogar': total_ingreso_anual_hogar,

        'gastos_hogar_fijos': gastos_hogar_fijos,
        'gastos_hogar_provision': gastos_hogar_provision,
        'gastos_hogar_variables': gastos_hogar_variables,
        'total_gastos_hogar': total_gastos_hogar,
        'total_gastos_individuales': total_gastos_individuales,
        'total_gastos_all': total_gastos_all,

        'libre_base_hogar': libre_base_hogar,
        'libre_ponderado_hogar': libre_ponderado_hogar,

        'tasa_ahorro_base': tasa_ahorro_base,
        'tasa_ahorro_ponderado': tasa_ahorro_ponderado,
        'semaforo': semaforo,
        'semaforo_texto': semaforo_texto,

        'reglas': reglas,
        'reparto_base': reparto_base,
        'reparto_ponderado': reparto_ponderado,
        'total_porcentaje': total_porcentaje,
        'porcentaje_sin_asignar': porcentaje_sin_asignar,
        'sin_asignar_base': sin_asignar_base,
        'sin_asignar_ponderado': sin_asignar_ponderado,

        'pct_hogar_fijos': pct_hogar_fijos,
        'pct_hogar_provision': pct_hogar_provision,
        'pct_hogar_variables': pct_hogar_variables,
        'pct_ind': pct_ind,
        'pct_libre': pct_libre,
    }
