"""Las hipotecas de una propiedad: alta, ficha con su cuadro, revisiones del
tipo, amortizaciones hechas y el simulador de pronto pago.

El cálculo es de `amortizacion.py`; lo que une el cuadro con la propiedad
(deuda, coste, conciliación), de `hipoteca_propiedad.py`.
"""
import datetime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import amortizacion, hipoteca_propiedad
from .forms import AmortizacionAnticipadaForm, HipotecaForm, RevisionTipoForm
from .models import AmortizacionAnticipada, Hipoteca, Propiedad, RevisionTipo

TIPO_REFERENCIA_DEFECTO = Decimal('3')
MESES = ['', 'ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sep', 'oct', 'nov', 'dic']


def _hogar(request):
    profile = getattr(request.user, 'userprofile', None)
    return profile.hogar if profile and profile.hogar else None


def _hipoteca(request, pk):
    hogar = _hogar(request)
    return get_object_or_404(Hipoteca.objects.select_related('propiedad'), pk=pk, propiedad__hogar=hogar)


def _volver(hipoteca):
    return redirect('finanzas:detalle_hipoteca', pk=hipoteca.pk)


def _sincronizar(propiedad):
    hipoteca_propiedad.sincronizar(propiedad)


def tipo_referencia(propiedad):
    return propiedad.tipo_referencia_pct if propiedad.tipo_referencia_pct is not None else TIPO_REFERENCIA_DEFECTO


def _texto_comision(hipoteca):
    """La regla de comisión que aplica, en palabras."""
    p = hipoteca.como_prestamo()
    reglas = amortizacion.reglas_comision(p)
    if reglas is None:
        return (f'{amortizacion.COMISION_FIJO[0]:g} % durante el tramo fijo '
                f'({amortizacion.COMISION_FIJO[2]:g} % pasados 10 años); después, como un variable: '
                f'{amortizacion.COMISION_VARIABLE[0]:g} % los 3 primeros años, luego nada.')
    inicial, meses, despues = reglas
    texto = f'{inicial:g} % los primeros {meses} meses'
    texto += f', {despues:g} % después.' if despues else ', nada después.'
    return texto


# ── Alta y edición ───────────────────────────────────────────────────────────

@login_required
def nueva_hipoteca(request, pk):
    hogar = _hogar(request)
    if not hogar:
        return redirect('dashboard')
    propiedad = get_object_or_404(Propiedad, pk=pk, hogar=hogar)
    if request.method == 'POST':
        form = HipotecaForm(request.POST, hogar=hogar, instance=Hipoteca(propiedad=propiedad))
        if form.is_valid():
            hipoteca = form.save()
            _sincronizar(propiedad)
            messages.success(request, f'Hipoteca «{hipoteca.nombre}» añadida a {propiedad.nombre}.')
            return _volver(hipoteca)
    else:
        form = HipotecaForm(hogar=hogar, initial={
            'fecha_firma': propiedad.fecha_compra,
            'capital_inicial': propiedad.hipoteca_inicial or None,
            'nombre': 'Hipoteca',
        })
    return render(request, 'finanzas/hipoteca/form.html', {
        'form': form, 'propiedad': propiedad, 'accion': 'Declarar hipoteca',
    })


@login_required
def editar_hipoteca(request, pk):
    hipoteca = _hipoteca(request, pk)
    if request.method == 'POST':
        form = HipotecaForm(request.POST, hogar=hipoteca.propiedad.hogar, instance=hipoteca)
        if form.is_valid():
            form.save()
            _sincronizar(hipoteca.propiedad)
            messages.success(request, 'Hipoteca actualizada.')
            return _volver(hipoteca)
    else:
        form = HipotecaForm(hogar=hipoteca.propiedad.hogar, instance=hipoteca)
    return render(request, 'finanzas/hipoteca/form.html', {
        'form': form, 'propiedad': hipoteca.propiedad, 'hipoteca': hipoteca, 'accion': 'Editar hipoteca',
    })


@login_required
@require_POST
def eliminar_hipoteca(request, pk):
    hipoteca = _hipoteca(request, pk)
    propiedad = hipoteca.propiedad
    hipoteca.delete()
    hipoteca_propiedad.olvidar(propiedad)
    _sincronizar(propiedad)
    messages.success(request, 'Hipoteca eliminada. Si la propiedad ya no tiene ninguna, su deuda vuelve a ser la puesta a mano.')
    return redirect(reverse('finanzas:listar_propiedades') + f'#propiedad-{propiedad.pk}')


# ── Revisiones y amortizaciones ──────────────────────────────────────────────

@login_required
@require_POST
def nueva_revision(request, pk):
    hipoteca = _hipoteca(request, pk)
    form = RevisionTipoForm(request.POST)
    if form.is_valid():
        RevisionTipo.objects.update_or_create(
            hipoteca=hipoteca, fecha_desde=form.cleaned_data['fecha_desde'],
            defaults={'tipo_pct': form.cleaned_data['tipo_pct'],
                      'valor_indice_pct': form.cleaned_data['valor_indice_pct']},
        )
        _sincronizar(hipoteca.propiedad)
        messages.success(request, f"Tipo del {form.cleaned_data['tipo_pct']} % desde el "
                                  f"{form.cleaned_data['fecha_desde']:%d/%m/%Y}.")
    else:
        messages.error(request, 'Revisión no válida: ' + '; '.join(
            f'{form.fields[k].label}: {" ".join(v)}' for k, v in form.errors.items()))
    return _volver(hipoteca)


