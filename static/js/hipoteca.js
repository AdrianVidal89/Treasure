/* Motor del simulador de vivienda.
 *
 * Todo son funciones puras: entran números, salen números. Viven aquí y no en
 * la plantilla para poder probarlas de verdad (y para que la plantilla se lea).
 * Los porcentajes de impuestos y los valores por defecto los pone el servidor
 * (finanzas/hipoteca.py) y llegan como datos, para que no haya dos verdades.
 */
(function (global) {
'use strict';

// ── Préstamo francés ────────────────────────────────────────────────────────

function cuotaMensual(capital, tipoAnual, años) {
    if (!(capital > 0) || !(años > 0)) return 0;
    const n = años * 12;
    if (!tipoAnual) return capital / n;
    const r = tipoAnual / 100 / 12;
    return capital * r * Math.pow(1 + r, n) / (Math.pow(1 + r, n) - 1);
}

function capitalMaximo(cuotaMax, tipoAnual, años) {
    if (!(cuotaMax > 0) || !(años > 0)) return 0;
    const n = años * 12;
    if (!tipoAnual) return cuotaMax * n;
    const r = tipoAnual / 100 / 12;
    return cuotaMax * (Math.pow(1 + r, n) - 1) / (r * Math.pow(1 + r, n));
}

/* Tipo que se aplica cada año según la modalidad.
 *   fijo     → el mismo siempre
 *   variable → euríbor + diferencial desde el primer día
 *   mixto    → un tipo fijo los primeros `añosFijos`, luego euríbor + diferencial
 * `euriborDelta` desplaza el euríbor: es la palanca del test de estrés. */
function tipoEnAño(cfg, año) {
    const euribor = (cfg.euribor || 0) + (cfg.euriborDelta || 0);
    if (cfg.modalidad === 'fijo') return cfg.tipoFijo;
    if (cfg.modalidad === 'variable') return euribor + cfg.diferencial;
    return año < cfg.añosFijos ? cfg.tipoFijo : euribor + cfg.diferencial;
}

/* Cuadro de amortización año a año.
 *
 * En variable y mixto la cuota se recalcula cuando cambia el tipo, sobre el
 * capital que queda y el plazo que resta, que es como funciona una revisión.
 * `amortizacionAnual` es dinero extra que se mete cada año: si `modo` es
 * 'plazo' acorta el préstamo (y la cuota no baja); si es 'cuota', la recalcula
 * sobre el plazo original. */
function cuadroAmortizacion(capital, cfg, años, amortizacionAnual, modo) {
    amortizacionAnual = amortizacionAnual || 0;
    const filas = [];
    let pendiente = capital;
    let plazoRestante = años;
    let tipoAnterior = null;
    let cuota = 0;
    let interesesAcum = 0;

    for (let a = 0; a < años && pendiente > 0.01; a++) {
        const tipo = tipoEnAño(cfg, a);
        if (tipo !== tipoAnterior || cuota === 0) {
            cuota = cuotaMensual(pendiente, tipo, plazoRestante);
            tipoAnterior = tipo;
        }
        const r = tipo / 100 / 12;
        let interesAño = 0, capitalAño = 0;

        for (let m = 0; m < 12 && pendiente > 0.01; m++) {
            const interes = pendiente * r;
            let amortiza = Math.min(cuota - interes, pendiente);
            if (amortiza < 0) amortiza = 0;
            pendiente -= amortiza;
            interesAño += interes;
            capitalAño += amortiza;
        }

        if (amortizacionAnual > 0 && pendiente > 0.01) {
            const extra = Math.min(amortizacionAnual, pendiente);
            pendiente -= extra;
            capitalAño += extra;
            if (modo === 'cuota') {
                cuota = cuotaMensual(pendiente, tipo, años - a - 1);
                tipoAnterior = null;   // fuerza recálculo el año siguiente
            }
        }

        interesesAcum += interesAño;
        filas.push({
            año: a + 1, tipo: tipo, cuota: cuota,
            intereses: interesAño, capital: capitalAño,
            pendiente: Math.max(0, pendiente), interesesAcum: interesesAcum,
        });
        plazoRestante = Math.max(1, plazoRestante - 1);
    }
    return filas;
}

function totalIntereses(capital, cfg, años, amortizacionAnual, modo) {
    const filas = cuadroAmortizacion(capital, cfg, años, amortizacionAnual, modo);
    return filas.length ? filas[filas.length - 1].interesesAcum : 0;
}

/* La cuota que hay que poder pagar, no la del primer año: en variable y mixto
 * la de partida es la más baja del préstamo, y decidir con ella es engañarse. */
function cuotaMaxima(capital, cfg, años) {
    const filas = cuadroAmortizacion(capital, cfg, años, 0, 'plazo');
    return filas.reduce(function (m, f) { return Math.max(m, f.cuota); }, 0);
}

// ── Gastos de compra ────────────────────────────────────────────────────────

function gastosCompra(precio, ccaa, obraNueva, joven, D) {
    if (!(precio > 0)) {
        return {impuestos: 0, notaria: 0, registro: 0, gestoria: 0, tasacion: 0,
                total: 0, totalPct: 0, impuestoNombre: '—', impuestoPct: 0};
    }
    let impuestoPct, impuestoNombre;
    if (obraNueva) {
        impuestoPct = D.iva_obra_nueva + ccaa.ajd;
        impuestoNombre = 'IVA ' + D.iva_obra_nueva + '% + AJD ' + ccaa.ajd + '%';
    } else {
        impuestoPct = (joven && ccaa.itp_joven !== null) ? ccaa.itp_joven : ccaa.itp;
        impuestoNombre = 'ITP ' + impuestoPct + '%';
    }
    const entre = function (v, lo, hi) { return Math.max(lo, Math.min(hi, v)); };
    const impuestos = precio * impuestoPct / 100;
    const notaria   = entre(precio * D.notaria_pct / 100, D.notaria_min, D.notaria_max);
    const registro  = entre(precio * D.registro_pct / 100, D.registro_min, D.registro_max);
    const total = impuestos + notaria + registro + D.gestoria + D.tasacion;
    return {
        impuestos: impuestos, notaria: notaria, registro: registro,
        gestoria: D.gestoria, tasacion: D.tasacion,
        total: total, totalPct: total / precio * 100,
        impuestoNombre: impuestoNombre, impuestoPct: impuestoPct,
    };
}

// ── Coste de tener la vivienda ──────────────────────────────────────────────

function costeRecurrente(valor, p) {
    const mantenimiento = valor * p.mantenimientoPct / 100 / 12;
    const ibi = valor * p.ibiPct / 100 / 12;
    const seguro = p.seguroAnual / 12;
    return {
        mantenimiento: mantenimiento, ibi: ibi, seguro: seguro,
        comunidad: p.comunidadMensual,
        total: mantenimiento + ibi + seguro + p.comunidadMensual,
    };
}

// ── Capacidad de compra ─────────────────────────────────────────────────────

/* Precio máximo alcanzable con un capital y una cuota tope.
 *
 * La cuota tope manda sobre el capital que se puede pedir; el capital propio
 * tiene que cubrir la entrada mínima MÁS los gastos de compra. Se resuelve
 * iterando porque los gastos dependen del precio (y el impuesto es un % de él). */
function precioMaximo(capitalDisponible, cuotaTope, cfg, años, entradaMinPct, ccaa, obraNueva, joven, D) {
    const hipMax = capitalMaximo(cuotaTope, tipoEnAño(cfg, 0), años);
    let precio = capitalDisponible + hipMax;
    for (let i = 0; i < 40; i++) {
        const g = gastosCompra(precio, ccaa, obraNueva, joven, D);
        // Con gastos g, el capital que queda para la entrada es este:
        const paraEntrada = Math.max(0, capitalDisponible - g.total);
        // Precio que sale de juntar entrada + hipoteca, respetando la entrada mínima
        const porCapital = paraEntrada + hipMax;
        const porEntrada = entradaMinPct > 0 ? paraEntrada / (entradaMinPct / 100) : porCapital;
        const nuevo = Math.max(0, Math.min(porCapital, porEntrada));
        if (Math.abs(nuevo - precio) < 1) { precio = nuevo; break; }
        precio = nuevo;
    }
    return Math.max(0, precio);
}

// ── Comprar vs alquilar ─────────────────────────────────────────────────────

/* Comprar frente a seguir de alquiler, año a año.
 *
 * Los dos parten del MISMO dinero (la entrada más los gastos de compra) y del
 * mismo sueldo, así que se pueden comparar sin trampa:
 *
 *   · El comprador mete ese dinero en la casa y cada año paga cuota y costes
 *     de tenerla. Su patrimonio es lo que vale la casa menos lo que debe.
 *   · El inquilino conserva ese dinero invertido y, además, mete en la cartera
 *     la diferencia entre lo que le costaría comprar y lo que paga de alquiler
 *     (si el alquiler es más caro, la cartera mengua). Su patrimonio es la
 *     cartera.
 *
 * Esa segunda parte es la que se suele olvidar, y sin ella la comparación está
 * trucada a favor de comprar.
 */
function compararComprarAlquilar(o) {
    const filas = [];
    const cuadro = cuadroAmortizacion(o.hipoteca, o.cfg, o.años, 0, 'plazo');

    let alquilerAnual = o.alquilerMensual * 12;
    let cartera = o.capitalEntrada + o.gastosCompra;   // lo que el inquilino NO gasta
    let añoEquilibrio = null;

    for (let a = 0; a < o.años; a++) {
        const fila = cuadro[a];
        if (!fila) break;
        const valorVivienda = o.precio * Math.pow(1 + o.revalorizacionPct / 100, a + 1);
        const recurrenteAnual = costeRecurrente(valorVivienda, o.recurrente).total * 12;
        const desembolsoComprador = fila.cuota * 12 + recurrenteAnual;

        cartera *= (1 + o.rentabilidadPct / 100);
        cartera += (desembolsoComprador - alquilerAnual);

        const netoComprar = valorVivienda - fila.pendiente;
        const netoAlquilar = cartera;

        if (añoEquilibrio === null && netoComprar >= netoAlquilar) añoEquilibrio = a + 1;

        filas.push({
            año: a + 1, netoComprar: netoComprar, netoAlquilar: netoAlquilar,
            alquilerAnual: alquilerAnual, valorVivienda: valorVivienda,
            interesesAño: fila.intereses, recurrenteAño: recurrenteAnual,
        });
        alquilerAnual *= (1 + o.subidaAlquilerPct / 100);
    }
    return {filas: filas, añoEquilibrio: añoEquilibrio};
}

// ── ¿Cuándo llego? ──────────────────────────────────────────────────────────

function mesesHastaReunir(falta, ahorroMensual) {
    if (falta <= 0) return 0;
    if (!(ahorroMensual > 0)) return null;
    return Math.ceil(falta / ahorroMensual);
}

global.Hipoteca = {
    cuotaMensual: cuotaMensual,
    capitalMaximo: capitalMaximo,
    tipoEnAño: tipoEnAño,
    cuadroAmortizacion: cuadroAmortizacion,
    totalIntereses: totalIntereses,
    cuotaMaxima: cuotaMaxima,
    gastosCompra: gastosCompra,
    costeRecurrente: costeRecurrente,
    precioMaximo: precioMaximo,
    compararComprarAlquilar: compararComprarAlquilar,
    mesesHastaReunir: mesesHastaReunir,
};

})(typeof window !== 'undefined' ? window : globalThis);
