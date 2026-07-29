"""Construye un resumen financiero compacto del hogar para inyectar como
contexto en las conversaciones con la IA.

Regla de seguridad: toda consulta debe estar filtrada por hogar (directamente,
o vía usuario__userprofile__hogar), para que un usuario nunca reciba datos de
otro hogar.
"""
from finanzas.models import (
    CuentaBancaria, TarjetaCredito, PrestamoSimple,
    Inversion, FuenteIngreso, CategoriaGasto, PartidaGasto,
    FondoFamiliar, Propiedad,
)


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

    categorias = CategoriaGasto.objects.filter(hogar=hogar, activo=True)
    for categoria in categorias:
        partidas = PartidaGasto.objects.filter(categoria=categoria, activo=True)
        if partidas.exists():
            total_categoria = sum(p.importe_mensual for p in partidas)
            partes.append(
                f"- Gastos '{categoria.nombre}' ({categoria.get_tipo_display()}): "
                f"{partidas.count()} partidas, ~{total_categoria}/mes."
            )

    fondos = FondoFamiliar.objects.filter(hogar=hogar, activo=True)
    for fondo in fondos:
        extra = f" (cartera ~{fondo.valor_cartera})" if fondo.tipo_fondo == 'inversion' else ""
        partes.append(f"- Fondo '{fondo.nombre}' ({fondo.get_tipo_fondo_display()}){extra}.")

    propiedades = Propiedad.objects.filter(hogar=hogar, activo=True)
    for propiedad in propiedades:
        partes.append(
            f"- Propiedad '{propiedad.nombre}': valor actual {propiedad.valor_actual}, "
            f"deuda hipotecaria {propiedad.deuda_hipotecaria}, "
            f"patrimonio neto {propiedad.patrimonio_neto}."
        )

    if len(partes) == 1:
        partes.append("- Todavía no hay datos financieros registrados en este hogar.")

    return '\n'.join(partes)
