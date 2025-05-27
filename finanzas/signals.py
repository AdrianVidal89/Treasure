from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone
from .models import ValorActualInversion, HistorialValorInversion
from django.db.models import Q

@receiver(post_save, sender=ValorActualInversion)
def guardar_historial_valor(sender, instance, created, **kwargs):
    from django.utils import timezone
    from .models import HistorialValorInversion

    HistorialValorInversion.objects.update_or_create(
        inversion=instance.inversion,
        fecha=timezone.now().date(),
        defaults={
            'valor_unitario': instance.valor_unitario,
            'cantidad_activos': instance.inversion.cantidad_actual,
            'fuente': instance.fuente
        }
    )
