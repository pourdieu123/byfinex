from pathlib import Path

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _

register = template.Library()


@register.simple_tag
def static_versionne(chemin):
    """URL statique suivie de la date de modification du fichier, pour que le navigateur
    recharge la feuille de style dès qu'elle change au lieu de garder l'ancienne en cache."""
    fichier = finders.find(chemin)
    if not fichier:
        return static(chemin)
    return f'{static(chemin)}?v={int(Path(fichier).stat().st_mtime)}'


@register.filter
def fcfa(montant):
    """12500 -> '12 500 FCFA'"""
    try:
        n = int(montant)
    except (TypeError, ValueError):
        return montant
    return f'{n:,}'.replace(',', ' ') + ' FCFA'


@register.filter
def accord(nombre, suffixes):
    """Terminaison du pluriel selon la langue : {{ n|accord:"s|es" }} (français|anglais).

    Utile pour les adjectifs, qui s'accordent en français mais pas en anglais ({{ n|accord:"s|" }}).
    """
    from django.utils.translation import get_language

    fr, _sep, en = suffixes.partition('|')
    try:
        pluriel = int(nombre) > 1 if (get_language() or '').startswith('fr') else int(nombre) != 1
    except (TypeError, ValueError):
        return ''
    if not pluriel:
        return ''
    return en if (get_language() or '').startswith('en') else fr


@register.filter
def milliers(montant):
    """12500 -> '12 500'"""
    try:
        return f'{int(montant):,}'.replace(',', ' ')
    except (TypeError, ValueError):
        return montant


# --- Montant en lettres (reçus) ---

_UNITES = ['zéro', 'un', 'deux', 'trois', 'quatre', 'cinq', 'six', 'sept', 'huit', 'neuf', 'dix',
           'onze', 'douze', 'treize', 'quatorze', 'quinze', 'seize']
_DIZAINES = {2: 'vingt', 3: 'trente', 4: 'quarante', 5: 'cinquante', 6: 'soixante'}


def _moins_de_cent(n):
    if n <= 16:
        return _UNITES[n]
    if n < 20:
        return 'dix-' + _UNITES[n - 10]
    d, u = divmod(n, 10)
    if d in (7, 9):  # soixante-dix…, quatre-vingt-dix…
        base = 'soixante' if d == 7 else 'quatre-vingt'
        reste = n - (60 if d == 7 else 80)
        liaison = ' et ' if reste == 11 and d == 7 else '-'
        return base + liaison + _moins_de_cent(reste)
    if d == 8:
        return 'quatre-vingts' if u == 0 else 'quatre-vingt-' + _UNITES[u]
    if u == 0:
        return _DIZAINES[d]
    if u == 1:
        return _DIZAINES[d] + ' et un'
    return _DIZAINES[d] + '-' + _UNITES[u]


def _moins_de_mille(n):
    c, r = divmod(n, 100)
    if c == 0:
        return _moins_de_cent(r)
    tete = 'cent' if c == 1 else _UNITES[c] + ' cent' + ('s' if r == 0 else '')
    return tete if r == 0 else tete + ' ' + _moins_de_cent(r)


def nombre_en_lettres(n):
    n = int(n)
    if n < 1000:
        return _moins_de_mille(n)
    parties = []
    for valeur, nom in ((1_000_000_000, 'milliard'), (1_000_000, 'million')):
        q, n = divmod(n, valeur)
        if q:
            parties.append(f'{nombre_en_lettres(q)} {nom}' + ('s' if q > 1 else ''))
    q, n = divmod(n, 1000)
    if q:
        # « cent » et « vingt » ne prennent pas de « s » devant « mille ».
        texte = _moins_de_mille(q)
        if texte.endswith(('cents', 'vingts')):
            texte = texte[:-1]
        parties.append('mille' if q == 1 else texte + ' mille')
    if n:
        parties.append(_moins_de_mille(n))
    return ' '.join(parties)


_EN_UNITES = ['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten', 'eleven', 'twelve',
              'thirteen', 'fourteen', 'fifteen', 'sixteen', 'seventeen', 'eighteen', 'nineteen']
_EN_DIZAINES = ['', '', 'twenty', 'thirty', 'forty', 'fifty', 'sixty', 'seventy', 'eighty', 'ninety']


