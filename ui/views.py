import hashlib
import json

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.decorators import login_required
from datetime import datetime, date
from decimal import Decimal

from finanzas.models import (
    RegistroMensual, Inversion, HistorialValorInversion,
    FuenteIngreso, PartidaGasto, FondoFamiliar, CuentaBancaria,
    TarjetaCredito, AjusteIngresoMensual, SaldoRealFondo,
    IngresoRealMes, ReglaReparto, Propiedad, HistorialPropiedad,
)
from finanzas.distribucion import calcular_flujos, clasificar_salud

from django.db.models import Sum, F, FloatField, ExpressionWrapper
from django.db.models.functions import TruncMonth
from collections import defaultdict


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_hogar(request):
    profile = getattr(request.user, 'userprofile', None)
    if not profile or not profile.hogar:
        return None, None
    return profile, profile.hogar


def _saludo():
    h = datetime.now().hour
    if h < 6:
        return 'Buenas noches'
    elif h < 13:
        return 'Buenos días'
    elif h < 20:
        return 'Buenas tardes'
    return 'Buenas noches'


NOMBRE_MES = [
    '', 'Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio',
    'Julio', 'Agosto', 'Septiembre', 'Octubre', 'Noviembre', 'Diciembre',
]


def _alerta_key(texto):
    return hashlib.md5(texto.encode()).hexdigest()[:12]


def _get_alertas(hogar, mes, anio, propiedades=None):
    alertas = []

    fuentes_variable = FuenteIngreso.objects.filter(hogar=hogar, activo=True, tipo='variable')
    for fv in fuentes_variable:
        tiene_ajuste = AjusteIngresoMensual.objects.filter(
            fuente=fv, mes=mes, **{'año': anio}
        ).exists()
        if not tiene_ajuste:
            nombre_user = fv.usuario.first_name or fv.usuario.username
            texto = f'{nombre_user}: "{fv.nombre}" sin ajuste en {NOMBRE_MES[mes]}'
            alertas.append({
                'tipo': 'warning', 'icono': '✏️', 'texto': texto,
                'link': f'/finanzas/distribucion/?mes={mes}&anio={anio}',
                'link_text': 'Ajustar',
                'key': _alerta_key(texto),
            })

    fondos_sin_saldo = []
    for fondo in FondoFamiliar.objects.filter(hogar=hogar, activo=True):
        if not SaldoRealFondo.objects.filter(fondo=fondo, año=anio, mes=mes).exists():
            fondos_sin_saldo.append(fondo.nombre)

    if fondos_sin_saldo:
        n = len(fondos_sin_saldo)
        nombres = ', '.join(fondos_sin_saldo[:3]) + (f' y {n - 3} más' if n > 3 else '')
        texto = f'{n} fondo{"s" if n > 1 else ""} sin saldo en {NOMBRE_MES[mes]}: {nombres}'
        alertas.append({
            'tipo': 'info', 'icono': '📊', 'texto': texto,
            'link': f'/finanzas/evolucion/?año={anio}',
            'link_text': 'Registrar',
            'key': _alerta_key(texto),
        })

    if propiedades is None:
        propiedades = Propiedad.objects.filter(hogar=hogar, activo=True)
    hoy_prop = date.today()
    for prop in propiedades:
        if not HistorialPropiedad.objects.filter(propiedad=prop, año=hoy_prop.year, mes=hoy_prop.month).exists():
            texto = f'"{prop.nombre}": actualiza valor e hipoteca de {NOMBRE_MES[hoy_prop.month]}'
            alertas.append({
                'tipo': 'info', 'icono': '🏡', 'texto': texto,
                'link': f'/finanzas/evolucion/?año={hoy_prop.year}',
                'link_text': 'Evolución',
                'key': _alerta_key(texto),
            })

    if not IngresoRealMes.objects.filter(hogar=hogar, año=anio, mes=mes).exists():
        texto = f'Ingresos reales de {NOMBRE_MES[mes]} no registrados en Evolución'
        alertas.append({
            'tipo': 'info', 'icono': '💰', 'texto': texto,
            'link': f'/finanzas/evolucion/?año={anio}',
            'link_text': 'Registrar',
            'key': _alerta_key(texto),
        })

    return alertas


