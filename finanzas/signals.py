from django.db.models import Q
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.utils import timezone
from .models import ValorActualInversion, HistorialValorInversion, MovimientoInversion


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