def number_to_words(n):
    """Montant en lettres, en anglais (pour les reçus édités en anglais)."""
    n = int(n)
    if n < 20:
        return _EN_UNITES[n]
    if n < 100:
        d, u = divmod(n, 10)
        return _EN_DIZAINES[d] + (f'-{_EN_UNITES[u]}' if u else '')
    if n < 1000:
        c, r = divmod(n, 100)
        return f'{_EN_UNITES[c]} hundred' + (f' {number_to_words(r)}' if r else '')
    for valeur, nom in ((1_000_000_000, 'billion'), (1_000_000, 'million'), (1000, 'thousand')):
        if n >= valeur:
            q, r = divmod(n, valeur)
            return f'{number_to_words(q)} {nom}' + (f' {number_to_words(r)}' if r else '')
    return str(n)


@register.filter
def en_lettres(montant):
    from django.utils.translation import get_language

    try:
        if (get_language() or '').startswith('en'):
            texte = number_to_words(montant)
            return texte[:1].upper() + texte[1:] + ' CFA francs'
        texte = nombre_en_lettres(montant)
    except (TypeError, ValueError):
        return ''
    if texte.endswith(('million', 'millions', 'milliard', 'milliards')):
        texte += ' de'
    return texte[:1].upper() + texte[1:] + ' francs CFA'


# --- Icônes (SVG en ligne, trait 2px, couleur du texte) ---

_ICONES = {
    'tableau': '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/>'
               '<rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
    'eleves': '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c0-3.6 2.9-6.5 6.5-6.5s6.5 2.9 6.5 6.5"/>'
              '<path d="M16 4.6a3.5 3.5 0 0 1 0 6.8M18.5 14c1.8.9 3 3 3 6"/>',
    'inscrire': '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c0-3.6 2.9-6.5 6.5-6.5s6.5 2.9 6.5 6.5"/>'
                '<path d="M19 8v6M16 11h6"/>',
    'encaisser': '<rect x="2.5" y="6" width="19" height="13" rx="2"/><path d="M2.5 10.5h19"/>'
                 '<path d="M16 15h2"/><path d="M6 3.5h12"/>',
    'journal': '<path d="M6 2.5h12v19l-3-2-3 2-3-2-3 2z"/><path d="M9 7.5h6M9 11.5h6M9 15.5h3.5"/>',
    'suivi': '<path d="M3 3v18h18"/><path d="m7 15 4-4 3 3 5.5-6.5"/>',
    'relances': '<path d="M6 16.5v-5.5a6 6 0 0 1 12 0v5.5l1.5 2h-15z"/><path d="M10 21.5h4"/>',
    'exports': '<path d="M12 3v12M7 10l5 5 5-5"/><path d="M4 20.5h16"/>',
    'utilisateurs': '<path d="M12 2.5 4 5.5v6c0 5 3.4 8.4 8 10 4.6-1.6 8-5 8-10v-6z"/><path d="m8.5 12 2.5 2.5 4.5-5"/>',
    'parametres': '<circle cx="12" cy="12" r="3"/><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3'
                  'M5.3 5.3l2.1 2.1M16.6 16.6l2.1 2.1M5.3 18.7l2.1-2.1M16.6 7.4l2.1-2.1"/>',
    'deconnexion': '<path d="M15 3.5h3.5a2 2 0 0 1 2 2v13a2 2 0 0 1-2 2H15"/><path d="M10 16.5 5.5 12 10 7.5M5.5 12H15"/>',
    'menu': '<path d="M3.5 6h17M3.5 12h17M3.5 18h17"/>',
    'fermer': '<path d="M6 6l12 12M18 6 6 18"/>',
    'recherche': '<circle cx="11" cy="11" r="7"/><path d="m20.5 20.5-4.5-4.5"/>',
    'telephone': '<path d="M5 3h3.5l2 5-2.5 1.5a11 11 0 0 0 6.5 6.5l1.5-2.5 5 2V19a2 2 0 0 1-2 2A17 17 0 0 1 3 5a2 2 0 0 1 2-2z"/>',
    'sms': '<path d="M4 4.5h16a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H9l-5 4v-15a1 1 0 0 1 1-1z"/><path d="M8 9.5h8M8 12.5h5"/>',
    'whatsapp': '<path d="M4 20.5 5.3 16.6A8.5 8.5 0 1 1 8.4 19.4z"/><path d="M9 8.5c0 3.5 3 6.5 6.5 6.5l1-1.8-2-1-1 .8a4.5 4.5 0 0 1-2.5-2.5l.8-1-1-2z"/>',
    'notes': '<path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1 0-5H20"/><path d="M9 7h7M9 11h5"/>',
    'professeur': '<path d="m2 9 10-5 10 5-10 5z"/><path d="M6 11.2V16c3.3 2.7 8.7 2.7 12 0v-4.8"/><path d="M22 9v5.5"/>',
    'examen': '<rect x="5" y="4" width="14" height="17.5" rx="2"/><path d="M9 4V2.5h6V4"/><path d="m9 13 2 2 4-4.5"/>',
    'horloge': '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/>',
    'accueil': '<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9v11.5h14V9"/><path d="M10 20.5v-6h4v6"/>',
    'reglages': '<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
    'pdf': '<path d="M6 2.5h8.5l4 4v15H6z"/><path d="M14.5 2.5v4h4"/><path d="M9 12.5h6M9 16.5h4"/>',
    'imprimer': '<path d="M6.5 9V3h11v6"/><rect x="3" y="9" width="18" height="8" rx="2"/><path d="M6.5 14h11v7h-11z"/>',
    'modifier': '<path d="M4 20h4L19.5 8.5l-4-4L4 16z"/><path d="m13.5 6.5 4 4"/>',
    'supprimer': '<path d="M4 7h16M9.5 7V4h5v3M6 7l1 13.5h10L18 7"/>',
    'plus': '<path d="M12 5v14M5 12h14"/>',
    'retour': '<path d="M19 12H5M11 6l-6 6 6 6"/>',
    'suivant': '<path d="m9 6 6 6-6 6"/>',
    'ecole': '<path d="M3 21h18M5 21V10l7-5.5L19 10v11"/><path d="M10 21v-5h4v5M12 9.5v1.5"/>',
    'classes': '<path d="m12 3 9 5-9 5-9-5z"/><path d="m3 13 9 5 9-5"/>',
    'cadenas': '<rect x="4.5" y="10.5" width="15" height="11" rx="2"/><path d="M8 10.5V7a4 4 0 0 1 8 0v3.5"/><path d="M12 15v2.5"/>',
    'ok': '<circle cx="12" cy="12" r="9"/><path d="m8 12.5 2.8 2.8L16.5 9"/>',
    'attention': '<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5M12 16.5v.5"/>',
    'partiel': '<circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor" stroke="none"/>',
    'calendrier': '<rect x="3.5" y="5" width="17" height="15.5" rx="2"/><path d="M3.5 10h17M8 3v4M16 3v4"/>',
    'recu': '<path d="M6 2.5h12v19l-3-2-3 2-3-2-3 2z"/><path d="M9 12.5l2 2 4-4.5"/>',
}


