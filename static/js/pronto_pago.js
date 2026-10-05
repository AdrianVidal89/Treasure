/* Simulador de pronto pago sobre un préstamo.
 *
 * Es la parte de pantalla del motor (`amortizacion.js`): lee los campos del
 * parcial `finanzas/hipoteca/_pronto_pago.html`, llama a
 * `Amortizacion.simularProntoPago` y pinta la comparación —no hacer nada,
 * reducir cuota, reducir plazo— y el gráfico de capital e intereses por año.
 * Lo usan la ficha de una hipoteca y el simulador de vivienda: una sola
 * implementación.
 *
 *     const pp = ProntoPago.montar(document.getElementById('pp'), {
 *         prestamo: {...}, tipoReferencia: 3, hoy: '2026-10-05'});
 *     pp.actualizar(otroPrestamo);   // si el préstamo cambia (sliders)
 */
(function (global) {
'use strict';

const A = global.Amortizacion;
const NS = 'http://www.w3.org/2000/svg';

function eur(v, dec) {
    return new Intl.NumberFormat('es-ES', {
        style: 'currency', currency: 'EUR',
        minimumFractionDigits: dec || 0, maximumFractionDigits: dec || 0,
    }).format(v || 0);
}
function pct(v) {
    return v === null || v === undefined ? '—' :
        v.toLocaleString('es-ES', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' %';
}
function fechaTxt(iso) {
    if (!iso) return '—';
    const p = iso.split('-');
    return p[1] + '/' + p[0];
}
function css(nombre) {
    return getComputedStyle(document.documentElement).getPropertyValue(nombre).trim() || '#888';
}
function el(tag, attrs) {
    const e = document.createElementNS(NS, tag);
    Object.keys(attrs || {}).forEach(function (k) { e.setAttribute(k, attrs[k]); });
    return e;
}
function menos(v) { return v > 0 ? '−' + eur(v) : eur(0); }
function num(input) {
    if (!input) return 0;
    const v = parseFloat(String(input.value).replace(',', '.'));
    return isFinite(v) ? v : 0;
}

function montar(raiz, opciones) {
    const $ = function (k) { return raiz.querySelector('[data-pp="' + k + '"]'); };
    let prestamo = opciones.prestamo;
    const hoy = opciones.hoy;
    if ($('referencia') && opciones.tipoReferencia !== undefined && !$('referencia').value) {
        $('referencia').value = opciones.tipoReferencia;
    }
    if ($('fecha') && !$('fecha').value) $('fecha').value = hoy;
    if ($('desde') && !$('desde').value) $('desde').value = hoy;

    function calcular() {
        const importe = num($('importe'));
        const periodica = num($('periodica'));
        const referencia = $('referencia') && $('referencia').value !== '' ? num($('referencia')) : null;
        const puntuales = importe > 0 ? [{fecha: $('fecha').value || hoy, importe: importe}] : [];
        const per = periodica > 0 ? {importe: periodica, desde: $('desde').value || hoy, cada_meses: 12} : null;
        if (!prestamo || !(prestamo.capital > 0)) {
            $('resultado').innerHTML = '<p class="pp-vacio">Sin préstamo que simular.</p>';
            return;
        }
        const r = A.simularProntoPago(prestamo, puntuales, per, referencia, hoy);
        pintarTabla(r, puntuales.length || per);
        pintarGrafico(r);
    }

    function pintarTabla(r, hayExtra) {
        const b = r.base, c = r.cuota, p = r.plazo;
        if (!hayExtra) {
            $('resultado').innerHTML =
                '<p class="pp-vacio">Sin amortizar de más pagarías <strong>' + eur(b.intereses) +
                '</strong> de intereses hasta ' + fechaTxt(b.fin) + '. Prueba un importe arriba.</p>';
            return;
        }
        const filas = [
            ['Amortizas', '—', eur(c.amortizado), eur(p.amortizado)],
            ['Intereses que pagarás', eur(b.intereses), eur(c.intereses), eur(p.intereses)],
            ['Intereses ahorrados', '—', eur(c.intereses_ahorrados), eur(p.intereses_ahorrados)],
            ['Comisión', '—', menos(c.comision_pagada), menos(p.comision_pagada)],
            ['Ahorro neto', '—', '<strong>' + eur(c.ahorro_neto) + '</strong>', '<strong>' + eur(p.ahorro_neto) + '</strong>'],
            ['Cuota después', eur(b.primera_cuota, 2), eur(c.cuota_despues, 2), eur(p.cuota_despues, 2)],
            ['Termina', fechaTxt(b.fin), fechaTxt(c.fin), fechaTxt(p.fin) +
                (p.meses_menos > 0 ? ' <span class="pp-sub">(' + p.meses_menos + ' meses antes)</span>' : '')],
            ['Rinde amortizar', '—', pct(c.rentabilidad), pct(p.rentabilidad)],
        ];
        let html = '<div class="pp-tabla"><div class="pp-fila pp-cab"><span></span><span>Sin amortizar</span>' +
                   '<span>Reducir cuota</span><span>Reducir plazo</span></div>';
        filas.forEach(function (f) {
            html += '<div class="pp-fila"><span class="k">' + f[0] + '</span><span>' + f[1] +
                    '</span><span>' + f[2] + '</span><span>' + f[3] + '</span></div>';
        });
        html += '</div>';

        // El veredicto: lo que rinde amortizar frente a lo que rendiría el dinero fuera.
        const mejor = p.ahorro_neto >= c.ahorro_neto ? p : c;
        const modo = mejor === p ? 'reduciendo plazo' : 'reduciendo cuota';
        if (r.tipo_referencia !== null && mejor.rentabilidad !== null) {
            const gana = mejor.rentabilidad >= r.tipo_referencia;
            html += '<p class="pp-veredicto ' + (gana ? 'pos' : 'neg') + '">Amortizar ' + modo +
                    ' te rinde <strong>' + pct(mejor.rentabilidad) + '</strong> (lo que dejas de pagar, ya descontada ' +
                    'la comisión); invertirlo al tipo de referencia daría <strong>' + pct(r.tipo_referencia) + '</strong>. ' +
                    (gana ? 'Compensa amortizar.' : 'Rinde más invertirlo, si de verdad consigues ese ' +
                    pct(r.tipo_referencia) + ' (y asumes su riesgo).') + '</p>';
        }
        html += '<p class="pp-nota">Reducir plazo ahorra más intereses; reducir cuota libera dinero cada mes. ' +
                'La comisión es la de tu préstamo (o la máxima de la Ley 5/2019 si no la has indicado).</p>';
        $('resultado').innerHTML = html;
    }

    function pintarGrafico(r) {
        const svg = $('grafico');
        if (!svg) return;
        const modo = ($('modo-grafico') && $('modo-grafico').value) || 'plazo';
        const antes = r.base.anual, despues = r[modo].anual;
        svg.innerHTML = '';
        if (!antes.length) return;
        const porAnio = {};
        despues.forEach(function (a) { porAnio[a.anio] = a; });
        const W = 720, H = 260, ml = 64, mr = 12, mt = 14, mb = 40;
        const max = Math.max.apply(null, antes.concat(despues).map(function (a) {
            return a.capital + a.intereses + a.extra;
        }).concat([1]));
        const alto = function (v) { return (H - mt - mb) * v / max; };
        const bw = (W - ml - mr) / antes.length;
        const verde = css('--success'), rojo = css('--danger'), azul = css('--info');

        function barra(x, w, a, opacidad) {
            let y = H - mb;
            [[a.capital, verde], [a.extra || 0, azul], [a.intereses, rojo]].forEach(function (t) {
                const h = alto(t[0]);
                if (h <= 0) return;
                y -= h;
                svg.appendChild(el('rect', {x: x, y: y, width: w, height: h, fill: t[1], opacity: opacidad, rx: 1.5}));
            });
        }
        antes.forEach(function (a, i) {
            const x = ml + i * bw;
            const w = Math.max(bw * 0.4, 1);
            barra(x + bw * 0.08, w, a, 0.3);
            if (porAnio[a.anio]) barra(x + bw * 0.08 + w, w, porAnio[a.anio], 1);
            if (i % Math.ceil(antes.length / 10) === 0 || i === antes.length - 1) {
                const t = el('text', {x: x + bw / 2, y: H - mb + 15, 'text-anchor': 'middle', 'font-size': 10, fill: css('--muted')});
                t.textContent = a.anio;
                svg.appendChild(t);
            }
        });
        const ty = el('text', {x: ml - 8, y: mt + 8, 'text-anchor': 'end', 'font-size': 10, fill: css('--muted')});
        ty.textContent = eur(max);
        svg.appendChild(ty);
        [[verde, 'Capital'], [rojo, 'Intereses'], [azul, 'Amortización anticipada']].forEach(function (s, i) {
            const lx = ml + i * 120;
            svg.appendChild(el('rect', {x: lx, y: H - 14, width: 10, height: 10, fill: s[0], rx: 2}));
            const t = el('text', {x: lx + 15, y: H - 5, 'font-size': 10.5, fill: css('--muted')});
            t.textContent = s[1];
            svg.appendChild(t);
        });
        const nota = el('text', {x: W - mr, y: H - 5, 'text-anchor': 'end', 'font-size': 10.5, fill: css('--muted')});
        nota.textContent = 'claro: sin amortizar · intenso: amortizando';
        svg.appendChild(nota);
    }

    raiz.querySelectorAll('input, select').forEach(function (i) {
        i.addEventListener('input', calcular);
        i.addEventListener('change', calcular);
    });
    calcular();
    return {
        actualizar: function (nuevo) { prestamo = nuevo; calcular(); },
        calcular: calcular,
    };
}

global.ProntoPago = {montar: montar};

})(typeof window !== 'undefined' ? window : globalThis);
