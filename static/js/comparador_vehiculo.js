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
 *
 * EL PERIODO ES EL TIEMPO QUE QUIERES TENER COCHE, NO EL DEL CONTRATO
 *
 * Un leasing de 48 meses que devuelves sale barato a 48 meses, pero al
 * devolverlo necesitas otro coche. Por eso el leasing se renueva al acabar y
 * el coche comprado se cambia cuando dices (o te lo quedas): a diez años se
 * comparan diez años de coche en los dos casos. La misma cuenta, hecha cada
 * mes, es la curva de la gráfica: lo que te habría costado dejarlo ese mes.
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

/* Lo que cuesta USAR el coche cada mes, quitando lo que incluya la cuota.
 *
 * `incluye`: si ese mes la cuota del leasing cubre lo marcado (deja de
 * hacerlo si te quedas el coche y acaba el contrato). `edad`: cuánto se
 * multiplican el taller y los neumáticos porque el coche ya no es nuevo. */
function costeUsoMensual(o, kmAnuales, incluye, edad) {
    if (incluye === undefined) incluye = true;
    if (!(edad > 0)) edad = 1;
    const incluido = function (clave) { return o.tipo === 'leasing' && incluye && !!o['incl_' + clave]; };
    const fijo =
        (incluido('seguro') ? 0 : num(o.seguro_anual)) +
        (incluido('mantenimiento') ? 0 : num(o.mantenimiento_anual) * edad) +
        (incluido('neumaticos') ? 0 : num(o.neumaticos_anual) * edad) +
        (incluido('impuesto') ? 0 : num(o.impuesto_anual));
    const energia = kmAnuales / 12 * num(o.consumo) / 100 * num(o.precio_energia);
    return {fijo: fijo / 12, energia: energia, total: fijo / 12 + energia};
}

/* Simula una opción durante `horizonte` meses, mes a mes.
 *
 * `o`: la opción (tipo 'leasing' | 'financiado' | 'contado' y sus campos).
 * `g`: lo común — horizonte (meses), km_anuales, rentabilidad (% anual que
 *      darían los ahorros si no se gastaran en el coche), inflacion (% anual
 *      que suben los coches: encarece renovar y comprar el siguiente) y
 *      envejecimiento (% que sube cada año de edad el taller y los neumáticos).
 *
 * Nunca te quedas sin coche: el leasing que devuelves se renueva por otro
 * igual al acabar el contrato, y el coche comprado se cambia cada
 * `cambio_cada` meses (vacío = te lo quedas todo el periodo). Así el periodo
 * puede ser tan largo como quieras tener coche —diez años, por ejemplo— y las
 * opciones siguen comparando lo mismo.
 */