@register.simple_tag
def icone(nom, taille=20, classe=''):
    chemins = _ICONES.get(nom, '')
    return format_html(
        '<svg class="ico {}" width="{}" height="{}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{}</svg>',
        classe, taille, taille, mark_safe(chemins),
    )


STATUT_ICONES = {'solde': 'ok', 'partiel': 'partiel', 'aucun': 'attention'}


@register.inclusion_tag('ecole/_badge.html')
def badge(eleve):
    return {'statut': eleve.statut, 'libelle': eleve.statut_libelle, 'icone': STATUT_ICONES[eleve.statut]}


SITUATION_ICONES = {'solde': 'ok', 'a_jour': 'horloge', 'retard': 'attention'}


@register.inclusion_tag('ecole/_badge.html')
def badge_situation(eleve):
    """Badge « Soldé / À jour / En retard » selon l'échéancier de l'élève."""
    sit = eleve.situation
    return {'statut': f'sit-{sit.statut}', 'libelle': sit.statut_libelle, 'icone': SITUATION_ICONES[sit.statut]}


@register.filter
def statut_ligne(statut):
    """Classe CSS d'une ligne d'échéancier."""
    return {'payee': 'sit-solde', 'retard': 'sit-retard', 'partielle': 'sit-partielle', 'en_cours': 'sit-en_cours',
            'a_venir': 'badge-neutre'}.get(statut, '')


INSCRIPTION_STYLES = {'payee': ('sit-solde', 'ok'), 'partielle': ('sit-partielle', 'partiel'),
                      'impayee': ('sit-retard', 'attention'), 'non_definie': ('neutre', 'horloge')}


@register.inclusion_tag('ecole/_badge.html')
def badge_inscription(eleve):
    """Badge « Inscription payée / partielle / non payée »."""
    classe, icone = INSCRIPTION_STYLES[eleve.statut_inscription]
    return {'statut': classe, 'libelle': eleve.statut_inscription_libelle, 'icone': icone}