def _get_alertas_filtradas(request, hogar, mes, anio):
    alertas = _get_alertas(hogar, mes, anio)
    dismissed = set(request.session.get('dismissed_alerts', []))
    return [a for a in alertas if a['key'] not in dismissed]


# ─── Auth views ───────────────────────────────────────────────────────────────

def index(request):
    return redirect('dashboard')


def login_view(request):
    if request.method == 'POST':
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            login(request, form.get_user())
            return redirect('dashboard')
    else:
        form = AuthenticationForm()
    return render(request, 'ui/login.html', {'form': form, 'hide_navbar': True})


def logout_view(request):
    logout(request)
    return redirect('login')


# ─── Dashboard Command Center ────────────────────────────────────────────────

@login_required
def dashboard_view(request):
    profile, hogar = _get_hogar(request)

    # Sin hogar → pantalla de espera
    if not hogar:
        return render(request, 'core/sin_hogar.html')

    hoy = date.today()
    mes = hoy.month
    anio = hoy.year

    # ── 1. Motor de distribución (reutilizado, no duplicado) ──
    flujo = calcular_flujos(hogar, mes=mes, anio=anio)

    # ── 1b. Salud financiera sobre la base general ──
    # Tasa de ahorro = % del ingreso neto base que queda tras los gastos
    # recurrentes. Se calcula sobre el ingreso BASE (sin pagas extras ni
    # ajustes del mes) para que un mes con paga extra —o unas reglas de
    # ahorro de importe fijo— no disparen la tasa por encima del 100 %.
    ingreso_base = flujo['ingreso_base_puro_hogar']
    gastos_recurrentes = flujo['total_gastos_all']
    if ingreso_base > 0:
        salud_tasa = round((ingreso_base - gastos_recurrentes) / ingreso_base * 100, 1)
    else:
        salud_tasa = Decimal('0')
    salud_semaforo, salud_texto = clasificar_salud(salud_tasa)

    # ── 2. Contadores por módulo ──
    num_fuentes = FuenteIngreso.objects.filter(hogar=hogar, activo=True).count()
    num_partidas = PartidaGasto.objects.filter(hogar=hogar, activo=True).count()
    num_fondos = FondoFamiliar.objects.filter(hogar=hogar, activo=True).count()
    num_reglas = ReglaReparto.objects.filter(hogar=hogar, activo=True).count()
    num_cuentas = CuentaBancaria.objects.filter(
        usuario__userprofile__hogar=hogar, activa=True
    ).count()
    num_tarjetas = TarjetaCredito.objects.filter(
        usuario__userprofile__hogar=hogar, activa=True
    ).count()

    # ── 3. Inversiones (portfolio del hogar) ──
    miembros_ids = list(
        hogar.miembros.values_list('user_id', flat=True)
    )
    inversiones = Inversion.objects.filter(usuario_id__in=miembros_ids)
    total_inversiones = sum(inv.valor_total_actual for inv in inversiones)
    num_inversiones = inversiones.count()

    # ── 4. Evolución: liquidez actual ──
    ultimo_mes_con_saldo = None
    for m in range(mes, 0, -1):
        if SaldoRealFondo.objects.filter(fondo__hogar=hogar, año=anio, mes=m).exists():
            ultimo_mes_con_saldo = m
            break

    liquidez_real = Decimal('0')
    patrimonio_real = Decimal('0')
    if ultimo_mes_con_saldo:
        saldos = SaldoRealFondo.objects.filter(
            fondo__hogar=hogar, año=anio, mes=ultimo_mes_con_saldo
        ).select_related('fondo')
        for s in saldos:
            if s.fondo.tipo_fondo in ('comun', 'ahorro'):
                liquidez_real += s.saldo
            patrimonio_real += s.saldo

    # ── 4b. Propiedades ──
    propiedades = Propiedad.objects.filter(hogar=hogar, activo=True)
    num_propiedades = propiedades.count()
    patrimonio_inmuebles = sum(p.patrimonio_neto for p in propiedades)

    # ── 5. Alertas — now shown via bell icon, not inline ──
    # (still passed for context but not rendered in the page body)

    # ── 6. Datos para el donut chart (JS) ──
    donut_data = []
    if flujo['ingreso_base_hogar'] > 0:
        base = float(flujo['ingreso_base_hogar'])
        segments = [
            ('Gastos', float(flujo['total_gastos_all']), '#ff4d4d'),
            ('Ahorro', float(flujo['total_ahorro']), '#00d1ff'),
            ('Inversión', float(flujo['total_inversion']), '#a259ff'),
            ('Libre', float(flujo['libre_total']), '#ffaa00'),
        ]
        for label, val, color in segments:
            if val > 0:
                donut_data.append({
                    'label': label,
                    'value': round(val, 2),
                    'pct': round(val / base * 100, 1),
                    'color': color,
                })

    # ── 7. Module cards data ──
    modulos = [
        {
            'icono': '💰', 'titulo': 'Ingresos',
            'metrica': f'€{flujo["ingreso_base_puro_hogar"]:,.2f}/mes',
            'sub': f'{num_fuentes} fuente{"s" if num_fuentes != 1 else ""} activa{"s" if num_fuentes != 1 else ""}',
            'color': '#00ff88',
            'link': '/finanzas/ingresos/',
        },
        {
            'icono': '📋', 'titulo': 'Presupuesto',
            'metrica': f'€{flujo["total_gastos_all"]:,.2f}/mes',
            'sub': f'{num_partidas} partida{"s" if num_partidas != 1 else ""}',
            'color': '#ff4d4d',
            'link': '/finanzas/gastos/',
        },
        {
            'icono': '🔀', 'titulo': 'Distribución',
            'metrica': f'{flujo["tasa_ahorro"]}% ahorro',
            'sub': f'{num_fondos} fondo{"s" if num_fondos != 1 else ""} · {num_reglas} regla{"s" if num_reglas != 1 else ""}',
            'color': '#a259ff',
            'link': '/finanzas/distribucion/',
        },
        {
            'icono': '📈', 'titulo': 'Evolución',
            'metrica': f'€{liquidez_real:,.0f}' if liquidez_real > 0 else '—',
            'sub': 'Liquidez actual' if liquidez_real > 0 else 'Sin datos aún',
            'color': '#00d1ff',
            'link': '/finanzas/evolucion/',
        },
        {
            'icono': '📊', 'titulo': 'Inversiones',
            'metrica': f'€{total_inversiones:,.2f}' if total_inversiones > 0 else '—',
            'sub': f'{num_inversiones} activo{"s" if num_inversiones != 1 else ""}',
            'color': '#b266ff',
            'link': '/finanzas/inversiones/',
        },
        {
            'icono': '🏦', 'titulo': 'Cuentas',
            'metrica': f'{num_cuentas} cuenta{"s" if num_cuentas != 1 else ""}',
            'sub': f'{num_tarjetas} tarjeta{"s" if num_tarjetas != 1 else ""}',
            'color': '#ffaa00',
            'link': '/finanzas/gestionar/',
        },
        {
            'icono': '🏡', 'titulo': 'Propiedades',
            'metrica': f'€{patrimonio_inmuebles:,.0f}' if num_propiedades > 0 else '—',
            'sub': f'{num_propiedades} inmueble{"s" if num_propiedades != 1 else ""} · patrimonio neto' if num_propiedades > 0 else 'Sin propiedades registradas',
            'color': '#e67e22',
            'link': '/finanzas/propiedades/',
        },
    ]

    # Capital líquido total del hogar = liquidez disponible en fondos
    # (común + ahorro) registrada en el último mes con datos.
    capital_liquido_total = liquidez_real

    context = {
        'hogar': hogar,
        'profile': profile,
        'saludo': _saludo(),
        'mes': mes,
        'anio': anio,
        'mes_nombre': NOMBRE_MES[mes],
        'flujo': flujo,
        'salud_tasa': salud_tasa,
        'salud_semaforo': salud_semaforo,
        'salud_texto': salud_texto,
        'modulos': modulos,
        'donut_data': donut_data,
        'liquidez_real': liquidez_real,
        'patrimonio_real': patrimonio_real,
        'total_inversiones': total_inversiones,
        'capital_liquido_total': capital_liquido_total,
        'patrimonio_inmuebles': patrimonio_inmuebles,
        'num_propiedades': num_propiedades,
        'mes_liquidez_nombre': NOMBRE_MES[ultimo_mes_con_saldo] if ultimo_mes_con_saldo else None,
    }
    return render(request, 'ui/dashboard.html', context)