@login_required
@require_POST
def eliminar_revision(request, pk):
    revision = get_object_or_404(RevisionTipo, pk=pk, hipoteca__propiedad__hogar=_hogar(request))
    hipoteca = revision.hipoteca
    revision.delete()
    _sincronizar(hipoteca.propiedad)
    return _volver(hipoteca)


@login_required
@require_POST
def nueva_amortizacion(request, pk):
    hipoteca = _hipoteca(request, pk)
    form = AmortizacionAnticipadaForm(request.POST)
    if form.is_valid():
        a = form.save(commit=False)
        a.hipoteca = hipoteca
        a.save()
        _sincronizar(hipoteca.propiedad)
        messages.success(request, f'Amortización de {a.importe} € del {a.fecha:%d/%m/%Y} añadida al cuadro.')
    else:
        messages.error(request, 'Amortización no válida: ' + '; '.join(
            f'{form.fields[k].label}: {" ".join(v)}' for k, v in form.errors.items()))
    return _volver(hipoteca)


@login_required
@require_POST
def eliminar_amortizacion(request, pk):
    a = get_object_or_404(AmortizacionAnticipada, pk=pk, hipoteca__propiedad__hogar=_hogar(request))
    hipoteca = a.hipoteca
    a.delete()
    _sincronizar(hipoteca.propiedad)
    return _volver(hipoteca)


# ── La ficha ─────────────────────────────────────────────────────────────────

def _indice(request):
    texto = (request.GET.get('indice') or '').strip().replace(',', '.')
    if not texto:
        return None
    try:
        return Decimal(texto)
    except InvalidOperation:
        return None


@login_required
def detalle_hipoteca(request, pk):
    hipoteca = _hipoteca(request, pk)
    propiedad = hipoteca.propiedad
    hoy = datetime.date.today()
    variable = hipoteca.modalidad != 'fijo'
    indice = _indice(request) if variable else None

    prestamo = hipoteca.como_prestamo(indice)
    filas = amortizacion.cuadro(prestamo)
    pendientes = [f for f in filas if f['fecha'] > hoy]
    pasadas = [f for f in filas if f['fecha'] <= hoy]
    totales = amortizacion.totales(filas)
    proxima = pendientes[0] if pendientes else None

    anual = amortizacion.resumen_anual(filas)
    por_anio = {}
    for f in filas:
        por_anio.setdefault(f['fecha'].year, []).append(dict(f, mes=MESES[f['fecha'].month]))
    for a in anual:
        a['meses'] = por_anio.get(a['anio'], [])
        a['actual'] = a['anio'] == hoy.year

    meses_desde_firma = max(0, amortizacion.meses_entre(hipoteca.fecha_firma, hoy))
    conciliacion = hipoteca_propiedad.conciliacion(propiedad, hoy)
    for aviso in (conciliacion or {}).get('avisos', []):
        aviso['nombre'] = f"{MESES[aviso['mes']]} {aviso['anio']}"

    return render(request, 'finanzas/hipoteca/detalle.html', {
        'h': hipoteca,
        'propiedad': propiedad,
        'variable': variable,
        'indice': indice,
        'hoy': hoy,
        'saldo_hoy': amortizacion.saldo_a(prestamo, filas, hoy),
        'tipo_hoy': (pasadas[-1] if pasadas else filas[0])['tipo'] if filas else float(hipoteca.tipo_inicial_pct),
        'proxima': proxima,
        'totales': totales,
        'intereses_pendientes': round(sum(f['intereses'] for f in pendientes), 2),
        'intereses_pagados': round(sum(f['intereses'] for f in pasadas), 2),
        'cuotas_pendientes': len(pendientes),
        'este_anio': amortizacion.del_anio(filas, hoy.year),
        'proximos_12': amortizacion.siguientes(filas, hoy),
        'anual': anual,
        'conciliacion': conciliacion,
        'varias': len(hipoteca_propiedad.activas(propiedad)) > 1,
        'comision_hoy': amortizacion.pct_comision(prestamo, meses_desde_firma),
        'texto_comision': _texto_comision(hipoteca),
        'tipo_referencia': tipo_referencia(propiedad),
        'prestamo_json': prestamo,
        'form_revision': RevisionTipoForm(initial={'fecha_desde': hoy}),
        'form_amortizacion': AmortizacionAnticipadaForm(initial={'fecha': hoy, 'modo': 'plazo'}),
        'revisiones': hipoteca.revisiones.all(),
        'amortizaciones': hipoteca.amortizaciones.all(),
        'bonificaciones': hipoteca.bonificaciones or [],
    })
