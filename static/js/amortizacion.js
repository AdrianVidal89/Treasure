/* Motor de amortización de préstamos (sistema francés), mes a mes.
 *
 * Es el espejo en el navegador de `finanzas/amortizacion.py`: la misma
 * lógica, línea a línea, para lo que se recalcula a cada movimiento de un
 * slider. Los dos pasan los mismos casos (`finanzas/amortizacion_casos.json`)
 * en los tests, así que si se toca uno hay que tocar el otro.
 *
 * Las fechas entran y salen como texto ISO ('2026-03-01'). El préstamo es el
 * mismo diccionario que en Python; ver allí qué significa cada campo.
 */
(function (global) {
'use strict';

const MAX_MESES = 1200;
const COMISION_FIJO = [2.0, 120, 1.5];
const COMISION_VARIABLE = [0.25, 36, 0.0];
const COMISION_VARIABLE_5 = [0.15, 60, 0.0];

// ── Utilidades ──────────────────────────────────────────────────────────────

function r2(x) {
    const signo = x < 0 ? -1 : 1;
    return signo * Math.floor(Math.abs(x) * 100 + 0.5 + 1e-9) / 100;
}

function vacio(v) { return v === null || v === undefined || v === ''; }

/* Fechas como {y, m, d}; `clave` las ordena. */
function fecha(v) {
    if (vacio(v)) return null;
    if (typeof v === 'object') return v;
    const p = String(v).slice(0, 10).split('-');
    return {y: +p[0], m: +p[1], d: +p[2]};
}
function clave(f) { return f.y * 10000 + f.m * 100 + f.d; }
function iso(f) {
    return f.y + '-' + String(f.m).padStart(2, '0') + '-' + String(f.d).padStart(2, '0');
}
function diasMes(y, m) { return new Date(Date.UTC(y, m, 0)).getUTCDate(); }

function sumarMeses(f, meses, diaFijo) {
    const total = f.y * 12 + f.m - 1 + meses;
    const y = Math.floor(total / 12), m = total % 12 + 1;
    return {y: y, m: m, d: Math.min(diaFijo || f.d, diasMes(y, m))};
}

function mesesEntre(desde, hasta) {
    let n = (hasta.y - desde.y) * 12 + hasta.m - desde.m;
    if (hasta.d < desde.d) n -= 1;
    return n;
}

function cuota(capital, tipoAnual, meses) {
    if (capital <= 0 || meses <= 0) return 0;
    if (!tipoAnual) return capital / meses;
    const r = tipoAnual / 1200;
    return capital * r / (1 - Math.pow(1 + r, -meses));
}

function capitalParaCuota(cuotaMensual, tipoAnual, meses) {
    if (cuotaMensual <= 0 || meses <= 0) return 0;
    if (!tipoAnual) return cuotaMensual * meses;
    const r = tipoAnual / 1200;
    return cuotaMensual * (1 - Math.pow(1 + r, -meses)) / r;
}

function cuotasRestantes(capital, tipoAnual, cuotaMensual) {
    if (capital <= 0) return 0;
    if (cuotaMensual <= 0) return null;
    const r = (tipoAnual || 0) / 1200;
    if (r === 0) return Math.ceil(capital / cuotaMensual - 1e-9);
    if (cuotaMensual <= capital * r) return null;
    const n = -Math.log(1 - capital * r / cuotaMensual) / Math.log(1 + r);
    return Math.max(1, Math.ceil(n - 1e-6));
}

// ── Tipo y comisión ─────────────────────────────────────────────────────────

function revisiones(p) {
    const revs = (p.revisiones || []).map(function (r) {
        return [fecha(r.fecha), +r.tipo];
    });
    const ancla = p.ancla || {};
    if (!vacio(ancla.tipo)) revs.push([fecha(ancla.fecha), +ancla.tipo]);
    revs.sort(function (a, b) { return clave(a[0]) - clave(b[0]) || a[1] - b[1]; });
    return revs;
}

function tipoEn(p, inicioPeriodo, mesesDesdeFirma, revs) {
    revs = revs || revisiones(p);
    const modalidad = p.modalidad || 'fijo';
    const tipoInicial = +(p.tipo_inicial || 0);
    const tramoFijo = +(p.meses_tramo_fijo || 0);
    const indice = p.indice_futuro, diferencial = p.diferencial;
    const proyectable = modalidad !== 'fijo' && !vacio(indice) && !vacio(diferencial) &&
                        mesesDesdeFirma >= tramoFijo;

    let vigente = null;
    for (const r of revs) if (clave(r[0]) <= clave(inicioPeriodo)) vigente = r;
    if (vigente) {
        const caduca = sumarMeses(vigente[0], +(p.revision_meses || 12));
        if (!proyectable || clave(inicioPeriodo) < clave(caduca)) return vigente[1];
    }
    if (modalidad === 'fijo' || mesesDesdeFirma < tramoFijo) return tipoInicial;
    if (proyectable) return +indice + +diferencial;
    return vigente ? vigente[1] : tipoInicial;
}

function reglasComision(p) {
    const c = p.comision || {};
    const modalidad = p.modalidad || 'fijo';
    if (['pct_inicial', 'meses_iniciales', 'pct_despues'].some(function (k) { return !vacio(c[k]); })) {
        const def = modalidad === 'fijo' ? COMISION_FIJO : COMISION_VARIABLE;
        return [
            vacio(c.pct_inicial) ? def[0] : +c.pct_inicial,
            vacio(c.meses_iniciales) ? def[1] : +c.meses_iniciales,
            vacio(c.pct_despues) ? def[2] : +c.pct_despues,
        ];
    }
    if (modalidad === 'fijo') return COMISION_FIJO;
    if (modalidad === 'variable') return COMISION_VARIABLE;
    return null;
}

function pctComision(p, mesesDesdeFirma) {
    let reglas = reglasComision(p);
    if (reglas === null) {
        reglas = mesesDesdeFirma < +(p.meses_tramo_fijo || 0) ? COMISION_FIJO : COMISION_VARIABLE;
    }
    return mesesDesdeFirma < reglas[1] ? reglas[0] : reglas[2];
}

function comision(p, dia, importe) {
    const inicio = fecha(p.inicio);
    return r2(importe * pctComision(p, Math.max(0, mesesEntre(inicio, fecha(dia)))) / 100);
}

// ── El cuadro ───────────────────────────────────────────────────────────────

function fechaPago(p, k) {
    const inicio = fecha(p.inicio);
    return sumarMeses(inicio, k, p.dia_cobro || inicio.d);
}

function cuadro(p) {
    const plazo = +p.plazo_meses;
    const revs = revisiones(p);
    const ancla = p.ancla || {};
    const anclaFecha = vacio(ancla.fecha) ? null : fecha(ancla.fecha);

    let k = 1;
    let pendiente = +p.capital;
    let restantes = plazo;
    let cuotaFija = null;
    if (anclaFecha) {
        let hechas = 0;
        while (clave(fechaPago(p, hechas + 1)) <= clave(anclaFecha)) hechas++;
        k = hechas + 1;
        pendiente = +ancla.saldo;
        restantes = Math.max(plazo - hechas, 1);
        if (!vacio(ancla.cuota)) cuotaFija = r2(+ancla.cuota);
    }

    let extras = (p.amortizaciones || [])
        .filter(function (a) { return +(a.importe || 0) > 0; })
        .map(function (a) {
            return {fecha: fecha(a.fecha), importe: +a.importe, modo: a.modo || 'plazo', comision: a.comision};
        });
    // Orden estable por fecha, como `sorted` en Python.
    extras = extras.map(function (a, i) { return [a, i]; })
        .sort(function (a, b) { return clave(a[0].fecha) - clave(b[0].fecha) || a[1] - b[1]; })
        .map(function (x) { return x[0]; });
    if (anclaFecha) extras = extras.filter(function (a) { return clave(a.fecha) > clave(anclaFecha); });

    const filas = [];
    let tipoPrevio = null;
    let cuotaActual = null;
    if (cuotaFija !== null) {
        tipoPrevio = tipoEn(p, fechaPago(p, k - 1), k - 1, revs);
        cuotaActual = cuotaFija;
        const n = cuotasRestantes(pendiente, tipoPrevio, cuotaActual);
        if (n === null) cuotaActual = null;
        else restantes = n;
    }

    function aplicarExtras(hasta, tipo) {
        let extra = 0, com = 0;
        while (extras.length && clave(extras[0].fecha) < clave(hasta) && pendiente > 0.005) {
            const a = extras.shift();
            const importe = Math.min(a.importe, pendiente);
            pendiente = r2(pendiente - importe);
            extra += importe;
            com += vacio(a.comision) ? comision(p, a.fecha, importe) : r2(+a.comision);
            if (pendiente <= 0.005) break;
            if (a.modo === 'cuota') {
                cuotaActual = r2(cuota(pendiente, tipo, restantes));
                tipoPrevio = tipo;
            } else {
                const n = cuotasRestantes(pendiente, tipo, cuotaActual || 0);
                if (n !== null) restantes = n;
            }
        }
        return [extra, com];
    }

    const tipo0 = tipoEn(p, fechaPago(p, k - 1), k - 1, revs);
    if (cuotaActual === null) {
        cuotaActual = r2(cuota(pendiente, tipo0, restantes));
        tipoPrevio = tipo0;
    }
    const inicial = aplicarExtras(fechaPago(p, k), tipo0);

    while (pendiente > 0.005 && filas.length < MAX_MESES) {
        const tipo = tipoEn(p, fechaPago(p, k - 1), k - 1, revs);
        if (tipo !== tipoPrevio) {
            cuotaActual = r2(cuota(pendiente, tipo, restantes));
            tipoPrevio = tipo;
        }
        const intereses = r2(pendiente * tipo / 1200);
        if (cuotaActual <= intereses && restantes > 1) {
            cuotaActual = r2(cuota(pendiente, tipo, restantes));
        }
        let amortiza = r2(cuotaActual - intereses);
        if (amortiza >= pendiente || restantes <= 1) amortiza = pendiente;
        pendiente = r2(pendiente - amortiza);
        restantes = Math.max(restantes - 1, 1);
        let ec = aplicarExtras(fechaPago(p, k + 1), tipo);
        if (!filas.length) ec = [ec[0] + inicial[0], ec[1] + inicial[1]];
        filas.push({
            n: k,
            fecha: iso(fechaPago(p, k)),
            tipo: tipo,
            cuota: r2(intereses + amortiza),
            intereses: intereses,
            capital: r2(amortiza),
            extra: r2(ec[0]),
            comision: r2(ec[1]),
            pendiente: pendiente,
        });
        k++;
    }
    return filas;
}

// ── Lecturas del cuadro ─────────────────────────────────────────────────────

function saldoA(p, filas, dia) {
    dia = fecha(dia);
    let ultimo = null;
    for (const f of filas) if (clave(fecha(f.fecha)) <= clave(dia)) ultimo = f;
    if (ultimo) return ultimo.pendiente;
    const ancla = p.ancla || {};
    if (!vacio(ancla.fecha) && clave(fecha(ancla.fecha)) <= clave(dia)) return +ancla.saldo;
    return clave(dia) >= clave(fecha(p.inicio)) ? +p.capital : 0;
}

function resumenAnual(filas) {
    const anios = {};
    const orden = [];
    for (const f of filas) {
        const y = fecha(f.fecha).y;
        if (!anios[y]) {
            anios[y] = {anio: y, cuotas: 0, pagado: 0, intereses: 0, capital: 0,
                        extra: 0, comision: 0, pendiente: 0};
            orden.push(y);
        }
        const a = anios[y];
        a.cuotas += 1;
        a.pagado += f.cuota;
        a.intereses += f.intereses;
        a.capital += f.capital;
        a.extra += f.extra;
        a.comision += f.comision;
        a.pendiente = f.pendiente;
    }
    return orden.sort(function (a, b) { return a - b; }).map(function (y) {
        const a = anios[y];
        ['pagado', 'intereses', 'capital', 'extra', 'comision'].forEach(function (c) { a[c] = r2(a[c]); });
        return a;
    });
}

function delAnio(filas, anio) {
    const a = resumenAnual(filas).find(function (x) { return x.anio === anio; });
    if (a) return a;
    return {anio: anio, cuotas: 0, pagado: 0, intereses: 0, capital: 0, extra: 0, comision: 0,
            pendiente: filas.length ? filas[filas.length - 1].pendiente : 0};
}

function totales(filas) {
    const suma = function (c) { return r2(filas.reduce(function (s, f) { return s + f[c]; }, 0)); };
    return {
        cuotas: filas.length,
        pagado: suma('cuota'),
        intereses: suma('intereses'),
        capital: suma('capital'),
        extra: suma('extra'),
        comision: suma('comision'),
        fin: filas.length ? filas[filas.length - 1].fecha : null,
        primera_cuota: filas.length ? filas[0].cuota : 0,
    };
}

function siguientes(filas, desde, meses) {
    const d = clave(fecha(desde));
    return totales(filas.filter(function (f) { return clave(fecha(f.fecha)) > d; }).slice(0, meses || 12));
}

// ── Pronto pago ─────────────────────────────────────────────────────────────

function extrasPeriodicos(importe, desde, hasta, cadaMeses) {
    desde = fecha(desde);
    hasta = vacio(hasta) ? null : fecha(hasta);
    cadaMeses = cadaMeses || 12;
    const salida = [];
    let i = 0;
    while (importe > 0 && salida.length < MAX_MESES) {
        const dia = sumarMeses(desde, i * cadaMeses);
        if (hasta && clave(dia) > clave(hasta)) break;
        salida.push({fecha: iso(dia), importe: importe});
        i++;
        if (i * cadaMeses > MAX_MESES) break;
    }
    return salida;
}

function tirMensual(flujos) {
    if (!flujos.some(function (f) { return f < 0; }) || !flujos.some(function (f) { return f > 0; })) return null;
    const va = function (r) {
        let s = 0;
        for (let t = 0; t < flujos.length; t++) s += flujos[t] / Math.pow(1 + r, t);
        return s;
    };
    let bajo = -0.05, alto = 0.2;
    if (va(bajo) * va(alto) > 0) return null;
    for (let i = 0; i < 200; i++) {
        const medio = (bajo + alto) / 2;
        if (va(bajo) * va(medio) <= 0) alto = medio; else bajo = medio;
    }
    return (bajo + alto) / 2;
}

function rentabilidadDeAmortizar(base, nuevo, desde) {
    const d = clave(desde);
    const porMes = function (filas) {
        const m = {};
        for (const f of filas) {
            const fe = fecha(f.fecha);
            if (clave(fe) < d) continue;
            const c = fe.y * 12 + fe.m;
            m[c] = (m[c] || 0) + f.cuota + f.extra + f.comision;
        }
        return m;
    };
    const b = porMes(base), n = porMes(nuevo);
    const meses = Object.keys(b).concat(Object.keys(n)).map(Number);
    if (!meses.length) return null;
    const ini = Math.min.apply(null, meses), fin = Math.max.apply(null, meses);
    const flujos = [];
    for (let m = ini; m <= fin; m++) flujos.push((b[m] || 0) - (n[m] || 0));
    const r = tirMensual(flujos);
    return r === null ? null : Math.round(r * 1200 * 100) / 100;
}

function resultado(filas) {
    const t = totales(filas);
    t.anual = resumenAnual(filas);
    return t;
}

function simularProntoPago(p, puntuales, periodica, tipoReferencia, hoy) {
    hoy = hoy ? fecha(hoy) : (function () {
        const d = new Date();
        return {y: d.getFullYear(), m: d.getMonth() + 1, d: d.getDate()};
    })();
    let nuevas = (puntuales || []).filter(function (a) { return +(a.importe || 0) > 0; })
        .map(function (a) { return {fecha: iso(fecha(a.fecha)), importe: +a.importe}; });
    if (periodica && +(periodica.importe || 0) > 0) {
        nuevas = nuevas.concat(extrasPeriodicos(+periodica.importe, periodica.desde || iso(hoy),
                                                periodica.hasta, +(periodica.cada_meses || 12)));
    }
    const base = cuadro(p);
    const res = {base: resultado(base), tipo_referencia: vacio(tipoReferencia) ? null : tipoReferencia};
    let desde = hoy;
    if (nuevas.length) {
        desde = nuevas.map(function (a) { return fecha(a.fecha); })
            .reduce(function (a, b) { return clave(a) <= clave(b) ? a : b; });
    }
    ['cuota', 'plazo'].forEach(function (modo) {
        const q = Object.assign({}, p, {
            amortizaciones: (p.amortizaciones || []).concat(nuevas.map(function (a) {
                return Object.assign({}, a, {modo: modo});
            })),
        });
        const filas = cuadro(q);
        const r = resultado(filas);
        r.intereses_ahorrados = r2(res.base.intereses - r.intereses);
        r.comision_pagada = r2(r.comision - res.base.comision);
        r.ahorro_neto = r2(r.intereses_ahorrados - r.comision_pagada);
        r.amortizado = r2(r.extra - res.base.extra);
        r.meses_menos = res.base.cuotas - r.cuotas;
        const post = filas.filter(function (f) { return clave(fecha(f.fecha)) > clave(desde); });
        r.cuota_despues = post.length ? post[0].cuota : 0;
        r.rentabilidad = rentabilidadDeAmortizar(base, filas, sumarMeses(desde, -1));
        if (res.tipo_referencia !== null && r.rentabilidad !== null) {
            r.frente_a_invertir = Math.round((r.rentabilidad - +res.tipo_referencia) * 100) / 100;
        }
        res[modo] = r;
    });
    return res;
}

global.Amortizacion = {
    COMISION_FIJO: COMISION_FIJO,
    COMISION_VARIABLE: COMISION_VARIABLE,
    COMISION_VARIABLE_5: COMISION_VARIABLE_5,
    r2: r2,
    cuota: cuota,
    capitalParaCuota: capitalParaCuota,
    cuotasRestantes: cuotasRestantes,
    tipoEn: function (p, inicioPeriodo, meses) { return tipoEn(p, fecha(inicioPeriodo), meses); },
    pctComision: pctComision,
    comision: comision,
    cuadro: cuadro,
    saldoA: saldoA,
    resumenAnual: resumenAnual,
    delAnio: delAnio,
    totales: totales,
    siguientes: siguientes,
    extrasPeriodicos: extrasPeriodicos,
    simularProntoPago: simularProntoPago,
};

})(typeof window !== 'undefined' ? window : globalThis);
