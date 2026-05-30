import datetime
from decimal import Decimal
from .models import TablaIRPF, CotizacionSS


def _resolver_año(año):
    """Año fiscal: si no se pasa, usa el actual."""
    return año or datetime.date.today().year


def _obtener_tramos(pais, año):
    """Obtiene tramos IRPF, con fallback al último año disponible."""
    tramos = TablaIRPF.objects.filter(pais=pais, año=año).order_by('tramo_desde')
    if tramos.exists():
        return tramos

    ultimo = TablaIRPF.objects.filter(pais=pais).order_by('-año').first()
    if ultimo:
        return TablaIRPF.objects.filter(pais=pais, año=ultimo.año).order_by('tramo_desde')
    return TablaIRPF.objects.none()


def _obtener_cotizaciones(pais, año):
    """Obtiene cotizaciones SS, con fallback al último año disponible."""
    cotizaciones = CotizacionSS.objects.filter(pais=pais, año=año)
    if cotizaciones.exists():
        return cotizaciones

    ultimo = CotizacionSS.objects.filter(pais=pais).order_by('-año').first()
    if ultimo:
        return CotizacionSS.objects.filter(pais=pais, año=ultimo.año)
    return CotizacionSS.objects.none()


def _aplicar_tramos(base_liquidable, tramos):
    """Aplica tramos progresivos sobre una base liquidable."""
    impuesto = Decimal('0')
    restante = base_liquidable

    for tramo in tramos:
        if restante <= 0:
            break
        tope = tramo.tramo_hasta if tramo.tramo_hasta else Decimal('999999999')
        base_tramo = min(restante, tope - tramo.tramo_desde)
        if base_tramo > 0:
            impuesto += base_tramo * (tramo.porcentaje / 100)
            restante -= base_tramo

    return impuesto


# ── Constantes España ─────────────────────────────────────────────────────
# Estos valores cambian poco entre años. Si en el futuro necesitas
# parametrizarlos por año, muévelos a un modelo (DeduccionFiscal).
ES_GASTOS_DEDUCIBLES = Decimal('2000')   # Reducción por rendimientos del trabajo
ES_MINIMO_PERSONAL = Decimal('5550')     # Mínimo personal contribuyente


def calcular_ss(bruto_anual, pais='ES', año=None):
    """Calcula la cotización a la Seguridad Social del trabajador."""
    año = _resolver_año(año)
    cotizaciones = _obtener_cotizaciones(pais, año)

    if not cotizaciones.exists():
        return Decimal('0')

    total_porcentaje = sum(c.porcentaje_trabajador for c in cotizaciones)
    return round(Decimal(str(bruto_anual)) * (total_porcentaje / 100), 2)


def calcular_irpf(bruto_anual, pais='ES', año=None, ss=None):
    """
    Calcula el IRPF (retención) para un ingreso bruto anual.

    Para España aplica el cálculo real de retención:
      1. Base liquidable = bruto - SS - gastos deducibles (2.000€)
      2. Cuota íntegra = tramos progresivos sobre base liquidable
      3. Cuota líquida = cuota íntegra - cuota mínimo personal
    """
    año = _resolver_año(año)
    tramos = _obtener_tramos(pais, año)

    if not tramos.exists():
        return Decimal('0')

    bruto = Decimal(str(bruto_anual))

    if pais == 'ES':
        # SS del trabajador (si no se pasa, la calculamos)
        if ss is None:
            ss = calcular_ss(bruto_anual, pais, año)

        # 1. Base liquidable
        base_liquidable = max(bruto - ss - ES_GASTOS_DEDUCIBLES, Decimal('0'))

        # 2. Cuota íntegra
        cuota_integra = _aplicar_tramos(base_liquidable, tramos)

        # 3. Cuota mínimo personal (mismos tramos, sobre el mínimo)
        cuota_minimo = _aplicar_tramos(ES_MINIMO_PERSONAL, tramos)

        # 4. Cuota líquida (nunca negativa)
        return round(max(cuota_integra - cuota_minimo, Decimal('0')), 2)

    else:
        # Otros países: cálculo directo sobre bruto (simplificado)
        return round(_aplicar_tramos(bruto, tramos), 2)


def calcular_neto_anual(bruto_anual, pais='ES', año=None):
    """Calcula el neto anual: bruto - IRPF - SS."""
    año = _resolver_año(año)
    ss = calcular_ss(bruto_anual, pais, año)
    irpf = calcular_irpf(bruto_anual, pais, año, ss=ss)
    bruto = Decimal(str(bruto_anual))
    neto = bruto - irpf - ss

    return {
        'bruto': bruto,
        'irpf': irpf,
        'ss': ss,
        'neto': round(neto, 2),
        'tipo_efectivo': round((irpf / bruto) * 100, 2) if bruto > 0 else Decimal('0'),
    }


def calcular_neto_mensual(bruto_anual, pais='ES', año=None):
    """Neto mensual equivalente."""
    resultado = calcular_neto_anual(bruto_anual, pais, año)
    resultado['neto_mensual'] = round(resultado['neto'] / 12, 2)
    return resultado
