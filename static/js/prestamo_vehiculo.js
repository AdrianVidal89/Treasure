/* Motor del simulador «¿cuánto coche me puedo permitir?».
 *
 * Funciones puras, probadas con node (finanzas/tests.py). La plantilla solo
 * lee campos, llama a `evaluar` y pinta.
 *
 * LA IDEA: un coche no se juzga por la cuota. Se juzga como lo mira un banco
 * prudente y como lo miraría la propia familia:
 *
 *   1. La cuota, contra el límite que TÚ pones (el slider).
 *   2. Todas tus deudas juntas (hipoteca, otros préstamos y esta), contra el
 *      35 % de los ingresos, que es donde corta la banca.
 *   3. Todo lo que cuestan tus coches (cuotas + seguro, gasolina, taller…),
 *      contra el 15 % de los ingresos: lo recomendable, y más del 25 % es
 *      demasiado para algo que pierde valor cada día.
 *   4. El mes: después de pagar coche y uso, ¿te queda algo? ¿Un 10 %?
 *   5. El capital: entrada y gastos, sin comerte el colchón de 6 meses.
 *   6. El precio frente a lo que ganas en un año: más de la mitad es mucho.
 *   7. Las condiciones: entrada de al menos un 20 % y plazo de 5 años como
 *      mucho (la regla 20/4/10, algo relajada).
 *
 * Las 1, 2, 4 y 5 (sin el colchón) son las que dicen NO. Las demás, «con
 * reservas». El precio máximo sale de las MISMAS reglas, así que el veredicto
 * y el techo no pueden contradecirse.
 */
