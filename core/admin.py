from django.contrib import admin
from django.contrib.auth.models import User
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from .models import Hogar, UserProfile


@admin.register(Hogar)
class HogarAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'moneda_principal', 'creado_por', 'fecha_creacion')
    search_fields = ('nombre',)


class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    verbose_name_plural = 'Perfil'
    fields = ('hogar', 'rol', 'idioma', 'moneda', 'inflacion_referencia',
              'porcentaje_max_endeudamiento', 'permitir_apis_externas',
              'mostrar_alertas', 'recibir_emails')


class UserAdmin(BaseUserAdmin):
    inlines = (UserProfileInline,)


admin.site.unregister(User)
admin.site.register(User, UserAdmin)