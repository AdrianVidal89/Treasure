from django.conf import settings
from django.db import models
from django.utils import timezone


class ConfiguracionIA(models.Model):
    """Configuración de proveedores de IA a nivel de hogar (claves cifradas)."""

    PROVEEDOR_CHOICES = [
        ('anthropic', 'Claude (Anthropic)'),
        ('openai', 'ChatGPT (OpenAI)'),
        ('gemini', 'Gemini (Google)'),
    ]

    hogar = models.OneToOneField(
        'core.Hogar', on_delete=models.CASCADE, related_name='config_ia'
    )

    proveedor_activo = models.CharField(
        max_length=20, choices=PROVEEDOR_CHOICES, null=True, blank=True
    )

    anthropic_api_key_cifrada = models.BinaryField(null=True, blank=True)
    anthropic_modelo = models.CharField(max_length=60, default='claude-sonnet-5')

    openai_api_key_cifrada = models.BinaryField(null=True, blank=True)
    openai_modelo = models.CharField(max_length=60, default='gpt-4o')

    gemini_api_key_cifrada = models.BinaryField(null=True, blank=True)
    gemini_modelo = models.CharField(max_length=60, default='gemini-2.0-flash')

    actualizado_en = models.DateTimeField(auto_now=True)
    actualizado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )

    def __str__(self):
        return f"Configuración IA de {self.hogar.nombre}"

    def set_api_key(self, proveedor, api_key_plano):
        from .crypto import cifrar
        valor = cifrar(api_key_plano) if api_key_plano else None
        setattr(self, f'{proveedor}_api_key_cifrada', valor)

    def get_api_key(self, proveedor):
        from .crypto import descifrar
        valor = getattr(self, f'{proveedor}_api_key_cifrada')
        return descifrar(valor) if valor else None

    def get_modelo(self, proveedor):
        return getattr(self, f'{proveedor}_modelo')

    def tiene_proveedor_configurado(self, proveedor):
        return bool(getattr(self, f'{proveedor}_api_key_cifrada'))

    @property
    def esta_configurada(self):
        return bool(
            self.proveedor_activo and self.tiene_proveedor_configurado(self.proveedor_activo)
        )

    @property
    def modelo_activo(self):
        if not self.proveedor_activo:
            return ''
        return self.get_modelo(self.proveedor_activo)


class AgenteIA(models.Model):
    """Un agente personalizado (o el predeterminado) que el usuario puede usar en el chat."""

    hogar = models.ForeignKey('core.Hogar', on_delete=models.CASCADE, related_name='agentes_ia')
    nombre = models.CharField(max_length=100)
    descripcion = models.CharField(max_length=300, blank=True, default='')
    instrucciones = models.TextField(help_text='Instrucciones / system prompt para el modelo.')
    proveedor_preferido = models.CharField(
        max_length=20, choices=ConfiguracionIA.PROVEEDOR_CHOICES, null=True, blank=True,
        help_text='Si está vacío, se usa el proveedor activo del hogar.'
    )
    es_predeterminado = models.BooleanField(default=False)
    activo = models.BooleanField(default=True)
    creado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='agentes_ia_creados'
    )
    creado_en = models.DateTimeField(auto_now_add=True)
    actualizado_en = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-es_predeterminado', 'nombre']
        unique_together = ('hogar', 'nombre')

    def __str__(self):
        return self.nombre


class ConversacionIA(models.Model):
    """Historial de chat individual de un usuario con el asistente."""

    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='conversaciones_ia'
    )
    hogar = models.ForeignKey(
        'core.Hogar', on_delete=models.CASCADE, related_name='conversaciones_ia'
    )
    agente = models.ForeignKey(
        AgenteIA, on_delete=models.SET_NULL, null=True, blank=True, related_name='conversaciones'
    )
    titulo = models.CharField(max_length=150, blank=True, default='Nueva conversación')
    contexto_financiero_cache = models.TextField(blank=True, default='')
    contexto_generado_en = models.DateTimeField(null=True, blank=True)
    activa = models.BooleanField(default=True)
    creado_en = models.DateTimeField(auto_now_add=True)
    actualizado_en = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-actualizado_en']

    def __str__(self):
        return f"{self.titulo} ({self.usuario.username})"


class MensajeIA(models.Model):
    ROL_CHOICES = [
        ('user', 'Usuario'),
        ('assistant', 'Asistente'),
        ('system', 'Sistema'),
    ]

    conversacion = models.ForeignKey(
        ConversacionIA, on_delete=models.CASCADE, related_name='mensajes'
    )
    rol = models.CharField(max_length=10, choices=ROL_CHOICES)
    contenido = models.TextField()
    proveedor = models.CharField(max_length=20, blank=True, default='')
    modelo = models.CharField(max_length=60, blank=True, default='')
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['creado_en']

    def __str__(self):
        return f"[{self.rol}] {self.contenido[:50]}"


class AccionPropuestaIA(models.Model):
    """Acción que la IA propone realizar sobre datos de la app; requiere confirmación del usuario."""

    TIPO_CHOICES = [
        ('crear_gasto', 'Crear partida de gasto'),
        ('crear_categoria_gasto', 'Crear categoría de gasto'),
        ('crear_agente', 'Crear agente IA'),
        ('crear_ingreso', 'Crear fuente de ingreso'),
    ]
    ESTADO_CHOICES = [
        ('pendiente', 'Pendiente'),
        ('confirmada', 'Confirmada'),
        ('rechazada', 'Rechazada'),
        ('aplicada', 'Aplicada'),
        ('error', 'Error al aplicar'),
    ]

    conversacion = models.ForeignKey(
        ConversacionIA, on_delete=models.CASCADE, related_name='acciones_propuestas'
    )
    mensaje_origen = models.ForeignKey(
        MensajeIA, on_delete=models.CASCADE, related_name='acciones', null=True, blank=True
    )
    tipo_accion = models.CharField(max_length=40, choices=TIPO_CHOICES)
    payload = models.JSONField(help_text='Datos propuestos por la IA, validados antes de aplicar.')
    resumen_legible = models.CharField(max_length=300)
    estado = models.CharField(max_length=20, choices=ESTADO_CHOICES, default='pendiente')
    detalle_error = models.TextField(blank=True, default='')
    creado_en = models.DateTimeField(auto_now_add=True)
    resuelto_en = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-creado_en']

    def __str__(self):
        return f"{self.get_tipo_accion_display()} ({self.estado})"

    def marcar_resuelta(self, estado, detalle_error=''):
        self.estado = estado
        self.detalle_error = detalle_error
        self.resuelto_en = timezone.now()
        self.save()
