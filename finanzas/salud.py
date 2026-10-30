"""Los indicadores básicos de salud financiera de un hogar.

Cinco cifras que cualquier asesor miraría primero y que la app tenía
repartidas (o no tenía): cuántos meses aguantaríais sin ingresos, qué parte de
lo que entra se ahorra, qué parte se va en hipotecas, cuánto se debe frente a
lo que valen las casas y qué parte del dinero está invertida.

Cada indicador lleva su semáforo y una explicación con el criterio general que
se usa para valorarlo. Son orientaciones de manual, no un consejo a medida.
"""

import datetime
from decimal import Decimal

_CERO = Decimal('0')


def _cuota_proxima(hipoteca, hoy):
    """La próxima cuota del cuadro de amortización (o la conocida)."""
    try:
        for fila in hipoteca.cuadro():
            if fila['fecha'] > hoy:
                return Decimal(str(fila['cuota']))
    except Exception:  # un cuadro mal declarado no tumba el panel
        pass
    return Decimal(str(hipoteca.cuota_conocida or 0))


def _pct(parte, total):
    return float(parte) / float(total) * 100 if total else None


def indicadores(hogar, liquidez, inversiones, ingreso_mensual, gasto_mensual, tasa_ahorro, hoy=None):
    from .models import Hipoteca, Propiedad

    hoy = hoy or datetime.date.today()
    liquidez = Decimal(str(liquidez or 0))
    inversiones = Decimal(str(inversiones or 0))
    ingreso_mensual = Decimal(str(ingreso_mensual or 0))
    gasto_mensual = Decimal(str(gasto_mensual or 0))

    propiedades = list(Propiedad.objects.filter(hogar=hogar, activo=True))
    valor_casas = sum((p.valor_actual or _CERO for p in propiedades), _CERO)
    deuda = sum((p.deuda_hipotecaria or _CERO for p in propiedades), _CERO)
    cuotas = sum(
        (_cuota_proxima(h, hoy) for h in Hipoteca.objects.filter(propiedad__hogar=hogar, activa=True)),
        _CERO,
    )

    salida = []

    # 1. Colchón: meses de gasto que cubre el dinero disponible.
    if gasto_mensual > 0:
        meses = float(liquidez / gasto_mensual)
        if meses < 3:
            estado, frase = 'mal', 'por debajo de los 3 meses mínimos'
        elif meses < 6:
            estado, frase = 'atencion', 'aceptable; lo cómodo son 6 meses'
        elif meses <= 12:
            estado, frase = 'bien', 'un colchón sólido'
        else:
            estado, frase = 'info', 'más de un año: hay dinero que podría trabajar'
        salida.append({
            'clave': 'colchon', 'nombre': 'Colchón', 'valor': f'{meses:,.0f}'.replace(',', '.'),
            'unidad': 'meses', 'estado': estado, 'frase': frase,
            'detalle': f'{_eur(liquidez)} disponibles ÷ {_eur(gasto_mensual)} de gasto al mes.',
            'criterio': 'Lo habitual es tener entre 3 y 6 meses de gastos a mano para imprevistos '
                        '(un trabajo que se pierde, una avería grande). Con ingresos públicos o muy '
                        'estables bastan 3; con hijos o un solo sueldo, mejor 6. Por encima de 12, '
                        'salvo que se guarde para algo concreto (una entrada de casa), el exceso '
                        'pierde frente a la inflación.',
            'enlace': '/finanzas/evolucion/',
        })

    # 2. Tasa de ahorro.
    if tasa_ahorro is not None and ingreso_mensual > 0:
        t = float(tasa_ahorro)
        estado = 'bien' if t >= 20 else 'atencion' if t >= 10 else 'mal'
        salida.append({
            'clave': 'ahorro', 'nombre': 'Tasa de ahorro', 'valor': f'{t:.0f}', 'unidad': '%',
            'estado': estado,
            'frase': 'muy por encima de la media' if t >= 30 else 'en la zona buena' if t >= 20 else 'mejorable',
            'detalle': 'Lo que no se gasta, sobre lo que entra cada mes según el presupuesto.',
            'criterio': 'La regla 50/30/20 propone ahorrar al menos el 20 % de lo que entra. '
                        'Los hogares españoles ahorran de media en torno al 10–13 %.',
            'enlace': '/finanzas/distribucion/',
        })

    # 3. Endeudamiento: cuotas sobre ingresos.
    if cuotas > 0 and ingreso_mensual > 0:
        e = _pct(cuotas, ingreso_mensual)
        estado = 'bien' if e <= 30 else 'atencion' if e <= 35 else 'mal'
        salida.append({
            'clave': 'endeudamiento', 'nombre': 'Hipotecas sobre ingresos', 'valor': f'{e:.0f}', 'unidad': '%',
            'estado': estado,
            'frase': 'holgado' if e <= 20 else 'cómodo' if e <= 30 else 'al límite de la banca' if e <= 35 else 'por encima del límite de la banca',
            'detalle': f'{_eur(cuotas)} de cuotas al mes ÷ {_eur(ingreso_mensual)} de ingresos.',
            'criterio': 'Los bancos no suelen conceder un préstamo si todas las cuotas juntas pasan '
                        'del 35 % de los ingresos netos. Por debajo del 30 % se considera cómodo.',
            'enlace': '/finanzas/propiedades/',
        })

    # 4. Deuda frente al valor de las casas (LTV).
    if valor_casas > 0:
        ltv = _pct(deuda, valor_casas)
        estado = 'bien' if ltv <= 50 else 'atencion' if ltv <= 80 else 'mal'
        salida.append({
            'clave': 'ltv', 'nombre': 'Deuda sobre las casas', 'valor': f'{ltv:.0f}', 'unidad': '%',
            'estado': estado,
            'frase': 'casi todo es vuestro' if ltv <= 30 else 'la mayor parte es vuestra' if ltv <= 50 else 'normal en los primeros años' if ltv <= 80 else 'alto',
            'detalle': f'{_eur(deuda)} pendientes sobre {_eur(valor_casas)} de valor.',
            'criterio': 'Es lo que los bancos llaman LTV. Al comprar suele estar en el 80 %; por '
                        'debajo del 50 % la casa es mayoritariamente vuestra y una bajada de precios '
                        'no os dejaría debiendo más de lo que vale.',
            'enlace': '/finanzas/propiedades/',
        })

    # 5. Parte invertida del dinero.
    total_fin = liquidez + inversiones
    if total_fin > 0:
        inv = _pct(inversiones, total_fin)
        if inv < 10:
            estado, frase = 'info', 'casi todo en cuentas y depósitos'
        elif inv <= 60:
            estado, frase = 'bien', 'repartido'
        else:
            estado, frase = 'atencion', 'muy expuesto a la bolsa'
        salida.append({
            'clave': 'invertido', 'nombre': 'Dinero invertido', 'valor': f'{inv:.0f}', 'unidad': '%',
            'estado': estado, 'frase': frase,
            'detalle': f'{_eur(inversiones)} invertidos de {_eur(total_fin)} entre cuentas, depósitos e inversiones.',
            'criterio': 'No hay una cifra única: depende de cuándo se va a necesitar el dinero. El '
                        'criterio habitual es guardar a mano el colchón y lo que se vaya a gastar en '
                        'los próximos 3–5 años (una entrada de casa, por ejemplo) y, el resto, '
                        'invertirlo a largo plazo y diversificado. Como referencia orientativa se suele '
                        'hablar de un 20–30 % del dinero invertido para un hogar joven.',
            'enlace': '/finanzas/inversiones/',
        })

    return salida


def _eur(v):
    texto = f'{Decimal(str(v)):,.0f}'.replace(',', '.')
    return f'{texto} €'
