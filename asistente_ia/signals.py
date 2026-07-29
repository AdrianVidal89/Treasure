from django.db.models.signals import post_save
from django.dispatch import receiver

from core.models import Hogar


@receiver(post_save, sender=Hogar)
def crear_agente_predeterminado_para_hogar_nuevo(sender, instance, created, **kwargs):
    if not created:
        return
    from .models import AgenteIA
    from .seeds import crear_agente_predeterminado
    crear_agente_predeterminado(instance, AgenteIA)
