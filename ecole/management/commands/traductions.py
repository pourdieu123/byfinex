"""Gestion des traductions sans les outils GNU gettext (absents sous Windows).

    python manage.py traductions extraire   # met à jour locale/<langue>/LC_MESSAGES/django.po
    python manage.py traductions compiler   # génère les fichiers .mo lus par Django

Les textes à traduire sont repérés dans :
  - les gabarits : {% translate "…" %} et {% blocktranslate %}…{% endblocktranslate %}
  - le code Python : _("…") (gettext / gettext_lazy importés sous le nom « _ »)
Les traductions déjà saisies dans le .po sont conservées lors d'une nouvelle extraction.
"""

import ast
import re
import struct
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

DOSSIERS = ['ecole', 'pedagogie', 'config']
RE_TRANSLATE = re.compile(r"""{%\s*(?:translate|trans)\s+(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')""")
RE_BLOCK = re.compile(r"{%\s*blocktranslate[^%]*%}(.*?){%\s*endblocktranslate\s*%}", re.S)
RE_VAR = re.compile(r"{{\s*(\w+)\s*}}")
RE_UNDERSCORE_TPL = re.compile(r'_\("([^"]*)"\)')


def _echapper(s):
    return s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')


def _desechapper(s):
    return ast.literal_eval('"' + s + '"')


def extraire_textes():
    """{msgid: [emplacements]} dans l'ordre d'apparition."""
    textes = {}

    def ajouter(msgid, ou):
        if msgid.strip():
            textes.setdefault(msgid, []).append(ou)

    base = Path(settings.BASE_DIR)
    for dossier in DOSSIERS:
        for f in sorted((base / dossier).rglob('*.html')):
            contenu = f.read_text(encoding='utf-8')
            for m in RE_TRANSLATE.finditer(contenu):
                brut = m.group(1) if m.group(1) is not None else m.group(2)
                ajouter(brut.replace('\\"', '"').replace("\\'", "'"), f.relative_to(base).as_posix())
            for m in RE_UNDERSCORE_TPL.finditer(contenu):
                ajouter(m.group(1), f.relative_to(base).as_posix())
            for m in RE_BLOCK.finditer(contenu):
                msgid = RE_VAR.sub(lambda v: f'%({v.group(1)})s', m.group(1).replace('%', '%%'))
                ajouter(msgid, f.relative_to(base).as_posix())
        for f in sorted((base / dossier).rglob('*.py')):
            if 'migrations' in f.parts:
                continue
            arbre = ast.parse(f.read_text(encoding='utf-8'))
            for noeud in ast.walk(arbre):
                if (isinstance(noeud, ast.Call) and isinstance(noeud.func, ast.Name) and noeud.func.id in ('_', 'gettext_lazy')
                        and noeud.args and isinstance(noeud.args[0], ast.Constant) and isinstance(noeud.args[0].value, str)):
                    ajouter(noeud.args[0].value, f'{f.relative_to(base).as_posix()}:{noeud.lineno}')
    return textes


def lire_po(chemin):
    """{msgid: msgstr} (le po est écrit par ce programme : une ligne par champ, ou des suites de lignes « "…" »)."""
    if not chemin.exists():
        return {}
    entrees, courant, champ = {}, {}, None
    for ligne in chemin.read_text(encoding='utf-8').splitlines() + ['']:
        ligne = ligne.strip()
        if ligne.startswith('msgid '):
            if 'msgid' in courant:
                entrees[courant['msgid']] = courant.get('msgstr', '')
            courant, champ = {'msgid': _desechapper(ligne[7:-1])}, 'msgid'
        elif ligne.startswith('msgstr '):
            courant['msgstr'], champ = _desechapper(ligne[8:-1]), 'msgstr'
        elif ligne.startswith('"') and champ:
            courant[champ] += _desechapper(ligne[1:-1])
        elif not ligne and 'msgid' in courant:
            entrees[courant['msgid']] = courant.get('msgstr', '')
            courant, champ = {}, None
    entrees.pop('', None)
    return entrees


def ecrire_po(chemin, langue, textes, existantes):
    lignes = [
        '# Traductions de l\'application (source : français).',
        '# Modifier les lignes msgstr puis lancer : python manage.py traductions compiler',
        'msgid ""', 'msgstr ""',
        '"Content-Type: text/plain; charset=UTF-8\\n"',
        f'"Language: {langue}\\n"',
        '"Plural-Forms: nplurals=2; plural=(n != 1);\\n"', '',
    ]
    for msgid, emplacements in textes.items():
        lignes.append('#: ' + ' '.join(sorted(set(emplacements))[:4]))
        lignes.append(f'msgid "{_echapper(msgid)}"')
        lignes.append(f'msgstr "{_echapper(existantes.get(msgid, ""))}"')
        lignes.append('')
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text('\n'.join(lignes), encoding='utf-8')


def ecrire_mo(chemin, langue, entrees):
    """Format .mo de GNU gettext (petit-boutiste)."""
    entete = f'Content-Type: text/plain; charset=UTF-8\nLanguage: {langue}\nPlural-Forms: nplurals=2; plural=(n != 1);\n'
    paires = [('', entete)] + sorted((k, v) for k, v in entrees.items() if v)
    ids = [k.encode('utf-8') for k, _ in paires]
    strs = [v.encode('utf-8') for _, v in paires]
    n = len(paires)
    debut_ids = 7 * 4
    debut_strs = debut_ids + n * 8
    donnees = debut_strs + n * 8
    table_ids, table_strs, blob = [], [], b''
    for s in ids:
        table_ids.append((len(s), donnees + len(blob)))
        blob += s + b'\0'
    for s in strs:
        table_strs.append((len(s), donnees + len(blob)))
        blob += s + b'\0'
    contenu = struct.pack('<7I', 0x950412de, 0, n, debut_ids, debut_strs, 0, 0)
    contenu += b''.join(struct.pack('<2I', *t) for t in table_ids)
    contenu += b''.join(struct.pack('<2I', *t) for t in table_strs)
    chemin.write_bytes(contenu + blob)


class Command(BaseCommand):
    help = 'Extrait les textes à traduire (extraire) ou compile les fichiers .po en .mo (compiler).'

    def add_arguments(self, parser):
        parser.add_argument('action', choices=['extraire', 'compiler'])

    def handle(self, action, **options):
        langues = [code for code, _ in settings.LANGUAGES if code != settings.LANGUAGE_CODE]
        dossier = Path(settings.LOCALE_PATHS[0])
        for langue in langues:
            po = dossier / langue / 'LC_MESSAGES' / 'django.po'
            if action == 'extraire':
                textes = extraire_textes()
                existantes = lire_po(po)
                ecrire_po(po, langue, textes, existantes)
                manquantes = sum(1 for t in textes if not existantes.get(t))
                self.stdout.write(f'{langue} : {len(textes)} textes, {manquantes} sans traduction -> {po}')
            else:
                if not po.exists():
                    raise CommandError(f'{po} introuvable : lancez d\'abord « traductions extraire ».')
                entrees = lire_po(po)
                ecrire_mo(po.with_suffix('.mo'), langue, entrees)
                self.stdout.write(f'{langue} : {sum(1 for v in entrees.values() if v)} traductions compilées')
