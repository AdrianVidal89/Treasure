import datetime

from django.db.models import Q
from django.db.models.signals import post_save, post_delete, pre_save, pre_delete
from django.dispatch import receiver
from django.utils import timezone
from .models import (
    ValorActualInversion, HistorialValorInversion, MovimientoInversion,
    FuenteIngreso, PartidaGasto, ReglaReparto, AjusteIngresoMensual,
)


@receiver(post_save, sender=ValorActualInversion)
def guardar_historial_valor(sender, instance, created, **kwargs):
    HistorialValorInversion.objects.update_or_create(
        inversion=instance.inversion,
        fecha=timezone.now().date(),
        defaults={
            'valor_unitario': instance.valor_unitario,
            'cantidad_activos': instance.inversion.total_activos,
            'fuente': instance.fuente
        }
    )


@receiver(post_save, sender=MovimientoInversion)
def sincronizar_cantidad_tras_movimiento(sender, instance, **kwargs):
    instance.inversion.sincronizar_cantidad()


@receiver(post_delete, sender=MovimientoInversion)
def sincronizar_cantidad_tras_borrado(sender, instance, **kwargs):
    instance.inversion.sincronizar_cantidad()

# ─── Cierre de meses ─────────────────────────────────────────────────────────
# Evolución es un registro histórico: lo que pasó en julio no puede cambiar
# porque hoy subas el sueldo o des de baja un gasto. Antes de que la
# configuración cambie, se congelan los meses ya cerrados que aún no lo
# estuvieran, de forma que la foto se hace con los valores de ANTES del cambio.
# Ver `cierres.py`.

def _congelar_antes_de_cambiar(hogar):
    if hogar is None:
        return
    from .cierres import congelar_meses_cerrados
    hoy = datetime.date.today()
    congelar_meses_cerrados(hogar, hoy.year)
    # Enero: el mes en curso es el 1, así que lo que se acaba de cerrar es
    # diciembre del año anterior.
    if hoy.month == 1:
        congelar_meses_cerrados(hogar, hoy.year - 1)


@receiver(pre_save, sender=FuenteIngreso)
@receiver(pre_delete, sender=FuenteIngreso)
@receiver(pre_save, sender=PartidaGasto)
@receiver(pre_delete, sender=PartidaGasto)
@receiver(pre_save, sender=ReglaReparto)
@receiver(pre_delete, sender=ReglaReparto)
def congelar_meses_cerrados_antes_del_cambio(sender, instance, **kwargs):
    _congelar_antes_de_cambiar(getattr(instance, 'hogar', None))


@receiver(post_save, sender=AjusteIngresoMensual)
@receiver(post_delete, sender=AjusteIngresoMensual)
def rehacer_cierre_del_mes_ajustado(sender, instance, **kwargs):
    """Un ajuste de ingreso SÍ es un dato histórico: dice lo que se cobró
    realmente ese mes. Si se corrige un mes ya cerrado, su foto se rehace para
    recoger la corrección — es un cambio explícito sobre ese mes, no el
    arrastre de un cambio de configuración."""
    from .cierres import congelar_mes
    hogar = getattr(instance.fuente, 'hogar', None)
    if hogar is None:
        return
    congelar_mes(hogar, instance.año, instance.mes, forzar=True)
