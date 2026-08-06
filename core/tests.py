import re
from pathlib import Path

from django.conf import settings
from django.test import TestCase

RAIZ_PLANTILLAS = Path(settings.BASE_DIR) / 'templates'

# {# … #} solo funciona en UNA línea: si el comentario abarca varias, Django no
# lo reconoce y lo escupe tal cual al HTML, a la vista del usuario. Para varias
# líneas hay que usar {% comment %}.
COMENTARIO_CORTO = re.compile(r'\{#.*?#\}', re.S)


class PlantillasTests(TestCase):

    def test_ningun_comentario_corto_abarca_varias_lineas(self):
        culpables = []
        for ruta in sorted(RAIZ_PLANTILLAS.rglob('*.html')):
            for encontrado in COMENTARIO_CORTO.finditer(ruta.read_text(encoding='utf-8')):
                if '\n' in encontrado.group(0):
                    relativa = ruta.relative_to(RAIZ_PLANTILLAS)
                    culpables.append(f'{relativa}: {encontrado.group(0)[:60]!r}…')

        self.assertEqual(culpables, [], msg=(
            'Estos comentarios {# … #} ocupan varias líneas y se renderizarían '
            'como texto visible. Usa {% comment %}…{% endcomment %}:\n  '
            + '\n  '.join(culpables)
        ))
