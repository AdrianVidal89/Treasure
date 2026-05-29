"""
Motor de distribución de Treasure.

Cambios respecto a la versión anterior:
- Eliminado Sankey
- calcular_flujos(hogar, mes, anio) acepta mes concreto
- En el mes seleccionado se suman ingresos base + puntuales de ese mes
- Fix cascada: fondos muestran total_aportacion_neta (entradas - salidas por subsobres)
"""
from decimal import Decimal
import datetime

from .models import FuenteIngreso, PartidaGasto, ReglaReparto, FondoFamiliar, SubsobreFondo
from .fiscal import calcular_neto_anual


# ---------------------------------------------------------------------------
# Helpers de ingreso
# ---------------------------------------------------------------------------

def _neto_fuente_mes(f, mes: int):
    """
    Devuelve (ingreso_base_mes, ingreso_ponderado_mes).

    ingreso_base_mes  = lo que realmente cae en la cuenta ese mes
                        (nómina base + paga extra si corresponde ese mes
                         + cobros periódicos cuyo mes coincide)
    ingreso_ponderado = anual_estimado / 12  (para comparativas)
    """
    bruto_anual = f.importe_anual_bruto
    estimado_anual = f.importe_anual_estimado

    # Ratio neto/bruto fiscal
    if f.es_bruto and bruto_anual > 0:
        resultado = calcular_neto_anual(bruto_anual, f.pais_fiscal)
        ratio = resultado['neto'] / bruto_anual
    else:
        ratio = Decimal('1')

    pond = round(estimado_anual * ratio / Decimal('12'), 2)

    # --- Ingresos mensuales recurrentes ---
    if f.es_mensual_recurrente and f.incluir_en_mensual:
        base_mensual = round(f.importe_mensual_base * ratio, 2)
    else:
        base_mensual = Decimal('0')

    # --- Extras que caen este mes ---
    extra_mes = Decimal('0')

    if f.modo_entrada == 'anual' and f.num_pagas > 12:
        if mes in f.pagas_extras_meses:
            extra_mes += round(f.importe_paga_extra * ratio, 2)

    elif f.modo_entrada == 'periodo' and f.periodicidad != 'mensual':
        if mes in f.cobro_meses:
            extra_mes += round(f.importe_declarado * ratio, 2)

    base_total = base_mensual + extra_mes
    return base_total, pond


def _neto_fuente_base(f):
    """Ingreso base mensual 'normal' (sin extras de mes): para comparar."""
    bruto_anual = f.importe_anual_bruto
    estimado_anual = f.importe_anual_estimado

    if f.es_bruto and bruto_anual > 0:
        resultado = calcular_neto_anual(bruto_anual, f.pais_fiscal)
        ratio = resultado['neto'] / bruto_anual
        base = round(f.importe_mensual_base * ratio, 2) if f.es_mensual_recurrente and f.incluir_en_mensual else Decimal('0')
    else:
        base = f.importe_mensual_base if f.es_mensual_recurrente and f.incluir_en_mensual else Decimal('0')

    return base, pond if 'pond' in dir() else Decimal('0')


def _neto_fuente_base(f):
    """Ingreso base mensual 'normal' (sin extras de mes): para comparar."""
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