(function (global) {
'use strict';

const LIMITE_DEUDA = 35;          // % ingresos, todas las deudas
const LIMITE_DEUDA_DURO = 40;
const COCHES_RECOMENDADO = 15;    // % ingresos, coste total de los coches
const COCHES_MAXIMO = 25;
const MARGEN_MES = 10;            // % ingresos que conviene que sobre al mes
const PRECIO_SOBRE_AÑO = 50;      // % de los ingresos netos de un año
const ENTRADA_MIN = 20;
const PLAZO_MAX = 60;

// Consumos y precios de partida por tipo de motor. Solo sugerencias: todo se
// puede sobrescribir en la pantalla.
const PROPULSIONES = {
    gasolina:  {nombre: 'Gasolina', consumo: 6.5, precio: 1.60, unidad: 'l', mant_km: 0.035},
    diesel:    {nombre: 'Diésel', consumo: 5.3, precio: 1.50, unidad: 'l', mant_km: 0.037},
    hibrido:   {nombre: 'Híbrido', consumo: 4.8, precio: 1.60, unidad: 'l', mant_km: 0.030},
    enchufable:{nombre: 'Híbrido enchufable', consumo: 3.0, precio: 1.60, unidad: 'l', mant_km: 0.032},
    electrico: {nombre: 'Eléctrico', consumo: 17, precio: 0.15, unidad: 'kWh', mant_km: 0.022},
};

// Qué se suele comprar con cada presupuesto. Orientativo: sirve para poner
// cara al número, no para elegir modelo.
const SEGMENTOS = [
    {hasta: 12000, nombre: 'Ocasión económica', que: 'Utilitario de 5 a 8 años, o un compacto con muchos kilómetros.'},
    {hasta: 20000, nombre: 'Utilitario nuevo o compacto seminuevo', que: 'Un utilitario nuevo o un compacto / SUV pequeño de 2 o 3 años.'},
    {hasta: 30000, nombre: 'Compacto o SUV pequeño nuevo', que: 'Compacto o SUV pequeño nuevo de marca generalista, o un SUV medio seminuevo.'},
    {hasta: 45000, nombre: 'SUV medio o premium compacto', que: 'SUV medio generalista bien equipado, o un compacto / SUV pequeño premium.'},
    {hasta: 70000, nombre: 'Premium', que: 'SUV o berlina premium de tamaño medio.'},
    {hasta: Infinity, nombre: 'Alta gama', que: 'Premium grande, deportivo o eléctrico de gama alta.'},
];

function num(v) { const n = parseFloat(v); return isFinite(n) ? n : 0; }

function cuotaMensual(capital, tin, meses) {
    if (!(capital > 0) || !(meses > 0)) return 0;
    if (!(tin > 0)) return capital / meses;
    const r = tin / 100 / 12;
    return capital * r * Math.pow(1 + r, meses) / (Math.pow(1 + r, meses) - 1);
}

/* Lo que cuesta USAR el coche, sugerido a partir del precio y los km. */
function sugerenciasUso(precio, kmAnuales, propulsion) {
    const p = PROPULSIONES[propulsion] || PROPULSIONES.gasolina;
    return {
        // Un todo riesgo en un coche nuevo ronda el 1-2 % de su valor al año,
        // con un suelo por el terceros ampliado de cualquier coche.
        seguro_anual: Math.round(Math.max(380, 350 + precio * 0.012)),
        energia_mensual: Math.round(kmAnuales * p.consumo / 100 * p.precio / 12),
        mantenimiento_anual: Math.round(Math.max(250, kmAnuales * p.mant_km)),
        impuestos_anual: propulsion === 'electrico' ? 60 : 140,
        parking_mensual: 0,
    };
}

function usoMensual(u) {
    return num(u.seguro_anual) / 12 + num(u.energia_mensual) + num(u.mantenimiento_anual) / 12 +
           num(u.impuestos_anual) / 12 + num(u.parking_mensual);
}

/* Todo lo que decide si te lo puedes permitir.
 *
 * `e`: precio, entrada_pct, tin, meses, contado (bool), gastos_compra_pct,
 *      uso_mensual (del coche nuevo), ingresos, gastos (los de hoy, que ya
 *      incluyen tus coches), liquidez, venta (lo que sacas vendiendo),
 *      otras_cuotas (todas tus deudas de hoy), liberado_cuota y liberado_uso
 *      (lo que deja de salir por el coche que vendes), coste_coches_hoy (lo
 *      que cuestan hoy tus coches, cuotas incluidas), max_pct (el slider),
 *      colchon_meses.
 */
function evaluar(e) {
    const precio = Math.max(num(e.precio), 0);
    const I = num(e.ingresos);
    const contado = !!e.contado;
    const gastosCompra = precio * num(e.gastos_compra_pct) / 100;
    // Lo que sacas vendiendo tus coches va, por defecto, a la entrada del
    // nuevo: es para lo que se vende el viejo, y es lo que hace bajar el
    // préstamo y la cuota. Si no, se queda como liquidez.
    const venta = Math.max(num(e.venta), 0);
    const ventaAEntrada = e.venta_a_entrada !== false && !contado;
    const entradaPropia = contado ? precio : Math.min(precio, precio * num(e.entrada_pct) / 100);
    const entradaVenta = ventaAEntrada ? Math.min(venta, precio - entradaPropia) : 0;
    const entrada = entradaPropia + entradaVenta;
    const prestamo = contado ? 0 : Math.max(precio - entrada, 0);
    const meses = Math.max(Math.round(num(e.meses)), 1);
    const cuota = contado ? 0 : cuotaMensual(prestamo, num(e.tin), meses);
    const totalDevuelto = cuota * (contado ? 0 : meses);
    const intereses = Math.max(totalDevuelto - prestamo, 0);
    const uso = Math.max(num(e.uso_mensual), 0);

    const capital = Math.max(num(e.liquidez), 0) + venta;
    const necesario = entrada + gastosCompra;
    const colchon = Math.max(num(e.gastos), 0) * (e.colchon_meses == null ? 6 : num(e.colchon_meses));
    const quedaCapital = capital - necesario;

    const otrasRestantes = Math.max(num(e.otras_cuotas) - num(e.liberado_cuota), 0);
    const cochesOtros = Math.max(num(e.coste_coches_hoy) - num(e.liberado_cuota) - num(e.liberado_uso), 0);
    const costeCoches = cochesOtros + cuota + uso;
    const libreHoy = I - num(e.gastos);
    const libreDespues = libreHoy + num(e.liberado_cuota) + num(e.liberado_uso) - cuota - uso;

    const pct = function (v) { return I > 0 ? v / I * 100 : Infinity; };
    const r = {
        precio: precio, entrada: entrada, prestamo: prestamo, cuota: cuota, meses: meses,
        entrada_propia: entradaPropia, entrada_venta: entradaVenta, venta: venta,
        prestamo_sin_venta: contado ? 0 : Math.max(precio - entradaPropia, 0),
        cuota_sin_venta: contado ? 0 : cuotaMensual(Math.max(precio - entradaPropia, 0), num(e.tin), meses),
        intereses: intereses, total_devuelto: totalDevuelto, gastos_compra: gastosCompra,
        necesario: necesario, capital: capital, queda_capital: quedaCapital, colchon: colchon,
        uso: uso, coste_mensual: cuota + uso, coste_coches: costeCoches,
        libre_hoy: libreHoy, libre_despues: libreDespues,
        pct_cuota: pct(cuota), pct_deuda: pct(otrasRestantes + cuota), pct_coches: pct(costeCoches),
        pct_libre: pct(libreDespues), otras_restantes: otrasRestantes,
        pct_precio_año: I > 0 ? precio / (I * 12) * 100 : Infinity,
        reglas: [],
    };

    const regla = function (clave, titulo, estado, detalle) {
        r.reglas.push({clave: clave, titulo: titulo, estado: estado, detalle: detalle});
    };
    const f = function (v) { return Math.round(v).toLocaleString('es-ES') + ' €'; };
    const p = function (v) { return isFinite(v) ? v.toLocaleString('es-ES', {maximumFractionDigits: 1}) + ' %' : '—'; };
    const maxPct = num(e.max_pct);

    if (!(I > 0)) {
        regla('ingresos', 'Tus ingresos', 'no',
              'No hay ingresos con los que medir: sin ellos no se puede decir que te lo puedas permitir. Revisa la fuente de datos de arriba.');
    } else {
        if (contado) {
            regla('cuota', 'La cuota, frente a tu límite', 'si', 'Al contado: no hay cuota.');
        } else {
            regla('cuota', 'La cuota, frente a tu límite', r.pct_cuota <= maxPct ? 'si' : 'no',
                  f(cuota) + '/mes es el ' + p(r.pct_cuota) + ' de tus ingresos. Tu límite: ' + p(maxPct) + '.');
        }
        regla('deuda', 'Todas tus deudas juntas',
              r.pct_deuda <= LIMITE_DEUDA ? 'si' : (r.pct_deuda <= LIMITE_DEUDA_DURO ? 'reservas' : 'no'),
              'Esta cuota más ' + f(otrasRestantes) + ' de otras cuotas: el ' + p(r.pct_deuda) +
              ' de tus ingresos. La banca corta en el ' + LIMITE_DEUDA + ' %.');
        regla('coches', 'Lo que te cuestan los coches',
              r.pct_coches <= COCHES_RECOMENDADO ? 'si' : (r.pct_coches <= COCHES_MAXIMO ? 'reservas' : 'no'),
              'Cuotas, seguro, energía, taller e impuestos de todos tus coches: ' + f(costeCoches) + '/mes, el ' +
              p(r.pct_coches) + ' de tus ingresos. Lo sano: hasta el ' + COCHES_RECOMENDADO + ' %; más del ' +
              COCHES_MAXIMO + ' % es demasiado.');
        regla('mes', 'Tu mes, después de comprar',
              libreDespues < 0 ? 'no' : (r.pct_libre < MARGEN_MES ? 'reservas' : 'si'),
              libreDespues < 0
                  ? 'Te faltarían ' + f(-libreDespues) + ' al mes.'
                  : 'Te quedarían ' + f(libreDespues) + ' al mes (' + p(r.pct_libre) + ' de tus ingresos). Conviene que sobre al menos un ' + MARGEN_MES + ' %.');
        regla('precio', 'El precio, frente a lo que ganas en un año',
              r.pct_precio_año <= PRECIO_SOBRE_AÑO ? 'si' : 'reservas',
              'Cuesta el ' + p(r.pct_precio_año) + ' de tus ingresos netos de un año. Prudente: no más de la mitad.');
    }
    regla('capital', 'El dinero para pagarlo',
          quedaCapital < 0 ? 'no' : (quedaCapital < colchon ? 'reservas' : 'si'),
          quedaCapital < 0
              ? 'Necesitas ' + f(necesario) + (contado ? ' (precio y gastos)' : ' de entrada y gastos') + ' y tienes ' + f(capital) + '.'
              : 'Pagas ' + f(necesario) + ' y te quedan ' + f(quedaCapital) + '. ' +
                (quedaCapital < colchon ? 'Por debajo del colchón de 6 meses (' + f(colchon) + ').' : 'El colchón de 6 meses (' + f(colchon) + ') queda intacto.'));
    if (!contado) {
        // Cuenta la entrada entera, también la que sale de vender el viejo.
        const entradaPct = precio > 0 ? entrada / precio * 100 : 0;
        const bien = entradaPct >= ENTRADA_MIN && meses <= PLAZO_MAX;
        regla('condiciones', 'Entrada y plazo', bien ? 'si' : 'reservas',
              'Entrada del ' + p(entradaPct) + ' y ' + meses + ' meses. Lo prudente: al menos un ' + ENTRADA_MIN +
              ' % de entrada y no más de ' + PLAZO_MAX + ' meses; cuanto más largo, más intereses y más tiempo debiendo más de lo que vale el coche.');
    }

    const nos = r.reglas.filter(function (x) { return x.estado === 'no'; });
    const reservas = r.reglas.filter(function (x) { return x.estado === 'reservas'; });
    if (nos.length) {
        r.veredicto = {clase: 'rojo', titulo: 'No te lo puedes permitir', motivo: nos[0].detalle, fallan: nos.map(function (x) { return x.titulo; })};
    } else if (reservas.length) {
        r.veredicto = {clase: 'ambar', titulo: 'Sí, pero con reservas', motivo: reservas[0].detalle, fallan: reservas.map(function (x) { return x.titulo; })};
    } else {
        r.veredicto = {clase: 'verde', titulo: 'Sí, con los números en verde', motivo: 'Cumple todas las reglas.', fallan: []};
    }
    return r;
}

/* El precio más alto que pasa las reglas.
 *
 * `usoPara(precio)`: el coste de uso a ese precio (el seguro sube con él).
 * Devuelve el recomendado (todo en verde) y el límite (nada en rojo). Busca
 * por bisección: todas las reglas empeoran al subir el precio.
 */
function precioMaximo(e, usoPara) {
    function cumple(precio, exigencia) {
        const r = evaluar(Object.assign({}, e, {precio: precio, uso_mensual: usoPara(precio)}));
        return r.reglas.every(function (x) { return exigencia === 'verde' ? x.estado === 'si' : x.estado !== 'no'; });
    }
    function buscar(exigencia) {
        let lo = 0, hi = 400000;
        if (!cumple(1000, exigencia)) return 0;
        for (let i = 0; i < 45; i++) {
            const mid = (lo + hi) / 2;
            if (cumple(mid, exigencia)) lo = mid; else hi = mid;
        }
        return Math.floor(lo / 100) * 100;
    }
    return {recomendado: buscar('verde'), limite: buscar('limite')};
}

function segmento(precio) {
    for (let i = 0; i < SEGMENTOS.length; i++) if (precio <= SEGMENTOS[i].hasta) return i;
    return SEGMENTOS.length - 1;
}

/* Meses de ahorro para reunir lo que falta. null si no se ahorra. */
function mesesHastaReunir(falta, ahorro) {
    if (falta <= 0) return 0;
    if (!(ahorro > 0)) return null;
    return Math.ceil(falta / ahorro);
}

/* Qué motor encaja con cómo usas el coche. */
function consejoPropulsion(kmAnuales, cargaEnCasa) {
    if (kmAnuales < 8000) return {rec: 'gasolina', texto: 'Con menos de 8.000 km al año, un gasolina (o un híbrido si es sobre todo ciudad): el ahorro de un eléctrico o un diésel no llega a amortizar lo que cuestan de más.'};
    if (kmAnuales <= 20000) return cargaEnCasa
        ? {rec: 'electrico', texto: 'Entre 8.000 y 20.000 km y cargando en casa, un eléctrico o un híbrido enchufable: la energía te cuesta una cuarta parte.'}
        : {rec: 'hibrido', texto: 'Entre 8.000 y 20.000 km sin cargador en casa, un híbrido: gasta poco en ciudad y no depende de enchufes.'};
    return cargaEnCasa
        ? {rec: 'electrico', texto: 'Con más de 20.000 km y cargando en casa, el eléctrico es el que más ahorra: cuanto más andas, antes compensa.'}
        : {rec: 'diesel', texto: 'Con más de 20.000 km y mucha carretera sin poder cargar en casa, un diésel o un híbrido siguen siendo lo más económico por km.'};
}

/* Consejos para ESTE caso, de más a menos importante. */
function consejos(e, r) {
    const c = [];
    const f = function (v) { return Math.round(v).toLocaleString('es-ES') + ' €'; };
    if (!e.contado && r.capital - r.necesario - r.colchon >= r.prestamo && r.prestamo > 0) {
        c.push('Tienes para pagarlo al contado y seguir con el colchón intacto: te ahorrarías ' + f(r.intereses) + ' de intereses.');
    }
    if (!e.contado && num(e.tin) >= 8) {
        c.push('Un ' + String(num(e.tin)).replace('.', ',') + ' % de interés es caro. Compara con un préstamo personal de tu banco y mira siempre la TAE, no solo el TIN: comisiones y seguros vinculados la disparan.');
    }
    if (!e.contado && r.meses > PLAZO_MAX) {
        c.push('A ' + r.meses + ' meses pagas mucho más de intereses y pasarás años debiendo más de lo que vale el coche. Si la cuota solo cabe alargando el plazo, el coche es demasiado caro.');
    }
    if (!e.contado && num(e.entrada_pct) < ENTRADA_MIN) {
        c.push('Con menos de un ' + ENTRADA_MIN + ' % de entrada, el primer año el coche vale menos de lo que debes: si hay que venderlo, pierdes dinero.');
    }
    if (r.pct_coches > COCHES_RECOMENDADO) {
        c.push('Tus coches se llevan el ' + Math.round(r.pct_coches) + ' % de tus ingresos. Un coche más barato, uno seminuevo o vender el que menos uses es donde más se nota.');
    }
    if (r.queda_capital >= 0 && r.queda_capital < r.colchon) {
        c.push('Comprándolo te quedas por debajo del colchón de 6 meses. Una avería o un mes sin ingresos te obligaría a endeudarte: mejor más entrada ahorrando unos meses, o un coche más barato.');
    }
    c.push('Un seminuevo de 2 o 3 años ha perdido ya un 30-40 % de su valor: es la forma más barata de tener un coche casi nuevo.');
    c.push('Negocia el precio del coche antes de hablar de financiación. Los descuentos «con financiación de la marca» suelen obligar a un seguro o a una permanencia que se comen el ahorro.');
    c.push('Cuenta el coste total, no la cuota: seguro, energía, taller, impuestos y la pérdida de valor. Un coche barato de comprar puede ser caro de tener.');
    c.push('¿Leasing o renting? Compáralo en la pestaña «Compra vs leasing» con el coste real al final.');
    return c;
}

global.PrestamoVehiculo = {
    PROPULSIONES: PROPULSIONES,
    SEGMENTOS: SEGMENTOS,
    LIMITE_DEUDA: LIMITE_DEUDA,
    COCHES_RECOMENDADO: COCHES_RECOMENDADO,
    cuotaMensual: cuotaMensual,
    sugerenciasUso: sugerenciasUso,
    usoMensual: usoMensual,
    evaluar: evaluar,
    precioMaximo: precioMaximo,
    segmento: segmento,
    mesesHastaReunir: mesesHastaReunir,
    consejoPropulsion: consejoPropulsion,
    consejos: consejos,
};

})(typeof window !== 'undefined' ? window : globalThis);
