from decimal import Decimal
from .models import TablaIRPF, CotizacionSS


def calcular_irpf(bruto_anual, pais='ES', año=2025):
    """Calcula el IRPF por tramos para un ingreso bruto anual."""
    tramos = TablaIRPF.objects.filter(pais=pais, año=año).order_by('tramo_desde')

    if not tramos.exists():
        return Decimal('0')

    impuesto_total = Decimal('0')
    restante = Decimal(str(bruto_anual))

    for tramo in tramos:
        if restante <= 0:
            break

        limite_superior = tramo.tramo_hasta if tramo.tramo_hasta else Decimal('999999999')
        base_tramo = min(restante, limite_superior - tramo.tramo_desde)

        if base_tramo > 0:
            impuesto_total += base_tramo * (tramo.porcentaje / 100)
            restante -= base_tramo

    return round(impuesto_total, 2)


def calcular_ss(bruto_anual, pais='ES', año=2025):
    """Calcula la cotización a la Seguridad Social del trabajador."""
    cotizaciones = CotizacionSS.objects.filter(pais=pais, año=año)

    if not cotizaciones.exists():
        return Decimal('0')

    total_porcentaje = sum(c.porcentaje_trabajador for c in cotizaciones)
    return round(Decimal(str(bruto_anual)) * (total_porcentaje / 100), 2)


def calcular_neto_anual(bruto_anual, pais='ES', año=2025):
    """Calcula el neto anual: bruto - IRPF - SS."""
    irpf = calcular_irpf(bruto_anual, pais, año)
    ss = calcular_ss(bruto_anual, pais, año)
    neto = Decimal(str(bruto_anual)) - irpf - ss
    return {
        'bruto': Decimal(str(bruto_anual)),
        'irpf': irpf,
        'ss': ss,
        'neto': round(neto, 2),
        'tipo_efectivo': round((irpf / Decimal(str(bruto_anual))) * 100, 2) if bruto_anual > 0 else Decimal('0'),
    }


def calcular_neto_mensual(bruto_anual, pais='ES', año=2025):
    """Neto mensual equivalente."""
    resultado = calcular_neto_anual(bruto_anual, pais, año)
    resultado['neto_mensual'] = round(resultado['neto'] / 12, 2)
    return resultado