# ─── Notification API ────────────────────────────────────────────────────────

@login_required
def api_notificaciones(request):
    profile, hogar = _get_hogar(request)
    if not hogar:
        return JsonResponse({'alertas': []})
    hoy = date.today()
    alertas = _get_alertas_filtradas(request, hogar, hoy.month, hoy.year)
    return JsonResponse({'alertas': alertas})


@login_required
def api_descartar_notificacion(request):
    profile, hogar = _get_hogar(request)
    if not hogar:
        return JsonResponse({'alertas': []})
    try:
        data = json.loads(request.body)
        key = data.get('key', '')
    except (json.JSONDecodeError, Exception):
        return JsonResponse({'error': 'invalid'}, status=400)

    dismissed = set(request.session.get('dismissed_alerts', []))
    dismissed.add(key)
    request.session['dismissed_alerts'] = list(dismissed)
    hoy = date.today()
    alertas = _get_alertas_filtradas(request, hogar, hoy.month, hoy.year)
    return JsonResponse({'alertas': alertas})


@login_required
def api_descartar_todas(request):
    profile, hogar = _get_hogar(request)
    if not hogar:
        return JsonResponse({'alertas': []})
    hoy = date.today()
    todas = _get_alertas(hogar, hoy.month, hoy.year)
    dismissed = set(request.session.get('dismissed_alerts', []))
    for a in todas:
        dismissed.add(a['key'])
    request.session['dismissed_alerts'] = list(dismissed)
    return JsonResponse({'alertas': []})


