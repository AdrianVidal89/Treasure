"""El ahorro del mes contado como lo cuenta una familia: todo lo que no se gasta.

El motor de distribución separa «ahorro», «inversión» y «libre» según el tipo
de fondo al que va cada euro. Pero si el presupuesto ya incluye el gasto
discrecional, lo que sobra no es dinero libre para gastar: es dinero que no se
va a usar, o sea, ahorro. Aquí se cuenta así:

    ahorro = ingresos − gastos

y se desglosa por el sitio al que va cada euro (Bankinter, el depósito, el
fondo indexado…), para que se vea dónde acaba.

Funciona igual con un flujo en vivo y con la foto de un mes cerrado: solo
usa claves que guardan las dos (ver `cierres.CLAVES_DETALLE`).
"""

from decimal import Decimal

_CERO = Decimal('0')
_CENT = Decimal('0.01')

# Cómo se enseña cada clase de destino.
CLASES = {
    'ahorro': 'Ahorro',
    'inversion': 'Inversión',
    'colchon': 'Sobra en un fondo de gastos',
    'en_cuenta': 'Se queda en la cuenta de cada uno',
    'sin_fondo': 'Gastos que no paga ningún fondo',
    'falta': 'Falta por cubrir',
}


def _attr(obj, nombre, defecto=''):
    if isinstance(obj, dict):
        return obj.get(nombre, defecto)
    return getattr(obj, nombre, defecto)


def desglose_ahorro(d):
    """Devuelve el ahorro del mes y a dónde va cada euro.

    `destinos` suma exactamente el ahorro: lo que va a cada fondo de ahorro o
    inversión, lo que sobra en un fondo de gastos, lo que se queda en la
    cuenta de alguien, y en negativo los gastos que no tienen fondo (se
    pagarán de alguno de esos sitios) o lo que falta para cubrir un fondo.
    """
    ingreso = d.get('ingreso_base_hogar') or _CERO
    gastos = d.get('total_gastos_all') or _CERO
    ahorro = ingreso - gastos

    destinos = []
    cubiertos = _CERO
    for fa in d.get('fondos_aportaciones') or []:
        fondo = fa.get('fondo')
        tipo = _attr(fondo, 'tipo_fondo', 'comun')
        neta = fa.get('total_aportacion_neta') or _CERO
        cub = fa.get('gastos_cubiertos') or _CERO
        cubiertos += cub

        if tipo == 'inversion':
            clase, importe = 'inversion', neta - cub
        elif tipo == 'ahorro':
            clase, importe = 'ahorro', neta - cub
        elif cub > 0:
            # Un fondo que paga gastos: lo que le sobra es ahorro que se queda
            # allí; si le falta, es dinero que tendrá que salir de otro sitio.
            importe = neta - cub
            clase = 'colchon' if importe > 0 else 'falta'
        else:
            # Una cuenta que no paga nada (Bankinter, una cuenta remunerada):
            # lo que entra se guarda.
            clase, importe = 'ahorro', neta

        if abs(importe) < _CENT:
            continue
        destinos.append({
            'nombre': _attr(fondo, 'nombre', 'Fondo'),
            'color': _attr(fondo, 'color', '') or '',
            'cuenta': _attr(fondo, 'cuenta_asociada', '') or '',
            'clase': clase,
            'importe': importe,
        })

    sin_fondo = (d.get('gastos_hogar_total') or _CERO) - cubiertos
    if sin_fondo >= _CENT:
        destinos.append({
            'nombre': CLASES['sin_fondo'], 'color': '', 'cuenta': '',
            'clase': 'sin_fondo', 'importe': -sin_fondo,
        })

    resto = ahorro - sum((x['importe'] for x in destinos), _CERO)
    if abs(resto) >= _CENT:
        destinos.append({
            'nombre': CLASES['en_cuenta'] if resto > 0 else CLASES['falta'],
            'color': '', 'cuenta': '',
            'clase': 'en_cuenta' if resto > 0 else 'falta',
            'importe': resto,
        })

    positivos = sorted((x for x in destinos if x['importe'] > 0), key=lambda x: -x['importe'])
    negativos = sorted((x for x in destinos if x['importe'] <= 0), key=lambda x: x['importe'])
    bruto = sum((x['importe'] for x in positivos), _CERO)

    def pct(v, base):
        return round(float(v / base * 100), 1) if base > 0 else 0.0

    for x in positivos:
        # Peso dentro de lo que se aparta (para la barra) y sobre lo que entra.
        x['pct_barra'] = pct(x['importe'], bruto)
        x['pct_ingreso'] = pct(x['importe'], ingreso)
    for x in negativos:
        x['pct_ingreso'] = pct(-x['importe'], ingreso)

    inversion = sum((x['importe'] for x in positivos if x['clase'] == 'inversion'), _CERO)
    return {
        'ingreso': ingreso,
        'gastos': gastos,
        'ahorro': ahorro,
        'inversion': inversion,
        'guardado': ahorro - inversion,
        'tasa': pct(ahorro, ingreso),
        'pct_gastos': pct(gastos, ingreso) if ahorro >= 0 else 100.0,
        'pct_ahorro': pct(max(ahorro, _CERO), ingreso),
        'aportado': bruto,
        'destinos': positivos,
        'restas': negativos,
        'en_numeros_rojos': ahorro < 0,
    }
