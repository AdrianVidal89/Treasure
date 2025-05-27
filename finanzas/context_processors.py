from .models import ResumenInversionesMensual

def resumen_inversiones_context(request):
    if request.user.is_authenticated:
        resumen = ResumenInversionesMensual.objects.filter(
            usuario=request.user
        ).order_by('-registro__anio', '-registro__mes').first()
    else:
        resumen = None

    return {
        'resumen_inversiones': resumen
    }
