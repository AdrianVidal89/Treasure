/* Motor del comparador de coche: comprar, financiar o leasing.
 *
 * Funciones puras: entra la configuración de cada opción y la común
 * (periodo, kilómetros), salen las cifras. Vive aquí y no en la plantilla
 * para poder probarlo con node y para que la plantilla solo pinte.
 *
 * LA CUENTA QUE HACE JUSTA LA COMPARACIÓN
 *
 * Comparar solo cuotas engaña: el leasing tiene la cuota baja porque al final
 * devuelves el coche, y la compra la tiene alta porque al final el coche es
 * tuyo y vale dinero. Por eso todo se lleva al mismo mes —el final del
 * periodo que se compara— y se mira cuánto te ha costado de verdad:
 *
 *     coste real = lo que has pagado hasta ese mes
 *                + lo que aún debes (cuotas y cuota final comprometidas)
 *                − lo que vale el coche si es tuyo
 *
 * Y sobre eso, el uso: seguro, mantenimiento, neumáticos, impuesto y energía,
 * salvo lo que la cuota ya incluya.
 */
(function (global) {
'use strict';

// Depreciación típica de un coche nuevo: un 20 % el primer año y en torno a
// un 13 % cada año siguiente. Es solo el valor por defecto: cada opción puede
// poner el suyo, que es lo que hay que hacer con un modelo concreto.
const DEPRECIACION_PRIMER_AÑO = 0.20;
const DEPRECIACION_AÑOS_SIGUIENTES = 0.13;

function num(v, porDefecto) {
    const n = typeof v === 'number' ? v : parseFloat(String(v === undefined || v === null ? '' : v).replace(',', '.'));
    return isFinite(n) ? n : (porDefecto || 0);
}

/* % del precio que vale el coche tras `meses`, con la curva por defecto. */
function valorResidualEstimado(meses) {
    const años = Math.max(num(meses), 0) / 12;
    if (años <= 1) return 100 * (1 - DEPRECIACION_PRIMER_AÑO * años);
    return 100 * (1 - DEPRECIACION_PRIMER_AÑO) * Math.pow(1 - DEPRECIACION_AÑOS_SIGUIENTES, años - 1);
}

function cuotaPrestamo(capital, tinAnual, meses) {
    if (!(capital > 0) || !(meses > 0)) return 0;
    if (!tinAnual) return capital / meses;
    const r = tinAnual / 100 / 12;
    return capital * r * Math.pow(1 + r, meses) / (Math.pow(1 + r, meses) - 1);
}

/* Lo que queda por devolver de un préstamo francés tras pagar `pagadas` cuotas. */
function saldoPendiente(capital, tinAnual, meses, pagadas) {
    if (!(capital > 0) || pagadas >= meses) return 0;
    if (!tinAnual) return capital * (1 - pagadas / meses);
    const r = tinAnual / 100 / 12;
    const c = cuotaPrestamo(capital, tinAnual, meses);
    return capital * Math.pow(1 + r, pagadas) - c * (Math.pow(1 + r, pagadas) - 1) / r;
}

/* Lo que cuesta USAR el coche cada mes, quitando lo que incluya la cuota. */
function costeUsoMensual(o, kmAnuales) {
    const incluido = function (clave) { return o.tipo === 'leasing' && !!o['incl_' + clave]; };
    const fijo =
        (incluido('seguro') ? 0 : num(o.seguro_anual)) +
        (incluido('mantenimiento') ? 0 : num(o.mantenimiento_anual)) +
        (incluido('neumaticos') ? 0 : num(o.neumaticos_anual)) +
        (incluido('impuesto') ? 0 : num(o.impuesto_anual));
    const energia = kmAnuales / 12 * num(o.consumo) / 100 * num(o.precio_energia);
    return {fijo: fijo / 12, energia: energia, total: fijo / 12 + energia};
}

/* Simula una opción durante `horizonte` meses.
 *
 * `o`: la opción (tipo 'leasing' | 'financiado' | 'contado' y sus campos).
 * `g`: lo común — horizonte (meses), km_anuales y rentabilidad (% anual que
 *      darían los ahorros si no se gastaran en el coche).
 */
function simular(o, g) {
    const H = Math.max(Math.round(num(g.horizonte, 48)), 1);
    const km = Math.max(num(g.km_anuales), 0);
    const precio = Math.max(num(o.precio), 0);
    const avisos = [];

    // Lo que sale de la cuenta cada mes para PAGAR el coche (0 = el día de la firma).
    const pagos = new Array(H + 1).fill(0);
    let deuda = 0;          // lo comprometido que aún no se ha pagado al final
    let esTuyo = true;      // al final del periodo, ¿el coche es tuyo?
    let mesesConCoche = H;
    let intereses = 0;
    let cuota = 0;
    let exceso = 0;
    // Las piezas de la cuenta, para poder enseñarla sumando: sin ellas el
    // «coste real» era una cifra que nadie podía comprobar a mano.
    let numCuotas = 0;
    let pagoFinal = 0;
    let pagoFinalTipo = '';

    pagos[0] += Math.max(num(o.gastos_iniciales), 0);

    if (o.tipo === 'contado') {
        pagos[0] += precio;
    } else if (o.tipo === 'financiado') {
        const entrada = Math.min(Math.max(num(o.entrada), 0), precio);
        const capital = precio - entrada;
        const meses = Math.max(Math.round(num(o.meses, 60)), 1);
        const tin = Math.max(num(o.tin), 0);
        const comision = capital * Math.max(num(o.comision_pct), 0) / 100;
        cuota = cuotaPrestamo(capital, tin, meses);
        pagos[0] += entrada + comision;
        const pagadas = Math.min(meses, H);
        for (let m = 1; m <= pagadas; m++) pagos[m] += cuota;
        numCuotas = pagadas;
        deuda = saldoPendiente(capital, tin, meses, pagadas);
        // Intereses: lo pagado en cuotas menos el capital que se ha devuelto,
        // más la comisión, que es coste de financiarse y no del coche.
        intereses = cuota * pagadas - (capital - deuda) + comision;
        if (meses > H) {
            avisos.push('Al final del periodo aún debes ' + Math.round(deuda).toLocaleString('es-ES') + ' € del préstamo: cuentan como coste.');
        }
    } else {
        // Leasing / renting con cuota final.
        const meses = Math.max(Math.round(num(o.meses, 48)), 1);
        const final = Math.max(num(o.cuota_final), 0);
        const quedarse = !!o.quedarse;
        cuota = Math.max(num(o.cuota), 0);
        pagos[0] += Math.max(num(o.entrada), 0);
        const pagadas = Math.min(meses, H);
        for (let m = 1; m <= pagadas; m++) pagos[m] += cuota;
        numCuotas = pagadas;

        // Kilómetros por encima de lo contratado: se pagan al devolverlo.
        const contratados = num(o.km_contratados);
        if (!quedarse && contratados > 0 && km > contratados) {
            exceso = (km - contratados) * pagadas / 12 * Math.max(num(o.coste_km_extra), 0);
        }

        if (meses <= H) {
            if (quedarse) { pagos[meses] += final; pagoFinal = final; pagoFinalTipo = 'cuota_final'; }
            else if (exceso > 0) { pagos[meses] += exceso; pagoFinal = exceso; pagoFinalTipo = 'km'; }
        } else {
            deuda = cuota * (meses - H) + (quedarse ? final : 0) + (quedarse ? 0 : exceso);
            avisos.push('El contrato dura ' + meses + ' meses y el periodo ' + H + ': las ' + (meses - H) +
                        ' cuotas que faltan' + (quedarse ? ' y la cuota final' : '') + ' cuentan como coste.');
        }
        esTuyo = quedarse;
        if (!quedarse && meses < H) {
            mesesConCoche = meses;
            avisos.push('Devuelves el coche en el mes ' + meses + ': del ' + (meses + 1) + ' al ' + H +
                        ' no tienes coche, así que no compara lo mismo. Ajusta el periodo a ' + meses + ' meses.');
        }
        // Lo que se paga de más por financiarse: todo lo pagado menos el precio
        // del coche, si te lo quedas; si lo devuelves no hay «precio» con el
        // que comparar y la cifra es el alquiler entero.
        if (quedarse && precio > 0) {
            intereses = Math.max(num(o.entrada) + cuota * meses + final - precio, 0);
        }
        if (exceso > 0) {
            avisos.push('Haces ' + Math.round(km - contratados).toLocaleString('es-ES') +
                        ' km/año más de los contratados: ' + Math.round(exceso).toLocaleString('es-ES') + ' € al devolverlo.');
        }
    }

    // Lo que vale el coche al final, si es tuyo.
    const valorEstimado = (o.valor_final_pct === '' || o.valor_final_pct === null || o.valor_final_pct === undefined);
    const pctFinal = valorEstimado ? valorResidualEstimado(H) : num(o.valor_final_pct);
    const valorFinal = esTuyo ? precio * Math.max(pctFinal, 0) / 100 : 0;

    // El uso, mes a mes mientras tienes el coche.
    const uso = costeUsoMensual(o, km);
    const flujos = pagos.slice();
    for (let m = 1; m <= mesesConCoche; m++) flujos[m] += uso.total;

    const totalPagosCoche = pagos.reduce(function (a, b) { return a + b; }, 0);
    const totalUso = uso.total * mesesConCoche;
    const totalPagado = totalPagosCoche + totalUso;
    const costeReal = totalPagado + deuda - valorFinal;
    const kmTotales = km * mesesConCoche / 12;

    // Coste de oportunidad: lo que habría rendido ese dinero hasta el final si,
    // en vez de salir de la cuenta, se hubiera quedado invertido. Es lo que
    // hace que pagar 30.000 € el primer día no sea lo mismo que pagarlos en
    // cuatro años.
    const r = Math.max(num(g.rentabilidad), 0) / 100 / 12;
    let oportunidad = 0;
    if (r > 0) {
        for (let m = 0; m <= H; m++) oportunidad += flujos[m] * (Math.pow(1 + r, H - m) - 1);
    }

    const acumulado = [];
    let suma = 0;
    for (let m = 0; m <= H; m++) { suma += flujos[m]; acumulado.push(suma); }

    const mesesCuota = flujos.slice(1);
    return {
        horizonte: H,
        desembolso_inicial: flujos[0],
        cuota: cuota,
        num_cuotas: numCuotas,
        total_cuotas: cuota * numCuotas,
        pago_final: pagoFinal,
        pago_final_tipo: pagoFinalTipo,
        valor_estimado: valorEstimado,
        meses_con_coche: mesesConCoche,
        uso_mensual: uso.total,
        uso_fijo_mensual: uso.fijo,
        energia_mensual: uso.energia,
        // Lo que sale de la cuenta un mes normal: sin la entrada ni la cuota
        // final, que son pagos de una vez.
        pago_mensual_tipico: cuota + uso.total,
        salida_media_mensual: mesesCuota.reduce(function (a, b) { return a + b; }, 0) / H,
        total_pagos_coche: totalPagosCoche,
        total_uso: totalUso,
        total_pagado: totalPagado,
        deuda_final: deuda,
        valor_final: valorFinal,
        valor_final_pct: esTuyo ? pctFinal : 0,
        es_tuyo: esTuyo,
        coste_real: costeReal,
        coste_coche: costeReal - totalUso,
        coste_mensual: costeReal / H,
        km_totales: kmTotales,
        coste_km: kmTotales > 0 ? costeReal / kmTotales : 0,
        sobrecoste_financiacion: intereses,
        exceso_km: exceso,
        oportunidad: oportunidad,
        coste_con_oportunidad: costeReal + oportunidad,
        acumulado: acumulado,
        avisos: avisos,
    };
}

/* Todas las opciones a la vez, con la ganadora y lo que la separa del resto. */
function comparar(opciones, g) {
    const res = opciones.map(function (o) { return {opcion: o, r: simular(o, g)}; });
    const orden = res.slice().sort(function (a, b) { return a.r.coste_real - b.r.coste_real; });
    const mejor = orden[0] || null;
    res.forEach(function (x) {
        x.diferencia = mejor ? x.r.coste_real - mejor.r.coste_real : 0;
        x.puesto = orden.indexOf(x) + 1;
    });
    return {resultados: res, mejor: mejor, orden: orden};
}

global.ComparadorVehiculo = {
    valorResidualEstimado: valorResidualEstimado,
    cuotaPrestamo: cuotaPrestamo,
    saldoPendiente: saldoPendiente,
    costeUsoMensual: costeUsoMensual,
    simular: simular,
    comparar: comparar,
};

})(typeof window !== 'undefined' ? window : globalThis);
