/* Renderizador Markdown mínimo para las respuestas del Asistente IA.
 *
 * Seguridad: el texto se escapa SIEMPRE antes de aplicar ninguna
 * transformación, así que el HTML que venga del modelo (o de algo que el
 * modelo haya leído de internet) se muestra como texto y nunca se ejecuta.
 * Solo se reintroducen las etiquetas que genera este propio fichero.
 */
window.renderMarkdownIA = (function () {
    "use strict";

    // Marcadores internos: caracteres de control que no aparecen en texto normal.
    var MARCA_BLOQUE = "\u0000";
    var MARCA_CODIGO = "\u0001";

    function escaparHtml(texto) {
        return String(texto)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function urlSegura(url) {
        return /^(https?:\/\/|mailto:)/i.test(url.trim());
    }

    function aplicarEnLinea(texto) {
        // Código en línea primero: su contenido no debe recibir más formato.
        var fragmentos = [];
        texto = texto.replace(/`([^`\n]+)`/g, function (_, codigo) {
            fragmentos.push("<code>" + codigo + "</code>");
            return MARCA_CODIGO + (fragmentos.length - 1) + MARCA_CODIGO;
        });

        texto = texto
            .replace(/\*\*\*([^*]+)\*\*\*/g, "<strong><em>$1</em></strong>")
            .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
            .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
            .replace(/__([^_]+)__/g, "<strong>$1</strong>")
            .replace(/~~([^~]+)~~/g, "<del>$1</del>");

        texto = texto.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, function (todo, etiqueta, url) {
            // El escapado previo convirtió los & de la URL en &amp;: se revierte
            // solo aquí, ya dentro de un atributo href controlado por nosotros.
            var limpia = url.replace(/&amp;/g, "&");
            if (!urlSegura(limpia)) return etiqueta;
            return '<a href="' + limpia + '" target="_blank" rel="noopener noreferrer">' + etiqueta + "</a>";
        });

        var reCodigo = new RegExp(MARCA_CODIGO + "(\\d+)" + MARCA_CODIGO, "g");
        return texto.replace(reCodigo, function (_, indice) {
            return fragmentos[Number(indice)];
        });
    }

    function celdasDe(linea) {
        return linea.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map(function (c) {
            return aplicarEnLinea(c.trim());
        });
    }

    function renderTabla(lineas) {
        var html = '<div class="ia-md-tabla-wrap"><table><thead><tr>';
        celdasDe(lineas[0]).forEach(function (c) { html += "<th>" + c + "</th>"; });
        html += "</tr></thead><tbody>";
        lineas.slice(2).forEach(function (linea) {
            html += "<tr>";
            celdasDe(linea).forEach(function (c) { html += "<td>" + c + "</td>"; });
            html += "</tr>";
        });
        return html + "</tbody></table></div>";
    }

    function esSeparadorTabla(linea) {
        return /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(linea) && linea.indexOf("-") !== -1;
    }

    return function renderMarkdownIA(textoOriginal) {
        var texto = escaparHtml(textoOriginal || "");

        // Bloques de código: se apartan para que nada más los toque.
        var bloques = [];
        texto = texto.replace(/```([\w+-]*)\n?([\s\S]*?)```/g, function (_, lenguaje, codigo) {
            bloques.push('<pre class="ia-md-pre"><code>' + codigo.replace(/\n$/, "") + "</code></pre>");
            return MARCA_BLOQUE + (bloques.length - 1) + MARCA_BLOQUE;
        });

        var lineas = texto.split("\n");
        var reLineaBloque = new RegExp("^" + MARCA_BLOQUE + "(\\d+)" + MARCA_BLOQUE + "$");
        var html = "";
        var i = 0;
        var parrafo = [];

        function cerrarParrafo() {
            if (parrafo.length) {
                html += "<p>" + aplicarEnLinea(parrafo.join("<br>")) + "</p>";
                parrafo = [];
            }
        }

        while (i < lineas.length) {
            var linea = lineas[i];

            if (/^\s*$/.test(linea)) { cerrarParrafo(); i++; continue; }

            // Bloque de código ya extraído
            var esBloque = reLineaBloque.exec(linea.trim());
            if (esBloque) {
                cerrarParrafo();
                html += bloques[Number(esBloque[1])];
                i++; continue;
            }

            // Titulares (empiezan en h3 para no competir con los de la página)
            var titular = linea.match(/^(#{1,6})\s+(.*)$/);
            if (titular) {
                cerrarParrafo();
                var nivel = Math.min(titular[1].length + 2, 6);
                html += "<h" + nivel + ">" + aplicarEnLinea(titular[2]) + "</h" + nivel + ">";
                i++; continue;
            }

            // Línea horizontal
            if (/^\s*([-*_])\s*\1\s*\1[\s\-*_]*$/.test(linea)) {
                cerrarParrafo();
                html += "<hr>";
                i++; continue;
            }

            // Tabla
            if (linea.indexOf("|") !== -1 && i + 1 < lineas.length && esSeparadorTabla(lineas[i + 1])) {
                cerrarParrafo();
                var filas = [linea, lineas[i + 1]];
                i += 2;
                while (i < lineas.length && lineas[i].indexOf("|") !== -1 && !/^\s*$/.test(lineas[i])) {
                    filas.push(lineas[i]); i++;
                }
                html += renderTabla(filas);
                continue;
            }

            // Cita (el escapado previo ha convertido el '>' de Markdown en '&gt;')
            if (/^\s*&gt;\s?/.test(linea)) {
                cerrarParrafo();
                var cita = [];
                while (i < lineas.length && /^\s*&gt;\s?/.test(lineas[i])) {
                    cita.push(lineas[i].replace(/^\s*&gt;\s?/, "")); i++;
                }
                html += "<blockquote>" + aplicarEnLinea(cita.join("<br>")) + "</blockquote>";
                continue;
            }

            // Listas (con un nivel de anidado)
            var esLista = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(linea);
            if (esLista) {
                cerrarParrafo();
                var ordenada = /\d/.test(esLista[2]);
                var etiqueta = ordenada ? "ol" : "ul";
                html += "<" + etiqueta + ">";
                var anidadaAbierta = false;
                while (i < lineas.length) {
                    var item = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(lineas[i]);
                    if (!item) break;
                    var anidado = item[1].length >= 2;
                    if (anidado && !anidadaAbierta) { html += "<" + etiqueta + ">"; anidadaAbierta = true; }
                    else if (!anidado && anidadaAbierta) { html += "</" + etiqueta + ">"; anidadaAbierta = false; }
                    html += "<li>" + aplicarEnLinea(item[3]) + "</li>";
                    i++;
                }
                if (anidadaAbierta) html += "</" + etiqueta + ">";
                html += "</" + etiqueta + ">";
                continue;
            }

            parrafo.push(linea);
            i++;
        }
        cerrarParrafo();

        // Bloques de código que hubieran quedado dentro de un párrafo
        var reBloqueSuelto = new RegExp(MARCA_BLOQUE + "(\\d+)" + MARCA_BLOQUE, "g");
        return html.replace(reBloqueSuelto, function (_, indice) {
            return bloques[Number(indice)];
        });
    };
})();
