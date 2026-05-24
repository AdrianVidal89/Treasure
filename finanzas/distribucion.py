from decimal import Decimal
from .models import FuenteIngreso, CategoriaGasto, PartidaGasto, ReglaReparto
from .fiscal import calcular_neto_anual


def calcular_distribucion(hogar):
    """
    Motor central: cruza ingresos netos vs gastos mensuales del hogar.
    Devuelve un dict completo con todos los KPIs de distribucion.
    """

    # === INGRESOS ===
    fuentes = FuenteIngreso.objects.filter(hogar=hogar, activo=True).select_related('destino')

    total_mensual_base = Decimal('0')
    total_mensual_ponderado = Decimal('0')
    total_anual_neto = Decimal('0')

    for f in fuentes:
        bruto_anual = f.importe_anual_bruto
        estimado_anual = f.importe_anual_estimado

        if f.es_bruto and bruto_anual > 0:
            resultado = calcular_neto_anual(bruto_anual, f.pais_fiscal)
            ratio_neto = resultado['neto'] / bruto_anual if bruto_anual > 0 else Decimal('1')

            neto_mensual_base = round(f.importe_mensual_base * ratio_neto, 2)
            neto_mensual_ponderado = round(estimado_anual * ratio_neto / Decimal('12'), 2)
            neto_anual = round(estimado_anual * ratio_neto, 2)
        else:
            neto_mensual_base = f.importe_mensual_base
            neto_mensual_ponderado = f.importe_mensual_ponderado
            neto_anual = estimado_anual

        if f.es_mensual_recurrente and f.incluir_en_mensual:
            total_mensual_base += neto_mensual_base
        total_mensual_ponderado += neto_mensual_ponderado
        total_anual_neto += neto_anual

    # === GASTOS ===
    partidas = PartidaGasto.objects.filter(hogar=hogar, activo=True).select_related('categoria')

    gastos_fijos = Decimal('0')
    gastos_provision = Decimal('0')
    gastos_variables = Decimal('0')

    for p in partidas:
        mensual = p.importe_mensual
        if p.categoria.tipo == 'fijo':
            gastos_fijos += mensual
        elif p.categoria.tipo == 'anual':
            gastos_provision += mensual
        elif p.categoria.tipo == 'variable':
            gastos_variables += mensual

    total_gastos = gastos_fijos + gastos_provision + gastos_variables

    # === CAPACIDAD DE AHORRO ===
    libre_base = total_mensual_base - total_gastos
    libre_ponderado = total_mensual_ponderado - total_gastos

    # Tasa de ahorro
    tasa_ahorro_base = round((libre_base / total_mensual_base * 100), 1) if total_mensual_base > 0 else Decimal('0')
    tasa_ahorro_ponderado = round((libre_ponderado / total_mensual_ponderado * 100), 1) if total_mensual_ponderado > 0 else Decimal('0')

    # Semaforo de salud financiera
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

    # === REGLAS DE REPARTO ===
    reglas = ReglaReparto.objects.filter(hogar=hogar, activo=True)
    reparto_base = []
    reparto_ponderado = []
    total_porcentaje = sum(r.porcentaje for r in reglas)

    for r in reglas:
        importe_base = round(libre_base * r.porcentaje / 100, 2) if libre_base > 0 else Decimal('0')
        importe_ponderado = round(libre_ponderado * r.porcentaje / 100, 2) if libre_ponderado > 0 else Decimal('0')
        reparto_base.append({
            'regla': r,
            'importe': importe_base,
        })
        reparto_ponderado.append({
            'regla': r,
            'importe': importe_ponderado,
        })

    # Porcentaje no asignado
    porcentaje_sin_asignar = max(Decimal('0'), Decimal('100') - total_porcentaje)
    importe_sin_asignar_base = round(libre_base * porcentaje_sin_asignar / 100, 2) if libre_base > 0 else Decimal('0')
    importe_sin_asignar_ponderado = round(libre_ponderado * porcentaje_sin_asignar / 100, 2) if libre_ponderado > 0 else Decimal('0')

    # === DESGLOSE PARA GRAFICO ===
    # Porcentaje de cada bloque sobre el ingreso total
    pct_fijos = round(gastos_fijos / total_mensual_base * 100, 1) if total_mensual_base > 0 else 0
    pct_provision = round(gastos_provision / total_mensual_base * 100, 1) if total_mensual_base > 0 else 0
    pct_variables = round(gastos_variables / total_mensual_base * 100, 1) if total_mensual_base > 0 else 0
    pct_libre = round(float(tasa_ahorro_base), 1) if tasa_ahorro_base > 0 else 0

    return {
        # Ingresos
        'ingreso_mensual_base': total_mensual_base,
        'ingreso_mensual_ponderado': total_mensual_ponderado,
        'ingreso_anual_neto': total_anual_neto,

        # Gastos
        'gastos_fijos': gastos_fijos,
        'gastos_provision': gastos_provision,
        'gastos_variables': gastos_variables,
        'total_gastos': total_gastos,

        # Libre
        'libre_base': libre_base,
        'libre_ponderado': libre_ponderado,

        # Tasas
        'tasa_ahorro_base': tasa_ahorro_base,
        'tasa_ahorro_ponderado': tasa_ahorro_ponderado,

        # Semaforo
        'semaforo': semaforo,
        'semaforo_texto': semaforo_texto,

        # Reparto
        'reglas': reglas,
        'reparto_base': reparto_base,
        'reparto_ponderado': reparto_ponderado,
        'total_porcentaje': total_porcentaje,
        'porcentaje_sin_asignar': porcentaje_sin_asignar,
        'importe_sin_asignar_base': importe_sin_asignar_base,
        'importe_sin_asignar_ponderado': importe_sin_asignar_ponderado,

        # Desglose porcentual
        'pct_fijos': pct_fijos,
        'pct_provision': pct_provision,
        'pct_variables': pct_variables,
        'pct_libre': pct_libre,
    }