function simular(o, g) {
    const H = Math.max(Math.round(num(g.horizonte, 48)), 1);
    const km = Math.max(num(g.km_anuales), 0);
    const precio = Math.max(num(o.precio), 0);
    const inflacion = Math.max(num(g.inflacion), 0) / 100;
    const envejecimiento = Math.max(num(g.envejecimiento), 0) / 100;
    const subida = function (mes) { return Math.pow(1 + inflacion, mes / 12); };
    const avisos = [];

    // Mes a mes (0 = el día de la firma): lo que sale de la cuenta para PAGAR
    // el coche, lo que debes y lo que vale el coche si es tuyo.
    const pagos = new Array(H + 1).fill(0);
    const deudaMes = new Array(H + 1).fill(0);
    const valorMes = new Array(H + 1).fill(0);
    const edadMes = new Array(H + 1).fill(0);     // meses que tiene el coche
    const inclMes = new Array(H + 1).fill(false);  // ¿la cuota incluye lo marcado?
    const eventos = [];     // {mes, texto}: renovaciones, cambios, cuota final
    let intereses = 0;
    let cuota = 0;
    let totalCuotas = 0;
    let numCuotas = 0;
    let exceso = 0;
    let esTuyo = true;
    let edadVenta = H;      // edad a la que se valora el coche («Valdrá al venderlo»)

    // Lo que vale el coche según su edad: la curva típica o, si pones cuánto
    // valdrá al venderlo, esa misma curva doblada para pasar por tu cifra.
    const valorManual = !(o.valor_final_pct === '' || o.valor_final_pct === null || o.valor_final_pct === undefined);
    let pctEdad = valorResidualEstimado;

    const gastos = Math.max(num(o.gastos_iniciales), 0);

    if (o.tipo === 'contado' || o.tipo === 'financiado') {
        const cambio = Math.round(num(o.cambio_cada));
        const ciclo = cambio > 0 && cambio < H ? cambio : H;
        edadVenta = ciclo;
        if (valorManual) {
            const k = Math.max(num(o.valor_final_pct), 0) / Math.max(valorResidualEstimado(ciclo), 1e-9);
            pctEdad = function (m) { return valorResidualEstimado(m) * Math.pow(k, m / ciclo); };
        }
        const financiado = o.tipo === 'financiado';
        const plazo = Math.max(Math.round(num(o.meses, 60)), 1);
        const tin = Math.max(num(o.tin), 0);
        for (let ini = 0; ini < H; ini += ciclo) {
            const fin = Math.min(ini + ciclo, H);
            const f = subida(ini);
            const pvp = precio * f;
            let capital = 0, cuotaCiclo = 0, comision = 0;
            pagos[ini] += gastos * f;
            if (financiado) {
                const entrada = Math.min(Math.max(num(o.entrada), 0) * f, pvp);
                capital = pvp - entrada;
                comision = capital * Math.max(num(o.comision_pct), 0) / 100;
                cuotaCiclo = cuotaPrestamo(capital, tin, plazo);
                if (ini === 0) cuota = cuotaCiclo;
                pagos[ini] += entrada + comision;
            } else {
                pagos[ini] += pvp;
            }
            const pagadas = financiado ? Math.min(plazo, fin - ini) : 0;
            for (let m = ini + 1; m <= ini + pagadas; m++) pagos[m] += cuotaCiclo;
            totalCuotas += cuotaCiclo * pagadas;
            numCuotas += pagadas;
            for (let m = ini; m <= fin; m++) {
                edadMes[m] = m - ini;
                valorMes[m] = pvp * Math.max(pctEdad(m - ini), 0) / 100;
                deudaMes[m] = financiado ? saldoPendiente(capital, tin, plazo, Math.min(m - ini, plazo)) : 0;
            }
            if (financiado) intereses += cuotaCiclo * pagadas - (capital - deudaMes[fin]) + comision;
            if (fin < H) {
                // Se vende el coche (y se cancela lo que quede del préstamo) y,
                // ese mismo mes, empieza el siguiente ciclo con uno nuevo.
                pagos[fin] += deudaMes[fin] - valorMes[fin];
                eventos.push({mes: fin, texto: 'Vendes por ' + Math.round(valorMes[fin]).toLocaleString('es-ES') +
                    ' €' + (deudaMes[fin] > 0.5 ? ', cancelas ' + Math.round(deudaMes[fin]).toLocaleString('es-ES') + ' € de préstamo' : '') +
                    ' y compras otro'});
            }
        }
        if (financiado && deudaMes[H] > 0.5) {
            avisos.push('Al final del periodo aún debes ' + Math.round(deudaMes[H]).toLocaleString('es-ES') + ' € del préstamo: cuentan como coste.');
        }
    } else if (!o.quedarse) {
        // Leasing que devuelves: al acabar, otro contrato igual con un coche nuevo.
        const meses = Math.max(Math.round(num(o.meses, 48)), 1);
        const contratados = num(o.km_contratados);
        const extraAnual = contratados > 0 && km > contratados ? (km - contratados) * Math.max(num(o.coste_km_extra), 0) : 0;
        esTuyo = false;
        edadVenta = 0;
        for (let ini = 0; ini < H; ini += meses) {
            const fin = Math.min(ini + meses, H);
            const f = subida(ini);
            const cuotaCiclo = Math.max(num(o.cuota), 0) * f;
            if (ini === 0) cuota = cuotaCiclo;
            else eventos.push({mes: ini, texto: 'Devuelves el coche y firmas otro leasing' + (inflacion ? ' (cuota ' + Math.round(cuotaCiclo).toLocaleString('es-ES') + ' €)' : '')});
            const pagoInicial = (Math.max(num(o.entrada), 0) + gastos) * f;
            pagos[ini] += pagoInicial;
            for (let m = ini + 1; m <= fin; m++) pagos[m] += cuotaCiclo;
            totalCuotas += cuotaCiclo * (fin - ini);
            numCuotas += fin - ini;
            for (let m = ini; m <= fin; m++) {
                edadMes[m] = m - ini;
                inclMes[m] = true;
                // Los km de más se pagan al devolverlo; mientras, se van debiendo.
                deudaMes[m] = extraAnual * (m - ini) / 12;
                // La entrada paga todo el contrato: la parte de los meses que
                // aún no has usado se descuenta como lo que vale un coche
                // tuyo. Sin esto, acabar el periodo a mitad de un leasing
                // cargaría la entrada entera por unos pocos meses de coche.
                valorMes[m] = pagoInicial * (1 - (m - ini) / meses);
            }
            if (ini + meses <= H) {
                pagos[fin] += deudaMes[fin];
                exceso += deudaMes[fin];
                deudaMes[fin] = 0;
            } else {
                exceso += deudaMes[fin];
                avisos.push('El contrato ' + (ini ? 'en curso ' : '') + 'dura hasta el mes ' + (ini + meses) + ' y el periodo acaba en el ' + H +
                    ': quedan ' + (ini + meses - H) + ' cuotas por pagar, que no cuentan como coste porque a cambio sigues teniendo coche esos meses' +
                    (pagoInicial > 0 ? ', y de su entrada solo cuenta la parte de los meses usados' : '') + '.');
            }
        }
        if (exceso > 0) {
            avisos.push('Haces ' + Math.round(km - contratados).toLocaleString('es-ES') +
                        ' km/año más de los contratados: ' + Math.round(exceso).toLocaleString('es-ES') + ' € al devolverlo.');
        }
        if (H > meses) {
            avisos.push('El contrato dura ' + meses + ' meses: en ' + H + ' meses firmas ' + Math.ceil(H / meses) +
                ' leasings seguidos' + (inflacion ? ', cada uno con la cuota subida lo que suben los coches' : '') + '.');
        }
    } else {
        // Leasing que te quedas: pagas la cuota final y el coche sigue contigo.
        const meses = Math.max(Math.round(num(o.meses, 48)), 1);
        const final = Math.max(num(o.cuota_final), 0);
        const entrada = Math.max(num(o.entrada), 0);
        cuota = Math.max(num(o.cuota), 0);
        if (valorManual) {
            const k = Math.max(num(o.valor_final_pct), 0) / Math.max(valorResidualEstimado(H), 1e-9);
            pctEdad = function (m) { return valorResidualEstimado(m) * Math.pow(k, m / H); };
        }
        pagos[0] += entrada + gastos;
        const pagadas = Math.min(meses, H);
        for (let m = 1; m <= pagadas; m++) pagos[m] += cuota;
        totalCuotas = cuota * pagadas;
        numCuotas = pagadas;
        if (meses <= H) {
            pagos[meses] += final;
            eventos.push({mes: meses, texto: 'Pagas la cuota final y el coche es tuyo'});
        }
        for (let m = 0; m <= H; m++) {
            edadMes[m] = m;
            inclMes[m] = m <= meses;
            valorMes[m] = precio * Math.max(pctEdad(m), 0) / 100;
            deudaMes[m] = m < meses ? cuota * (meses - m) + final : 0;
        }
        if (meses > H) {
            avisos.push('El contrato dura ' + meses + ' meses y el periodo ' + H + ': las ' + (meses - H) +
                        ' cuotas que faltan y la cuota final cuentan como coste.');
        }
        if (precio > 0) intereses = Math.max(entrada + cuota * meses + final - precio, 0);
    }

    // El uso, mes a mes. El taller y los neumáticos suben con la edad del
    // coche: un leasing siempre lleva un coche joven, uno comprado envejece.
    const usoMes = new Array(H + 1).fill(0);
    let usoFijo1 = 0, energia = 0;
    for (let m = 1; m <= H; m++) {
        const u = costeUsoMensual(o, km, inclMes[m], Math.pow(1 + envejecimiento, edadMes[m - 1] / 12));
        usoMes[m] = u.total;
        if (m === 1) { usoFijo1 = u.fijo; energia = u.energia; }
    }

    const flujos = pagos.map(function (p, m) { return p + usoMes[m]; });
    const totalPagosCoche = pagos.reduce(function (a, b) { return a + b; }, 0);
    const totalUso = usoMes.reduce(function (a, b) { return a + b; }, 0);
    const totalPagado = totalPagosCoche + totalUso;
    const deuda = deudaMes[H];
    const valorFinal = valorMes[H];
    const costeReal = totalPagado + deuda - valorFinal;
    const kmTotales = km * H / 12;

    // Coste de oportunidad: lo que habría rendido ese dinero hasta el final si,
    // en vez de salir de la cuenta, se hubiera quedado invertido. Es lo que
    // hace que pagar 30.000 € el primer día no sea lo mismo que pagarlos en
    // cuatro años.
    const r = Math.max(num(g.rentabilidad), 0) / 100 / 12;
    let oportunidad = 0;
    if (r > 0) {
        for (let m = 0; m <= H; m++) oportunidad += flujos[m] * (Math.pow(1 + r, H - m) - 1);
    }

    // Dos curvas mes a mes. `acumulado`: lo que ha salido de la cuenta.
    // `coste`: lo que te ha costado de verdad si lo dejaras ese mes —vendes el
    // coche o devuelves el leasing—: lo pagado + lo que debes − lo que vale.
    // La segunda es la que se compara; en el último mes es el coste real.
    const acumulado = [];
    const coste = [];
    let suma = 0;
    for (let m = 0; m <= H; m++) {
        suma += flujos[m];
        acumulado.push(suma);
        coste.push(suma + deudaMes[m] - valorMes[m]);
    }

    const pagoFinal = totalPagosCoche - pagos[0] - totalCuotas;
    return {
        horizonte: H,
        desembolso_inicial: flujos[0],
        cuota: cuota,
        num_cuotas: numCuotas,
        total_cuotas: totalCuotas,
        // Todo lo que no es el primer día ni cuota: cuota final, km de más,
        // renovar el leasing o vender el coche y comprar otro (en neto).
        pago_final: pagoFinal,
        eventos: eventos,
        valor_estimado: !valorManual,
        edad_venta: edadVenta,
        meses_con_coche: H,
        uso_mensual: usoFijo1 + energia,
        uso_fijo_mensual: usoFijo1,
        energia_mensual: energia,
        // Lo que sale de la cuenta un mes normal: sin la entrada ni la cuota
        // final, que son pagos de una vez.
        pago_mensual_tipico: cuota + usoFijo1 + energia,
        salida_media_mensual: flujos.slice(1).reduce(function (a, b) { return a + b; }, 0) / H,
        total_pagos_coche: totalPagosCoche,
        total_uso: totalUso,
        total_pagado: totalPagado,
        deuda_final: deuda,
        valor_final: valorFinal,
        valor_final_pct: esTuyo && precio > 0 ? valorFinal / (precio * subida(H - edadMes[H])) * 100 : 0,
        edad_final: edadMes[H],
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
        coste: coste,
        deuda_mes: deudaMes,
        valor_mes: valorMes,
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
