
import json
from django.contrib.auth import get_user_model
from finanzas.models import (
    RegistroMensual,
    CuentaBancaria,
    SaldoMensualCuenta,
    TarjetaCredito,
    SaldoMensualTarjeta
)

ruta_json = "/home/adrian/Descargas/registro_mensual_dump.json"

with open(ruta_json, "r") as f:
    data = json.load(f)

User = get_user_model()

for item in data:
    try:
        usuario = User.objects.get(username=item["usuario"])
    except User.DoesNotExist:
        print(f"❌ Usuario '{item['usuario']}' no encontrado. Saltando.")
        continue

    # Crear o recuperar el RegistroMensual
    registro, _ = RegistroMensual.objects.get_or_create(
        usuario=usuario,
        anio=item["anio"],
        mes=item["mes"]
    )

    # Asociar cuentas bancarias
    for nombre_cuenta, saldo in item.get("detalle_cuentas", {}).items():
        cuenta, _ = CuentaBancaria.objects.get_or_create(
            usuario=usuario,
            nombre__iexact=nombre_cuenta,
            defaults={"nombre": nombre_cuenta, "activa": True}
        )
        SaldoMensualCuenta.objects.update_or_create(
            cuenta=cuenta,
            registro=registro,
            defaults={"saldo": saldo}
        )

    # Asociar tarjetas de crédito
    for nombre_tarjeta, saldo in item.get("detalle_tarjetas", {}).items():
        tarjeta, _ = TarjetaCredito.objects.get_or_create(
            usuario=usuario,
            nombre__iexact=nombre_tarjeta,
            defaults={"nombre": nombre_tarjeta, "activa": True}
        )
        SaldoMensualTarjeta.objects.update_or_create(
            tarjeta=tarjeta,
            registro=registro,
            defaults={"saldo": abs(saldo)}  # usamos valor absoluto
        )

print("✅ Importación completa con asociación a cuentas y tarjetas.")
