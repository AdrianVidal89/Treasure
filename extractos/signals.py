from django.db.models.signals import pre_delete
from django.dispatch import receiver

from .models import MovimientoBancario


@receiver(pre_delete, sender=MovimientoBancario)
def borrar_reembolsos_en_efectivo(sender, instance, **kwargs):
    """Al borrar un gasto se van con él los reembolsos en efectivo apuntados a mano.

    Solo existían para describir ese gasto: sin él, los 30 € que te dio Juan en
    mano se quedarían sueltos como un ingreso que nunca fue. Los que vienen del
    banco —un Bizum— sí se quedan: son un apunte real, y vuelven a contar como
    lo que diga su categoría.
    """
    MovimientoBancario.objects.filter(reembolsa=instance, manual=True).delete()