def calcular_flujos(hogar, mes: int = None, anio: int = None):
    """
    Motor de flujos de Treasure.

    Args:
        hogar:  instancia de Hogar
        mes:    1-12. Si None, usa el mes actual.
        anio:   año. Si None, usa el año actual.

    Flujo de cálculo:
      1. Ingresos: base mensual + puntuales del mes seleccionado
      2. Gastos individuales: se restan del libre personal
         Gastos del hogar: informativos
      3. Reglas de reparto → aportaciones a fondos
      4. Subsobres (cascada): redistribución interna entre fondos
      5. Transferencias: lista de movimientos bancarios necesarios
      6. KPIs globales
    """
    hoy = datetime.date.today()
    mes = mes or hoy.month
    anio = anio or hoy.year

    miembros = hogar.miembros.select_related('user').all()
    partidas = PartidaGasto.objects.filter(
        hogar=hogar, activo=True
    ).select_related('categoria', 'responsable')
    reglas = ReglaReparto.objects.filter(
        hogar=hogar, activo=True
    ).select_related('fondo', 'usuario').order_by('orden')
    fondos = list(FondoFamiliar.objects.filter(hogar=hogar, activo=True))

    # Subsobres: redistribución interna entre fondos
    # Campos reales: fondo (FK origen), nombre, tipo, importe_manual, fondo_destino (FK nullable)
    subsobres = list(
        SubsobreFondo.objects.filter(
            fondo__hogar=hogar, activo=True
        ).select_related('fondo', 'fondo_destino')
    )

    # =========================================================
    # PASO 1: Ingresos por miembro (base mensual + extras del mes)
    # =========================================================
    datos_miembros = {}
    total_base_hogar = Decimal('0')
    total_pond_hogar = Decimal('0')
    total_anual_hogar = Decimal('0')

    for m in miembros:
        fuentes = FuenteIngreso.objects.filter(usuario=m.user, hogar=hogar, activo=True)
        ing_base = Decimal('0')
        ing_pond = Decimal('0')
        ing_anual = Decimal('0')
        extras_mes = []

        for f in fuentes:
            b, p = _neto_fuente_mes(f, mes)
            b_base, _ = _neto_fuente_base(f)

            extra = b - b_base
            if extra > 0:
                extras_mes.append({'fuente': f.nombre, 'importe': extra})

            ing_base += b
            ing_pond += p

            bruto_anual = f.importe_anual_bruto
            est_anual = f.importe_anual_estimado
            if f.es_bruto and bruto_anual > 0:
                res = calcular_neto_anual(bruto_anual, f.pais_fiscal)
                ratio = res['neto'] / bruto_anual
                ing_anual += round(est_anual * ratio, 2)
            else:
                ing_anual += est_anual

        datos_miembros[m.user.id] = {
            'miembro': m,
            'ingreso_base': ing_base,
            'ingreso_ponderado': ing_pond,
            'ingreso_anual': ing_anual,
            'extras_mes': extras_mes,
            'gastos_individuales': Decimal('0'),
            'disponible_base': ing_base,
            'disponible_pond': ing_pond,
            'aportacion_hogar_informativa': Decimal('0'),
            'proporcion': Decimal('0'),
            'aportaciones_fondos': [],
            'libre_base': ing_base,
            'libre_ponderado': ing_pond,
        }
        total_base_hogar += ing_base
        total_pond_hogar += ing_pond
        total_anual_hogar += ing_anual

    # =========================================================
    # PASO 2: Gastos
    # =========================================================
    gastos_hogar_fijos = Decimal('0')
    gastos_hogar_provision = Decimal('0')
    gastos_hogar_variables = Decimal('0')

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
        else:
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

    # =========================================================
    # PASO 3: Reglas de reparto → aportaciones a fondos
    # =========================================================
    fondos_aportaciones = {f.id: {
        'fondo': f,
        'total_aportacion_base': Decimal('0'),
        'total_aportacion_pond': Decimal('0'),
        'aportantes': [],
        'total_cascada_saliente': Decimal('0'),
        'subsobres': [],
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
                'regla': regla,
                'fondo': regla.fondo,
                'importe_base': importe_b,
                'importe_pond': importe_p,
            })

            if regla.fondo_id and regla.fondo_id in fondos_aportaciones:
                fa = fondos_aportaciones[regla.fondo_id]
                fa['total_aportacion_base'] += importe_b
                fa['total_aportacion_pond'] += importe_p
                fa['aportantes'].append({
                    'miembro': dm['miembro'],
                    'importe_base': importe_b,
                    'importe_pond': importe_p,
                })

        else:
            # Regla global
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
                    'regla': regla,
                    'fondo': regla.fondo,
                    'importe_base': importe_b,
                    'importe_pond': importe_p,
                })

                if regla.fondo_id and regla.fondo_id in fondos_aportaciones:
                    fa = fondos_aportaciones[regla.fondo_id]
                    fa['total_aportacion_base'] += importe_b
                    fa['total_aportacion_pond'] += importe_p
                    fa['aportantes'].append({
                        'miembro': dm['miembro'],
                        'importe_base': importe_b,
                        'importe_pond': importe_p,
                    })

    # =========================================================
    # PASO 4: Subsobres / cascada
    # SubsobreFondo campos reales:
    #   fondo (FK origen), nombre, tipo (choices), importe_manual,
    #   fondo_destino (FK nullable), orden, activo
    # =========================================================
    transferencias_cascada = []

    for ss in subsobres:
        if ss.fondo_id not in fondos_aportaciones:
            continue

        fa_origen = fondos_aportaciones[ss.fondo_id]

        # importe_manual es el importe fijo que sale del fondo
        importe = ss.importe_manual or Decimal('0')
        if importe <= 0:
            continue

        # Registrar salida en el fondo origen
        fa_origen['total_cascada_saliente'] += importe
        fa_origen['subsobres'].append({
            'id': ss.id,
            'nombre': ss.nombre,
            'tipo': ss.tipo,
            'importe': importe,
            'fondo_destino': ss.fondo_destino,
        })

        # Registrar entrada en el fondo destino (si existe)
        if ss.fondo_destino_id and ss.fondo_destino_id in fondos_aportaciones:
            fa_destino = fondos_aportaciones[ss.fondo_destino_id]
            fa_destino['total_aportacion_base'] += importe
            fa_destino['total_aportacion_pond'] += importe

        transferencias_cascada.append({
            'subsobre_id': ss.id,
            'origen': ss.fondo.nombre,
            'destino': ss.fondo_destino.nombre if ss.fondo_destino else ss.nombre,
            'importe': importe,
            'nombre': ss.nombre,
            'tipo': ss.tipo,
            'color': ss.fondo.color,
        })

    # Calcular neto para cada fondo
    for fid, fa in fondos_aportaciones.items():
        fa['total_aportacion_neta'] = fa['total_aportacion_base'] - fa['total_cascada_saliente']

    # =========================================================
    # PASO 5: Transferencias bancarias necesarias
    # =========================================================
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

    # Transferencias de cascada
    for tc in transferencias_cascada:
        transferencias.append({
            'origen': f"Fondo: {tc['origen']}",
            'destino': tc['destino'],
            'cuenta': '',
            'importe_base': tc['importe'],
            'importe_pond': tc['importe'],
            'concepto': tc['nombre'],
            'tipo': 'cascada',
            'color': tc['color'],
        })

    # =========================================================
    # PASO 6: KPIs globales
    # =========================================================
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
        semaforo, semaforo_texto = 'rojo', 'Gastas más de lo que ingresas'

    def pct(val):
        return round(float(val / total_base_hogar * 100), 1) if total_base_hogar > 0 else 0

    return {
        'mes': mes,
        'anio': anio,
        'mes_nombre': _nombre_mes(mes),

        'datos_miembros': list(datos_miembros.values()),
        'fondos_aportaciones': list(fondos_aportaciones.values()),
        'transferencias': transferencias,
        'transferencias_cascada': transferencias_cascada,

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
    }


