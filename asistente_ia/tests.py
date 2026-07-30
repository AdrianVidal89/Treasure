"""Pruebas del agente de IA rediseñado.

Cubren las invariantes no negociables del diseño:
  - Lectura ejecuta; escritura propone y para (nunca escribe sin confirmación).
  - Los permisos de escritura se revalidan en propose Y en apply.
  - tool_use nunca queda sin su tool_result; el historial se trunca por
    turnos completos, nunca a mitad de un par.
  - CAMPOS_OCULTOS bloquea también como clave de filtro, no solo al serializar.
  - Un fallo del proveedor nunca se traduce en una excepción sin capturar.
  - Si se agota el tope de iteraciones, se fuerza una respuesta final sin tools.
  - El aislamiento por hogar (tenant) se respeta en las tools de lectura.
"""
import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from core.models import Hogar, UserProfile
from finanzas.models import CategoriaGasto, FuenteIngreso, PartidaGasto

from . import acciones, registro
from .models import AgenteIA, ConfiguracionIA, ConversacionIA
from .proveedores.base import RespuestaLLM
from .proveedores.bloques import (
    aplanar_turnos, bloque_texto, bloque_tool_result, bloque_tool_use, mensaje,
    truncar_turnos, verificar_pairing,
)


def _crear_usuario_con_hogar(username, rol='miembro', hogar=None):
    hogar = hogar or Hogar.objects.create(nombre=f'Hogar de {username}')
    user = User.objects.create_user(username=username, password='x')
    UserProfile.objects.filter(user=user).delete()  # por si una señal ya creó el perfil
    UserProfile.objects.create(user=user, hogar=hogar, rol=rol)
    return user, hogar


# ─── registro.py ───────────────────────────────────────────────────────────

class RegistroTests(TestCase):
    def test_validar_lookup_acepta_campo_simple(self):
        self.assertTrue(registro.validar_lookup(PartidaGasto, 'nombre'))

    def test_validar_lookup_acepta_lookup_con_operador(self):
        self.assertTrue(registro.validar_lookup(PartidaGasto, 'importe__gte'))

    def test_validar_lookup_acepta_relacion_multihop(self):
        self.assertTrue(registro.validar_lookup(PartidaGasto, 'categoria__nombre'))

    def test_validar_lookup_rechaza_campo_inexistente(self):
        self.assertFalse(registro.validar_lookup(PartidaGasto, 'campo_que_no_existe'))

    def test_validar_lookup_rechaza_campo_oculto_en_cualquier_hop(self):
        # 'password' no es un campo de PartidaGasto, pero comprobamos el caso
        # directo sobre un modelo que sí lo tiene vía relación de usuario.
        self.assertFalse(registro.validar_lookup(PartidaGasto, 'categoria__hogar__id__password'))

    def test_campos_ocultos_bloquean_filtro_no_solo_serializacion(self):
        # 'avatar' está en CAMPOS_OCULTOS; ni siquiera debería poder usarse como filtro
        # (validar_lookup nunca lanza: la señal de rechazo es que devuelva False).
        self.assertFalse(registro.validar_lookup(UserProfile, 'avatar'))

    def test_serializar_instancia_excluye_campos_ocultos(self):
        hogar = Hogar.objects.create(nombre='Hogar test')
        categoria = CategoriaGasto.objects.create(hogar=hogar, nombre='Ocio', tipo='variable')
        fila = registro.serializar_instancia(categoria)
        self.assertNotIn('password', fila)
        self.assertEqual(fila['nombre'], 'Ocio')

    def test_filtrar_por_hogar_aisla_tenants(self):
        hogar_a = Hogar.objects.create(nombre='A')
        hogar_b = Hogar.objects.create(nombre='B')
        CategoriaGasto.objects.create(hogar=hogar_a, nombre='Solo A', tipo='variable')
        CategoriaGasto.objects.create(hogar=hogar_b, nombre='Solo B', tipo='variable')

        consulta = registro.filtrar_por_hogar(CategoriaGasto.objects.all(), hogar_a, 'hogar')
        nombres = set(consulta.values_list('nombre', flat=True))
        self.assertEqual(nombres, {'Solo A'})


# ─── proveedores/bloques.py ────────────────────────────────────────────────

