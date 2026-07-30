"""Construye un resumen financiero compacto del hogar para inyectar como
contexto (el "briefing") en las conversaciones con la IA.

Regla de seguridad: toda consulta debe estar filtrada por hogar (directamente,
o vía usuario__userprofile__hogar), para que un usuario nunca reciba datos de
otro hogar.

Es un resumen (cifras y recuentos), no un volcado fila a fila. Aun así, en
hogares con muchas categorías/fondos/propiedades se trunca explícitamente:
el modelo siempre puede pedir el detalle exacto con `query`/`get_item`.
"""
from finanzas.models import (
    CuentaBancaria, TarjetaCredito, PrestamoSimple,
    Inversion, FuenteIngreso, CategoriaGasto, PartidaGasto,
    FondoFamiliar, Propiedad,
)

MAX_CATEGORIAS_DETALLADAS = 12
MAX_FONDOS_DETALLADOS = 12
MAX_PROPIEDADES_DETALLADAS = 12


def construir_contexto_financiero(hogar):
    partes = [
        f"Contexto financiero del hogar '{hogar.nombre}' "
        f"(moneda principal: {hogar.get_moneda_principal_display()}):"
    ]

    cuentas = CuentaBancaria.objects.filter(usuario__userprofile__hogar=hogar, activa=True)
    if cuentas.exists():
        partes.append(f"- Cuentas bancarias activas: {', '.join(c.nombre for c in cuentas)}.")

    tarjetas = TarjetaCredito.objects.filter(usuario__userprofile__hogar=hogar, activa=True)
    if tarjetas.exists():
        partes.append(f"- Tarjetas de crédito: {', '.join(t.nombre for t in tarjetas)}.")

    prestamos = PrestamoSimple.objects.filter(usuario__userprofile__hogar=hogar)
    if prestamos.exists():
        total_pendiente = sum(p.total_pendiente for p in prestamos)
        partes.append(f"- Préstamos: {prestamos.count()}, pendiente total aprox. {total_pendiente}.")

    inversiones = Inversion.objects.filter(usuario__userprofile__hogar=hogar).select_related('valor_actual')
    if inversiones.exists():
        total_valor = sum((inv.valor_total_actual or 0) for inv in inversiones)
        partes.append(f"- Inversiones: {inversiones.count()} activos, valor total aprox. {total_valor}.")

    fuentes = FuenteIngreso.objects.filter(hogar=hogar, activo=True)
    if fuentes.exists():
        total_mensual = sum(f.importe_mensual_ponderado for f in fuentes)
        partes.append(f"- Fuentes de ingreso activas: {fuentes.count()}, ~{total_mensual}/mes ponderado.")

    categorias = list(CategoriaGasto.objects.filter(hogar=hogar, activo=True))
    for categoria in categorias[:MAX_CATEGORIAS_DETALLADAS]:
        partidas = PartidaGasto.objects.filter(categoria=categoria, activo=True)
        if partidas.exists():
            total_categoria = sum(p.importe_mensual for p in partidas)
            partes.append(
                f"- Gastos '{categoria.nombre}' ({categoria.get_tipo_display()}): "
                f"{partidas.count()} partidas, ~{total_categoria}/mes."
            )
    if len(categorias) > MAX_CATEGORIAS_DETALLADAS:
        restantes = len(categorias) - MAX_CATEGORIAS_DETALLADAS
        partes.append(
            f"- … y {restantes} categorías de gasto más (truncado; usa query sobre "
            f"'categorias_gasto'/'partidas_gasto' para verlas)."
        )

    fondos = list(FondoFamiliar.objects.filter(hogar=hogar, activo=True))
    for fondo in fondos[:MAX_FONDOS_DETALLADOS]:
        extra = f" (cartera ~{fondo.valor_cartera})" if fondo.tipo_fondo == 'inversion' else ""
        partes.append(f"- Fondo '{fondo.nombre}' ({fondo.get_tipo_fondo_display()}){extra}.")
    if len(fondos) > MAX_FONDOS_DETALLADOS:
        restantes = len(fondos) - MAX_FONDOS_DETALLADOS
        partes.append(f"- … y {restantes} fondos más (truncado; usa query sobre 'fondos').")

    propiedades = list(Propiedad.objects.filter(hogar=hogar, activo=True))
    for propiedad in propiedades[:MAX_PROPIEDADES_DETALLADAS]:
        partes.append(
            f"- Propiedad '{propiedad.nombre}': valor actual {propiedad.valor_actual}, "
            f"deuda hipotecaria {propiedad.deuda_hipotecaria}, "
            f"patrimonio neto {propiedad.patrimonio_neto}."
        )
    if len(propiedades) > MAX_PROPIEDADES_DETALLADAS:
        restantes = len(propiedades) - MAX_PROPIEDADES_DETALLADAS
        partes.append(f"- … y {restantes} propiedades más (truncado; usa query sobre 'propiedades').")

    if len(partes) == 1:
        partes.append("- Todavía no hay datos financieros registrados en este hogar.")

    return '\n'.join(partes)