# ---------------------------------------------------------------------------
# Resumen anual
# ---------------------------------------------------------------------------

def calcular_resumen_anual(hogar, anio: int = None):
    anio = anio or datetime.date.today().year

    resumen = {
        'anio': anio,
        'meses': [],
        'total_ingresos': Decimal('0'),
        'total_gastos': Decimal('0'),
        'total_libre': Decimal('0'),
        'total_fondos': Decimal('0'),
    }

    for mes in range(1, 13):
        d = calcular_flujos(hogar, mes=mes, anio=anio)
        total_fondos_mes = sum(
            fa['total_aportacion_neta']
            for fa in d['fondos_aportaciones']
            if fa['total_aportacion_neta'] > 0
        )
        resumen['meses'].append({
            'mes': mes,
            'nombre': _nombre_mes(mes),
            'ingresos': d['ingreso_base_hogar'],
            'gastos': d['total_gastos_all'],
            'libre': d['libre_base_hogar'],
            'fondos': total_fondos_mes,
            'semaforo': d['semaforo'],
        })
        resumen['total_ingresos'] += d['ingreso_base_hogar']
        resumen['total_gastos'] += d['total_gastos_all']
        resumen['total_libre'] += d['libre_base_hogar']
        resumen['total_fondos'] += total_fondos_mes

    return resumen


def _nombre_mes(mes: int) -> str:
    nombres = [
        '', 'Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio',
        'Julio', 'Agosto', 'Septiembre', 'Octubre', 'Noviembre', 'Diciembre'
    ]
    return nombres[mes] if 1 <= mes <= 12 else ''