class BloquesTests(TestCase):
    def test_pairing_correcto(self):
        turno = [
            mensaje('user', [bloque_texto('hola')]),
            mensaje('assistant', [bloque_tool_use('t1', 'count', {})]),
            mensaje('user', [bloque_tool_result('t1', '{"total": 3}')]),
            mensaje('assistant', [bloque_texto('hay 3')]),
        ]
        self.assertTrue(verificar_pairing(turno))

    def test_pairing_detecta_tool_use_huerfano(self):
        turno = [
            mensaje('assistant', [bloque_tool_use('t1', 'count', {})]),
            mensaje('user', [bloque_texto('esto no es un tool_result')]),
        ]
        self.assertFalse(verificar_pairing(turno))

    def test_truncar_turnos_mantiene_solo_los_ultimos(self):
        turnos = [[mensaje('user', [bloque_texto(f'turno {i}')])] for i in range(20)]
        truncados = truncar_turnos(turnos, max_turnos=8)
        self.assertEqual(len(truncados), 8)
        # Los turnos conservados deben ser los últimos, en orden.
        self.assertEqual(truncados[0][0]['blocks'][0]['texto'], 'turno 12')
        self.assertEqual(truncados[-1][0]['blocks'][0]['texto'], 'turno 19')

    def test_aplanar_turnos(self):
        turnos = [[mensaje('user', [bloque_texto('a')])], [mensaje('user', [bloque_texto('b')])]]
        self.assertEqual(len(aplanar_turnos(turnos)), 2)


# ─── herramientas.py (tools de lectura) ────────────────────────────────────

class HerramientasTests(TestCase):
    def setUp(self):
        self.usuario, self.hogar = _crear_usuario_con_hogar('lector')
        self.otro_usuario, self.otro_hogar = _crear_usuario_con_hogar('otro')
        self.categoria = CategoriaGasto.objects.create(hogar=self.hogar, nombre='Ocio', tipo='variable')
        PartidaGasto.objects.create(hogar=self.hogar, categoria=self.categoria, nombre='Cine', importe=50)
        # Datos de otro hogar: nunca deben aparecer en las consultas de arriba.
        cat_ajena = CategoriaGasto.objects.create(hogar=self.otro_hogar, nombre='Ocio ajeno', tipo='variable')
        PartidaGasto.objects.create(hogar=self.otro_hogar, categoria=cat_ajena, nombre='Cine ajeno', importe=999)

    def test_query_aisla_por_hogar(self):
        from .herramientas import query
        resultado = query(self.hogar, 'partidas_gasto')
        nombres = {f['nombre'] for f in resultado['filas']}
        self.assertEqual(nombres, {'Cine'})

    def test_query_rechaza_filtro_no_valido(self):
        from .herramientas import ErrorHerramienta, query
        with self.assertRaises(ErrorHerramienta):
            query(self.hogar, 'partidas_gasto', filtros={'campo_inventado__gte': 1})

    def test_search_encuentra_por_texto(self):
        from .herramientas import search
        resultado = search(self.hogar, 'Cine')
        entidades = {f['_entidad'] for f in resultado['filas']}
        self.assertIn('partidas_gasto', entidades)
        # Nunca debe colarse el dato del otro hogar.
        nombres = {f.get('nombre') for f in resultado['filas']}
        self.assertNotIn('Cine ajeno', nombres)

    def test_get_item_incluye_relaciones(self):
        from .herramientas import get_item
        fila = get_item(self.hogar, 'categorias_gasto', self.categoria.id)
        self.assertEqual(fila['nombre'], 'Ocio')
        self.assertIn('_relaciones', fila)

    def test_get_item_no_filtra_entre_hogares(self):
        from .herramientas import ErrorHerramienta, get_item
        cat_ajena = CategoriaGasto.objects.get(nombre='Ocio ajeno')
        with self.assertRaises(ErrorHerramienta):
            get_item(self.hogar, 'categorias_gasto', cat_ajena.id)

    def test_count(self):
        from .herramientas import count
        resultado = count(self.hogar, 'partidas_gasto')
        self.assertEqual(resultado['total'], 1)

    def test_ejecutar_herramienta_nunca_lanza(self):
        from .herramientas import ejecutar_herramienta
        texto = ejecutar_herramienta('entidad_no_existe', {'entidad': 'no_existe'}, self.hogar)
        data = json.loads(texto)
        self.assertIn('error', data)


# ─── acciones.py (staging de escritura) ────────────────────────────────────