# ─── Legacy API (mantener por si acaso) ───────────────────────────────────────

@login_required
def datos_evolucion_financiera(request):
    categorias = request.GET.getlist('categorias[]')
    periodo = request.GET.get('periodo', '12')

    registros = RegistroMensual.objects.filter(usuario=request.user).order_by('anio', 'mes')

    if periodo != 'todos':
        try:
            periodo_int = int(periodo)
            registros = list(registros)[-periodo_int:]
        except ValueError:
            pass

    labels = [f"{r.mes:02d}/{r.anio}" for r in registros] if registros else []
    data = {}

    if registros:
        if 'total' in categorias:
            data['total'] = [float(r.patrimonio_total) for r in registros]
        if 'liquido' in categorias:
            data['liquido'] = [float(r.total_liquido) for r in registros]

    if 'inversiones' in categorias:
        historial_dict = {}
        for label in labels:
            mes_l, anio_l = map(int, label.split('/'))
            valor_mes = HistorialValorInversion.objects.filter(
                inversion__usuario=request.user,
                fecha__month=mes_l, fecha__year=anio_l
            ).annotate(
                valor_total=ExpressionWrapper(
                    F('valor_unitario') * F('cantidad_activos'),
                    output_field=FloatField()
                )
            ).aggregate(total_mes=Sum('valor_total'))['total_mes'] or 0.0
            historial_dict[label] = float(valor_mes)
        data['inversiones'] = [historial_dict.get(l, 0.0) for l in labels]

    return JsonResponse({'labels': labels, 'series': data, 'resumen_actual': {}})
