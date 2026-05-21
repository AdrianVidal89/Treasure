from finanzas.models import TablaIRPF, CotizacionSS

tramos_irpf = [
    (0, 12450, 19),
    (12450, 20200, 24),
    (20200, 35200, 30),
    (35200, 60000, 37),
    (60000, 300000, 45),
    (300000, None, 47),
]

cotizaciones = [
    ('Contingencias comunes', 4.70),
    ('Desempleo (contrato indefinido)', 1.55),
    ('Formacion profesional', 0.10),
]

for anio in [2025, 2026]:
    for desde, hasta, porcentaje in tramos_irpf:
        TablaIRPF.objects.update_or_create(
            pais='ES',
            tramo_desde=desde,
            **{'a\u00f1o': anio},
            defaults={
                'tramo_hasta': hasta,
                'porcentaje': porcentaje,
            }
        )
    print(f"Tramos IRPF ES {anio} cargados.")

    for concepto, porcentaje in cotizaciones:
        CotizacionSS.objects.update_or_create(
            pais='ES',
            concepto=concepto,
            **{'a\u00f1o': anio},
            defaults={
                'porcentaje_trabajador': porcentaje,
            }
        )
    print(f"Cotizaciones SS ES {anio} cargadas.")

print("Completado.")