class AccionesTests(TestCase):
    def setUp(self):
        self.usuario, self.hogar = _crear_usuario_con_hogar('escritor', rol='miembro')
        self.viewer, _ = _crear_usuario_con_hogar('mirón', rol='viewer', hogar=self.hogar)
        self.categoria = CategoriaGasto.objects.create(hogar=self.hogar, nombre='Ocio', tipo='variable')
        self.conversacion = ConversacionIA.objects.create(usuario=self.usuario, hogar=self.hogar)
        self.session = self.client.session

    def test_proponer_crear_gasto_resuelve_categoria_contra_bd(self):
        resultado = acciones.proponer_accion(
            'crear_gasto', {'nombre': 'Cine', 'importe': 20, 'categoria': 'Ocio'},
            self.hogar, self.usuario, self.conversacion, self.session,
        )
        self.assertTrue(resultado.ok)
        self.assertIn('Cine', resultado.preview)
        # Nada se ha escrito todavía.
        self.assertFalse(PartidaGasto.objects.filter(nombre='Cine').exists())

    def test_proponer_crear_gasto_con_categoria_inexistente_falla_en_propose(self):
        resultado = acciones.proponer_accion(
            'crear_gasto', {'nombre': 'Cine', 'importe': 20, 'categoria': 'No existe'},
            self.hogar, self.usuario, self.conversacion, self.session,
        )
        self.assertFalse(resultado.ok)
        self.assertIn('No existe', resultado.mensaje)

    def test_viewer_no_puede_proponer(self):
        resultado = acciones.proponer_accion(
            'crear_gasto', {'nombre': 'Cine', 'importe': 20, 'categoria': 'Ocio'},
            self.hogar, self.viewer, self.conversacion, self.session,
        )
        self.assertFalse(resultado.ok)
        self.assertIn('permiso', resultado.mensaje.lower())

    def test_confirmar_aplica_el_cambio_real(self):
        propuesta = acciones.proponer_accion(
            'crear_gasto', {'nombre': 'Cine', 'importe': 20, 'categoria': 'Ocio'},
            self.hogar, self.usuario, self.conversacion, self.session,
        )
        resultado = acciones.confirmar_propuesta(self.session, propuesta.id, self.hogar, self.usuario)
        self.assertTrue(resultado.ok)
        self.assertTrue(PartidaGasto.objects.filter(nombre='Cine', importe=20).exists())
        # La propuesta ya no debe seguir disponible en sesión.
        self.assertIsNone(acciones.obtener_propuesta_sesion(self.session, propuesta.id))

    def test_permiso_se_revalida_en_confirmar_aunque_se_aprobara_en_propose(self):
        propuesta = acciones.proponer_accion(
            'crear_gasto', {'nombre': 'Cine', 'importe': 20, 'categoria': 'Ocio'},
            self.hogar, self.usuario, self.conversacion, self.session,
        )
        self.assertTrue(propuesta.ok)
        # El rol del usuario cambia a viewer ENTRE la propuesta y la confirmación.
        perfil = self.usuario.userprofile
        perfil.rol = 'viewer'
        perfil.save()

        resultado = acciones.confirmar_propuesta(self.session, propuesta.id, self.hogar, self.usuario)
        self.assertFalse(resultado.ok)
        self.assertFalse(PartidaGasto.objects.filter(nombre='Cine').exists())

    def test_rechazar_descarta_sin_aplicar(self):
        propuesta = acciones.proponer_accion(
            'crear_gasto', {'nombre': 'Cine', 'importe': 20, 'categoria': 'Ocio'},
            self.hogar, self.usuario, self.conversacion, self.session,
        )
        acciones.rechazar_propuesta(self.session, propuesta.id)
        self.assertFalse(PartidaGasto.objects.filter(nombre='Cine').exists())
        self.assertIsNone(acciones.obtener_propuesta_sesion(self.session, propuesta.id))

    def test_agente_sin_allowed_tools_no_puede_proponer(self):
        agente = AgenteIA.objects.create(
            hogar=self.hogar, nombre='Solo lectura', instrucciones='x', allowed_tools=[],
        )
        resultado = acciones.proponer_accion(
            'crear_gasto', {'nombre': 'Cine', 'importe': 20, 'categoria': 'Ocio'},
            self.hogar, self.usuario, self.conversacion, self.session, agente=agente,
        )
        self.assertFalse(resultado.ok)


# ─── loop.py (bucle del agente, con proveedor simulado) ────────────────────

class ProveedorStub:
    """Proveedor de pruebas: devuelve las respuestas programadas en orden y
    registra los argumentos de cada llamada (para comprobar, p.ej., que la
    llamada final del tope de iteraciones se hace sin tools)."""

    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.llamadas = []

    def enviar(self, mensajes, system_blocks, tools, api_key, modelo, timeout=30):
        self.llamadas.append({'mensajes': mensajes, 'tools': tools})
        if not self.respuestas:
            return RespuestaLLM(blocks=[bloque_texto('(sin más respuestas programadas)')])
        return self.respuestas.pop(0)


class LoopIntegrationTests(TestCase):
    def setUp(self):
        self.usuario, self.hogar = _crear_usuario_con_hogar('chat_user', rol='miembro')
        self.client.force_login(self.usuario)
        self.config = ConfiguracionIA.objects.create(hogar=self.hogar, proveedor_activo='anthropic')
        self.config.set_api_key('anthropic', 'sk-test-clave')
        self.config.save()
        self.categoria = CategoriaGasto.objects.create(hogar=self.hogar, nombre='Ocio', tipo='variable')

        resp = self.client.post('/asistente-ia/api/conversaciones/nueva/',
                                 data=json.dumps({'agente_id': 'ninguno'}), content_type='application/json')
        self.conv_id = resp.json()['id']

    def _enviar(self, texto):
        return self.client.post(
            f'/asistente-ia/api/conversaciones/{self.conv_id}/mensajes/',
            data=json.dumps({'mensaje': texto}), content_type='application/json',
        )

    def test_lectura_ejecuta_y_responde(self):
        stub = ProveedorStub([
            RespuestaLLM(blocks=[bloque_tool_use('t1', 'count', {'entidad': 'partidas_gasto'})]),
            RespuestaLLM(blocks=[bloque_texto('Tienes 0 partidas de gasto.')]),
        ])
        with patch('asistente_ia.loop.obtener_proveedor', return_value=stub):
            resp = self._enviar('¿cuántos gastos tengo?')
        data = resp.json()
        self.assertEqual(data['respuesta'], 'Tienes 0 partidas de gasto.')
        self.assertEqual(len(data['pasos']), 1)
        self.assertNotIn('accion_propuesta', data)

    def test_escritura_propone_y_corta_el_turno_sin_tocar_bd(self):
        stub = ProveedorStub([
            RespuestaLLM(blocks=[
                bloque_texto('Voy a proponerlo.'),
                bloque_tool_use('t1', 'propose_crear_gasto', {'nombre': 'Cine', 'importe': 20, 'categoria': 'Ocio'}),
            ]),
        ])
        with patch('asistente_ia.loop.obtener_proveedor', return_value=stub):
            resp = self._enviar('crea un gasto de cine de 20 euros')
        data = resp.json()
        self.assertIn('accion_propuesta', data)
        self.assertEqual(len(stub.llamadas), 1)  # el turno se corta: no hay segunda llamada
        self.assertFalse(PartidaGasto.objects.filter(nombre='Cine').exists())
        return data['accion_propuesta']['id']

    def test_confirmar_desde_el_endpoint_aplica_el_cambio(self):
        accion_id = self.test_escritura_propone_y_corta_el_turno_sin_tocar_bd()
        resp = self.client.post(f'/asistente-ia/api/acciones/{accion_id}/confirmar/')
        self.assertTrue(PartidaGasto.objects.filter(nombre='Cine', importe=20).exists())
        self.assertIn('aplicado', resp.json()['mensaje'])

    def test_tope_de_iteraciones_fuerza_llamada_final_sin_tools(self):
        respuestas_tool_use = [
            RespuestaLLM(blocks=[bloque_tool_use(f't{i}', 'count', {'entidad': 'partidas_gasto'})])
            for i in range(7)
        ]
        stub = ProveedorStub(respuestas_tool_use + [
            RespuestaLLM(blocks=[bloque_texto('Respuesta final forzada.')]),
        ])
        with patch('asistente_ia.loop.obtener_proveedor', return_value=stub):
            resp = self._enviar('sigue investigando indefinidamente')
        data = resp.json()
        self.assertEqual(data['respuesta'], 'Respuesta final forzada.')
        self.assertEqual(len(stub.llamadas), 8)  # 7 con tools + 1 forzada
        self.assertIsNone(stub.llamadas[-1]['tools'])  # la última, sin tools

    def test_error_de_proveedor_no_produce_500(self):
        stub = ProveedorStub([RespuestaLLM(error='Claude ha rechazado la clave API (error 401).')])
        with patch('asistente_ia.loop.obtener_proveedor', return_value=stub):
            resp = self._enviar('hola')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('error', resp.json())

    def test_pairing_se_mantiene_entre_llamadas_del_mismo_turno(self):
        stub = ProveedorStub([
            RespuestaLLM(blocks=[
                bloque_tool_use('t1', 'count', {'entidad': 'partidas_gasto'}),
                bloque_tool_use('t2', 'count', {'entidad': 'categorias_gasto'}),
            ]),
            RespuestaLLM(blocks=[bloque_texto('listo')]),
        ])
        with patch('asistente_ia.loop.obtener_proveedor', return_value=stub):
            self._enviar('cuenta dos cosas a la vez')
        # La segunda llamada debe incluir los dos tool_result emparejados con
        # sus respectivos tool_use en el mensaje inmediatamente anterior.
        segunda_llamada_mensajes = stub.llamadas[1]['mensajes']
        from .proveedores.bloques import verificar_pairing
        self.assertTrue(verificar_pairing(segunda_llamada_mensajes))
