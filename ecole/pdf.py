"""Génération des documents PDF (listes d'élèves, journal des paiements, reçus) avec ReportLab."""

from functools import lru_cache
from io import BytesIO
from itertools import groupby
from pathlib import Path
from xml.sax.saxutils import escape

from django.conf import settings
from django.utils import formats, timezone
from django.utils.translation import gettext as _, gettext_lazy
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4, A5, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    BaseDocTemplate, Flowable, Frame, KeepTogether, PageTemplate, Paragraph, Spacer, Table, TableStyle,
)

from .models import Parametres
from .templatetags.ecole_extras import en_lettres

# --- Couleurs (mêmes jetons que l'interface : noir et blanc) ---
GRIS_FONCE = colors.HexColor('#0a0a0a')
GRIS_TEXTE = colors.HexColor('#0a0a0a')
GRIS_MOYEN = colors.HexColor('#6b6b6b')
GRIS_LIGNE = colors.HexColor('#e5e5e5')
GRIS_FOND = colors.HexColor('#f5f5f5')
GRIS_CLAIR = colors.HexColor('#c4c4c4')
BLEU = colors.HexColor('#1a1a1a')  # couleur d'accent (noir By Finex)
BLEU_FONCE = colors.HexColor('#000000')
BLEU_PALE = colors.HexColor('#f2f2f2')
STATUTS = {  # (texte, fond) en niveaux de gris : soldé = noir plein, partiel = gris, impayé = contour
    'solde': (colors.HexColor('#0a0a0a'), colors.HexColor('#e5e5e5')),
    'partiel': (colors.HexColor('#404040'), colors.HexColor('#f5f5f5')),
    'aucun': (colors.HexColor('#0a0a0a'), colors.HexColor('#ffffff')),
}

# --- Polices : police du projet, sinon Segoe UI (Windows), sinon DejaVu (Linux), sinon Helvetica ---
_CANDIDATS = [
    (Path(__file__).parent / 'fonts' / 'regular.ttf', Path(__file__).parent / 'fonts' / 'bold.ttf'),
    (Path('C:/Windows/Fonts/segoeui.ttf'), Path('C:/Windows/Fonts/segoeuib.ttf')),
    (Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'), Path('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf')),
]


@lru_cache(maxsize=1)
def polices():
    for regular, bold in _CANDIDATS:
        if regular.exists() and bold.exists():
            pdfmetrics.registerFont(TTFont('FinMob', str(regular)))
            pdfmetrics.registerFont(TTFont('FinMob-Bold', str(bold)))
            pdfmetrics.registerFontFamily('FinMob', normal='FinMob', bold='FinMob-Bold')
            return 'FinMob', 'FinMob-Bold', True
    return 'Helvetica', 'Helvetica-Bold', False


def _texte(s, unicode_ok):
    s = str(s)
    if not unicode_ok:  # Helvetica ne connaît pas certains caractères
        s = s.replace('\u202f', ' ').replace('—', '-').replace('·', '-').replace('●', '')
    return s


def montant(n):
    return f'{int(n):,}'.replace(',', '\u00a0') + '\u00a0FCFA'


def _markup(s, unicode_ok):
    """Échappe le texte et convertit « 1ʳᵉ » / « 2ᵉ » en exposants ReportLab."""
    s = escape(_texte(s, unicode_ok))
    return s.replace('ʳᵉ', '<super>re</super>').replace('ᵉ', '<super>e</super>')


class Styles:
    def __init__(self):
        self.regular, self.bold, self.unicode_ok = polices()
        base = dict(fontName=self.regular, textColor=GRIS_TEXTE, fontSize=8.5, leading=11)
        self.cell = ParagraphStyle('cell', **base)
        self.cell_r = ParagraphStyle('cell_r', parent=self.cell, alignment=TA_RIGHT)
        self.cell_b = ParagraphStyle('cell_b', parent=self.cell, fontName=self.bold)
        self.cell_br = ParagraphStyle('cell_br', parent=self.cell_b, alignment=TA_RIGHT)
        self.cell_c = ParagraphStyle('cell_c', parent=self.cell, alignment=TA_CENTER)
        self.small = ParagraphStyle('small', parent=self.cell, fontSize=7.5, textColor=GRIS_MOYEN, leading=9.5)
        self.head = ParagraphStyle('head', parent=self.cell, fontName=self.bold, textColor=GRIS_TEXTE, fontSize=8)
        self.head_r = ParagraphStyle('head_r', parent=self.head, alignment=TA_RIGHT)
        self.titre = ParagraphStyle('titre', parent=self.cell, fontName=self.bold, fontSize=16, leading=20)
        self.sous_titre = ParagraphStyle('sous_titre', parent=self.cell, fontSize=9, textColor=GRIS_MOYEN, leading=12)

    def p(self, texte, style=None):
        return Paragraph(_markup(texte, self.unicode_ok), style or self.cell)

    def t(self, texte):
        return _texte(texte, self.unicode_ok)


# --- Numérotation « Page x / y » : le canevas mémorise les pages puis les dessine à la fin ---

class CanevasNumerote(canvas.Canvas):
    def __init__(self, *args, pied=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._pages = []
        self._pied = pied

    def showPage(self):
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._pages)
        for etat in self._pages:
            self.__dict__.update(etat)
            if self._pied:
                self._pied(self, total)
            super().showPage()
        super().save()


class EnTete:
    """Ce qu'affiche le bandeau du document : un établissement précis, ou tous."""

    def __init__(self, etab=None):
        annee = _('Année scolaire %(annee_scolaire)s') % {'annee_scolaire': Parametres.courant().annee_scolaire}
        if etab:
            self.nom = etab.nom
            self.ligne = ' · '.join(x for x in [etab.adresse, etab.telephone, annee] if x)
        else:
            self.nom = _('Tous les établissements')
            self.ligne = annee
        mots = [m for m in self.nom.split() if m[:1].isalpha() and len(m) > 2] if etab else []
        self.initiales = ''.join(m[0] for m in mots)[:2].upper() or 'FM'


def _entete(c, doc, etab, titre_doc, st):
    """Bandeau clair : logo noir, nom de l'établissement, titre du document, filet noir."""
    largeur, hauteur = doc.pagesize
    h = 24 * mm
    c.saveState()
    x0, y0 = doc.leftMargin, hauteur - h + 6 * mm
    xr = largeur - doc.rightMargin
    c.setStrokeColor(GRIS_FONCE)
    c.setLineWidth(1.2)
    c.line(x0, hauteur - h, xr, hauteur - h)
    # Logo : carré noir arrondi avec les initiales de l'établissement
    c.setFillColor(GRIS_FONCE)
    c.roundRect(x0, y0, 12 * mm, 12 * mm, 2.5 * mm, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont(st.bold, 11)
    c.drawCentredString(x0 + 6 * mm, y0 + 4.2 * mm, etab.initiales)
    c.setFillColor(GRIS_TEXTE)
    c.setFont(st.bold, 12.5)
    c.drawString(x0 + 16 * mm, y0 + 6.8 * mm, st.t(etab.nom))
    c.setFillColor(GRIS_MOYEN)
    c.setFont(st.regular, 8)
    c.drawString(x0 + 16 * mm, y0 + 1.8 * mm, st.t(etab.ligne))
    c.setFillColor(GRIS_TEXTE)
    c.setFont(st.bold, 11)
    c.drawRightString(xr, y0 + 6.8 * mm, st.t(titre_doc))
    c.setFillColor(GRIS_MOYEN)
    c.setFont(st.regular, 8)
    c.drawRightString(xr, y0 + 1.8 * mm, st.t(_('Édité le %(localtime)s') % {'localtime': formats.date_format(timezone.localtime(), 'SHORT_DATETIME_FORMAT')}))
    c.restoreState()


def _pied_factory(doc, etab, st):
    def pied(c, total):
        largeur, _hauteur = doc.pagesize
        c.saveState()
        c.setStrokeColor(GRIS_LIGNE)
        c.setLineWidth(0.6)
        c.line(doc.leftMargin, 12 * mm, largeur - doc.rightMargin, 12 * mm)
        c.setFont(st.regular, 7.5)
        c.setFillColor(GRIS_MOYEN)
        c.drawString(doc.leftMargin, 8 * mm, st.t(f'{settings.APP_NOM} · {etab.nom}'))
        c.drawRightString(largeur - doc.rightMargin, 8 * mm, _('Page %(page)s / %(total)s') % {'page': c.getPageNumber(), 'total': total})
        c.restoreState()
    return pied


def _document(buffer, pagesize, etab, titre_doc, st, marge=14 * mm):
    doc = BaseDocTemplate(
        buffer, pagesize=pagesize, leftMargin=marge, rightMargin=marge,
        topMargin=24 * mm + 8 * mm, bottomMargin=17 * mm,
        title=f'{titre_doc} — {etab.nom}', author=settings.APP_NOM,
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id='corps', leftPadding=0,
                  rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id='page', frames=[frame],
                                       onPage=lambda c, d: _entete(c, d, etab, titre_doc, st))])
    return doc


def _construire(doc, histoire, etab, st):
    pied = _pied_factory(doc, etab, st)
    doc.build(histoire, canvasmaker=lambda *a, **k: CanevasNumerote(*a, pied=pied, **k))


# --- Blocs réutilisables ---

class Jauge(Flowable):
    """Barre de progression (taux de recouvrement)."""

    def __init__(self, pourcentage, largeur, hauteur=2.2 * mm):
        super().__init__()
        self.pourcentage, self.width, self.height = max(0, min(100, pourcentage)), largeur, hauteur

    def draw(self):
        c = self.canv
        c.setFillColor(GRIS_LIGNE)
        c.roundRect(0, 0, self.width, self.height, self.height / 2, stroke=0, fill=1)
        if self.pourcentage:
            c.setFillColor(BLEU)
            c.roundRect(0, 0, max(self.height, self.width * self.pourcentage / 100), self.height,
                        self.height / 2, stroke=0, fill=1)


def _tuiles(st, largeur, tuiles):
    """Rangée de cartes de chiffres clés : [(libellé, valeur, jauge_ou_None, sous_texte)]."""
    n = len(tuiles)
    gap = 4 * mm
    lc = (largeur - gap * (n - 1)) / n
    val = ParagraphStyle('val', parent=st.cell_b, fontSize=13, leading=16)
    cellules = []
    for libelle, valeur, jauge, sous in tuiles:
        contenu = [st.p(libelle, st.small), Spacer(1, 1.2 * mm), st.p(valeur, val)]
        if jauge is not None:
            contenu += [Spacer(1, 1.5 * mm), Jauge(jauge, lc - 8 * mm)]
        if sous:
            contenu += [Spacer(1, 1.2 * mm), st.p(sous, st.small)]
        cellules.append(contenu)
    lignes = []
    largeurs = []
    for i, cell in enumerate(cellules):
        lignes.append(cell)
        largeurs.append(lc)
        if i < n - 1:
            lignes.append('')
            largeurs.append(gap)
    t = Table([lignes], colWidths=largeurs)
    style = [('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 4 * mm),
             ('RIGHTPADDING', (0, 0), (-1, -1), 4 * mm), ('TOPPADDING', (0, 0), (-1, -1), 3 * mm),
             ('BOTTOMPADDING', (0, 0), (-1, -1), 3.2 * mm)]
    for i in range(0, 2 * n - 1, 2):
        style += [('BACKGROUND', (i, 0), (i, 0), GRIS_FOND), ('BOX', (i, 0), (i, 0), 0.6, GRIS_LIGNE)]
    for i in range(1, 2 * n - 1, 2):
        style += [('LEFTPADDING', (i, 0), (i, 0), 0), ('RIGHTPADDING', (i, 0), (i, 0), 0)]
    t.setStyle(TableStyle(style))
    return t


def _titre(st, titre, sous_titre):
    sous_titre = sous_titre[:1].upper() + sous_titre[1:]
    return [st.p(titre, st.titre), Spacer(1, 1 * mm), st.p(sous_titre, st.sous_titre), Spacer(1, 5 * mm)]


def _style_tableau():
    return [
        ('BACKGROUND', (0, 0), (-1, 0), BLEU_PALE),
        ('LINEBELOW', (0, 0), (-1, 0), 1, GRIS_FONCE),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 4.2),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4.2),
        ('LEFTPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 5),
        ('LINEBELOW', (0, 1), (-1, -1), 0.4, GRIS_LIGNE),
    ]


# Paresseux : traduit au moment de la génération du PDF, dans la langue de l'utilisateur.
STATUT_LIBELLE = {'solde': gettext_lazy('●\xa0Soldé'), 'partiel': gettext_lazy('●\xa0Partiel'), 'aucun': gettext_lazy('●\xa0Impayé')}


# --- Liste des élèves ---

def pdf_eleves(eleves, etab, titre, sous_titre):
    st = Styles()
    etab = EnTete(etab)
    eleves = sorted(eleves, key=lambda e: (e.etablissement.nom.lower(), e.classe.lower(), e.nom.lower(), e.prenom.lower()))
    plusieurs_etabs = len({e.etablissement_id for e in eleves}) > 1
    buffer = BytesIO()
    doc = _document(buffer, landscape(A4), etab, titre, st)

    du = sum(e.montant_scolarite for e in eleves)
    verse = sum(e.total_verse for e in eleves)
    taux = round(verse * 100 / du) if du else 0
    nb_soldes = sum(1 for e in eleves if e.statut == 'solde')
    histoire = _titre(st, titre, sous_titre)
    histoire.append(_tuiles(st, doc.width, [
        (_('Élèves'), str(len(eleves)), None, _('%(valeur)s classe(s)') % {'valeur': len({e.classe for e in eleves})}),
        (_('Scolarité attendue'), montant(du), None, None),
        (_('Déjà versé'), montant(verse), None, _('Inscriptions : %(valeur)s') % {'valeur': montant(sum(e.total_inscription for e in eleves))}),
        (_('Reste à recouvrer'), montant(du - verse), None, _('%(valeur)s élève(s) non soldé(s)') % {'valeur': len(eleves) - nb_soldes}),
        (_('Taux de recouvrement'), f'{taux} %', taux, _('%(nb_soldes)s soldé(s)') % {'nb_soldes': nb_soldes}),
    ]))
    histoire.append(Spacer(1, 6 * mm))

    entetes = [_('N°'), _('Élève'), _('Parent / tuteur'), _('Téléphone'), _('N° Educ Master'),
               _('Scolarité'), _('Versé'), _('Reste'), _('Statut')]
    largeurs = [9 * mm, 54 * mm, 46 * mm, 28 * mm, 27 * mm, 27 * mm, 27 * mm, 27 * mm, 0]
    largeurs[-1] = doc.width - sum(largeurs)
    droite = {5, 6, 7}
    data = [[st.p(h, st.head_r if i in droite else st.head) for i, h in enumerate(entetes)]]
    style = _style_tableau()
    n = 0
    for (nom_etab, classe), items in groupby(eleves, key=lambda e: (e.etablissement.nom, e.classe)):
        items = list(items)
        ligne = len(data)
        libelle = f'{classe}  ·  {nom_etab}' if plusieurs_etabs else classe
        c_du = sum(e.montant_scolarite for e in items)
        c_verse = sum(e.total_verse for e in items)
        data.append([st.p(_('%(libelle)s  ·  %(items)s élève(s)  ·  %(valeur)s soldé(s)') % {'libelle': libelle, 'items': len(items), 'valeur': sum(1 for e in items if e.statut == "solde")},
                          ParagraphStyle('g', parent=st.cell_b, textColor=BLEU_FONCE))] + [''] * 4 +
                    [st.p(montant(c_du), st.cell_br), st.p(montant(c_verse), st.cell_br),
                     st.p(montant(c_du - c_verse), st.cell_br), st.p(f'{round(c_verse * 100 / c_du) if c_du else 100} %', st.cell_b)])
        style += [('SPAN', (0, ligne), (4, ligne)), ('BACKGROUND', (0, ligne), (-1, ligne), BLEU_PALE),
                  ('LINEABOVE', (0, ligne), (-1, ligne), 0.8, BLEU)]
        for e in items:
            n += 1
            ligne = len(data)
            fg, bg = {'solde': (VERT, VERT_FOND), 'partiel': (ORANGE, ORANGE_FOND), 'aucun': (ROUGE, ROUGE_FOND)}[e.statut]
            nature = f' ({e.get_nature_parent_display()})' if e.nature_parent else ''
            parent = _renseigne(e.parent)
            data.append([
                st.p(str(n), st.small), st.p(str(e), st.cell_b), st.p(f'{parent}{nature}' if parent else '—'),
                st.p(_renseigne(e.telephone) or '—'), st.p(_renseigne(e.educ_master) or '—', st.small),
                st.p(montant(e.montant_scolarite), st.cell_r), st.p(montant(e.total_verse), st.cell_r),
                st.p(montant(e.reste_a_payer), ParagraphStyle('r', parent=st.cell_br,
                     textColor=ROUGE if e.reste_a_payer else VERT)),
                st.p(STATUT_LIBELLE[e.statut], ParagraphStyle('s', parent=st.cell_b, textColor=fg, fontSize=8)),
            ])
            style.append(('BACKGROUND', (-1, ligne), (-1, ligne), bg))
            if n % 2 == 0:
                style.append(('BACKGROUND', (0, ligne), (-2, ligne), GRIS_FOND))
    ligne = len(data)
    data.append([st.p(_('TOTAL'), st.cell_b)] + [''] * 4 +
                [st.p(montant(du), st.cell_br), st.p(montant(verse), st.cell_br),
                 st.p(montant(du - verse), st.cell_br), st.p(f'{taux} %', st.cell_b)])
    style += [('SPAN', (0, ligne), (4, ligne)), ('BACKGROUND', (0, ligne), (-1, ligne), GRIS_FOND),
              ('LINEABOVE', (0, ligne), (-1, ligne), 1, GRIS_FONCE)]
    if not eleves:
        histoire.append(st.p(_('Aucun élève ne correspond à ces critères.'), st.sous_titre))
    else:
        tableau = Table(data, colWidths=largeurs, repeatRows=1)
        tableau.setStyle(TableStyle(style))
        histoire.append(tableau)
    _construire(doc, histoire, etab, st)
    return buffer.getvalue()


# --- Fiche élève (A4) ---

# Couleurs d'état (mêmes que l'interface) : vert = soldé, orange = partiel, rouge = en retard.
VERT, VERT_FOND = colors.HexColor('#1a7f37'), colors.HexColor('#e9f7ee')
ORANGE, ORANGE_FOND = colors.HexColor('#9a6700'), colors.HexColor('#fff6e0')
ROUGE, ROUGE_FOND = colors.HexColor('#c62828'), colors.HexColor('#fdecec')
NEUTRE, NEUTRE_FOND = colors.HexColor('#404040'), colors.HexColor('#f5f5f5')


def _couleurs_tranche(ligne):
    if ligne.statut == 'payee':
        return VERT, VERT_FOND
    if ligne.statut == 'retard':
        return ROUGE, ROUGE_FOND
    if ligne.paye:
        return ORANGE, ORANGE_FOND
    return NEUTRE, NEUTRE_FOND


def _renseigne(valeur):
    valeur = str(valeur or '').strip()
    return '' if valeur.lower() in ('', 'néant', 'neant') else valeur


def _bloc_infos(st, largeur, titre, lignes, toujours=()):
    """Encadré « libellé : valeur » sur deux colonnes ; une valeur peut être un Paragraph déjà stylé."""
    data = [[st.p(titre, st.cell_b), '']]
    data += [[st.p(libelle, st.small), valeur if isinstance(valeur, Paragraph) else st.p(_renseigne(valeur) or '—')]
             for libelle, valeur in lignes if isinstance(valeur, Paragraph) or _renseigne(valeur) or libelle in toujours]
    if len(data) == 1:
        data.append([st.p(_('Non renseigné'), st.small), ''])
    t = Table(data, colWidths=[largeur * 0.4, largeur * 0.6])
    t.setStyle(TableStyle([
        ('SPAN', (0, 0), (-1, 0)), ('BACKGROUND', (0, 0), (-1, 0), BLEU_PALE),
        ('LINEBELOW', (0, 0), (-1, 0), 0.8, GRIS_FONCE), ('BOX', (0, 0), (-1, -1), 0.6, GRIS_LIGNE),
        ('LINEBELOW', (0, 1), (-1, -2), 0.3, GRIS_LIGNE),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'), ('TOPPADDING', (0, 0), (-1, -1), 2.4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2.4), ('LEFTPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 5),
    ]))
    return t


def _cote_a_cote(gauche, droite, largeur, gap=4 * mm):
    demi = (largeur - gap) / 2
    t = Table([[gauche, '', droite]], colWidths=[demi, gap, demi])
    t.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 0),
                           ('RIGHTPADDING', (0, 0), (-1, -1), 0), ('TOPPADDING', (0, 0), (-1, -1), 0),
                           ('BOTTOMPADDING', (0, 0), (-1, -1), 0)]))
    return t


def _bandeau(st, largeur, texte, detail, couleur, fond):
    t = Table([[st.p(texte, ParagraphStyle('bt', parent=st.cell_b, fontSize=10.5, leading=13, textColor=couleur))],
               [st.p(detail, ParagraphStyle('bd', parent=st.cell, textColor=couleur))]], colWidths=[largeur])
    t.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), fond), ('LINEBEFORE', (0, 0), (0, -1), 3, couleur),
                           ('LEFTPADDING', (0, 0), (-1, -1), 8), ('RIGHTPADDING', (0, 0), (-1, -1), 8),
                           ('TOPPADDING', (0, 0), (0, 0), 6), ('BOTTOMPADDING', (0, -1), (-1, -1), 6),
                           ('TOPPADDING', (0, 1), (-1, -1), 1), ('BOTTOMPADDING', (0, 0), (-1, 0), 1)]))
    return t


def _situation(st, largeur, eleve, situation):
    """Bandeau de couleur : soldé (vert), à jour avec la prochaine échéance (vert) ou en retard (rouge)."""
    lettres = en_lettres(eleve.reste_a_payer) if eleve.reste_a_payer else ''
    if situation.statut == 'solde':
        return _bandeau(st, largeur, _('Scolarité soldée'), _('Toutes les tranches sont payées.'), VERT, VERT_FOND)
    if situation.en_retard:
        retard = [l for l in situation.lignes if l.statut == 'retard']
        return _bandeau(
            st, largeur, _('En retard de %(j)s jour(s) : %(m)s échu(s) non payé(s)') % {
                'j': situation.jours_de_retard, 'm': montant(situation.montant_en_retard)},
            _('%(tranches)s. Reste total à payer : %(reste)s (%(lettres)s).') % {
                'tranches': ', '.join(f'{l.echeance} ({l.echeance.echeance:%d/%m/%Y})' for l in retard),
                'reste': montant(eleve.reste_a_payer), 'lettres': lettres},
            ROUGE, ROUGE_FOND)
    p = situation.prochaine
    detail = _('Reste à payer : %(reste)s (%(lettres)s).') % {'reste': montant(eleve.reste_a_payer), 'lettres': lettres}
    texte = (_('À jour — prochaine échéance : %(tranche)s le %(date)s, %(m)s') % {
        'tranche': p.echeance, 'date': f'{p.echeance.echeance:%d/%m/%Y}', 'm': montant(p.reste)}
        if p else _('À jour'))
    return _bandeau(st, largeur, texte, detail, VERT, VERT_FOND)


def _repartition(st, largeur, paiement):
    """Tableau « sur quelle tranche est allé ce paiement » (paiements de scolarité)."""
    imputations = list(paiement.imputations.all()) if paiement and paiement.pk else []
    if not imputations:
        return None
    data = [[st.p(_('Affecté à'), st.head), st.p(_('Montant'), st.head_r), st.p(_('Reste sur la tranche'), st.head_r)]]
    style = _style_tableau()
    for i, x in enumerate(imputations, start=1):
        fg = VERT if x.soldee else ORANGE
        data.append([st.p(x.libelle + (f' ({x.date_echeance:%d/%m/%Y})' if x.date_echeance else '')),
                     st.p(montant(x.montant), st.cell_r),
                     st.p(_('Soldée') if x.soldee else montant(x.reste_tranche),
                          ParagraphStyle('rt', parent=st.cell_br, textColor=fg))])
    t = Table(data, colWidths=[largeur - 56 * mm, 26 * mm, 30 * mm])
    t.setStyle(TableStyle(style))
    return t


def _cadre_signature(st, largeur, gauche, titre, sous_titre, hauteur=11 * mm):
    """Texte à gauche, cadre « signature et cachet » à droite."""
    droite = [st.p(titre, st.cell_b), Spacer(1, 1 * mm), st.p(sous_titre, st.small), Spacer(1, hauteur)]
    t = Table([[gauche, '', droite]], colWidths=[largeur * 0.55, largeur * 0.05, largeur * 0.4])
    t.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('BOX', (2, 0), (2, 0), 0.6, GRIS_CLAIR),
                           ('LEFTPADDING', (0, 0), (1, 0), 0), ('LEFTPADDING', (2, 0), (2, 0), 6),
                           ('TOPPADDING', (2, 0), (2, 0), 5)]))
    return KeepTogether([t])


def _section(st, titre):
    return [Spacer(1, 4 * mm), st.p(titre, ParagraphStyle('sec', parent=st.cell_b, fontSize=10.5, leading=13)),
            Spacer(1, 2 * mm)]


def pdf_fiche_eleve(eleve, situation, paiements, reductions, fratrie=()):
    """Fiche complète d'un élève : situation, identité, parents, inscription, tranches, paiements, signature."""
    st = Styles()
    etab = EnTete(eleve.etablissement)
    buffer = BytesIO()
    doc = _document(buffer, A4, etab, _('Fiche élève'), st)
    L = doc.width
    sous_titre = _('%(classe)s · inscrit(e) le %(date)s') % {'classe': eleve.classe, 'date': f'{eleve.cree_le:%d/%m/%Y}'}
    if _renseigne(eleve.educ_master):
        sous_titre += f' · Educ Master {eleve.educ_master}'
    histoire = _titre(st, f'{eleve.nom.upper()} {eleve.prenom}', sous_titre)
    if eleve.archive:
        histoire += [_bandeau(st, L, _('Élève archivé'), _('Le %(date)s — %(motif)s') % {
            'date': f'{eleve.archive_le:%d/%m/%Y}' if eleve.archive_le else '—', 'motif': eleve.motif_archive},
            NEUTRE, NEUTRE_FOND), Spacer(1, 3 * mm)]

    # Chiffres clés
    du, verse = eleve.montant_scolarite, eleve.total_verse
    taux = round(verse * 100 / du) if du else 100
    histoire.append(_tuiles(st, L, [
        (_('Scolarité due'), montant(du), None, _('après réductions') if reductions else None),
        (_('Déjà versé'), montant(verse), taux, f'{taux} %'),
        (_('Reste à payer'), montant(eleve.reste_a_payer), None, None),
        (_('Inscription'), montant(eleve.total_inscription), None, str(eleve.statut_inscription_libelle)),
    ]))
    histoire.append(Spacer(1, 3 * mm))

    # Situation à la date d'édition
    histoire.append(_situation(st, L, eleve, situation))

    # Identité et parents
    histoire += _section(st, _('Identité et parents'))
    identite = _bloc_infos(st, (L - 4 * mm) / 2, _('Élève'), [
        (_('Sexe'), eleve.get_sexe_display()),
        (_('Né(e) le'), f'{eleve.date_naissance:%d/%m/%Y}' if eleve.date_naissance else ''),
        (_('Lieu de naissance'), eleve.lieu_naissance),
        (_('Classe'), eleve.classe),
        (_('Établissement'), eleve.etablissement.nom),
        (_('Résidence'), eleve.residence),
    ], toujours=(_('Classe'), _('Établissement')))
    nature = f' ({eleve.get_nature_parent_display()})' if eleve.nature_parent else ''
    parents = [
        (_('Parent principal'), _renseigne(eleve.parent) and f'{eleve.parent}{nature}'),
        (_('Téléphone'), eleve.telephone),
        (_('WhatsApp'), eleve.whatsapp),
        (_('E-mail'), eleve.email),
    ]
    for c in eleve.contacts.all():
        n = f' ({c.get_nature_display()})' if c.nature else ''
        parents.append((_('Autre parent'), f'{c.nom}{n} · {c.telephone or c.email or "—"}'))
    parents.append((_('Rappels automatiques'), _('Oui') if eleve.suivi_actif else _('Non')))
    histoire.append(_cote_a_cote(identite, _bloc_infos(st, (L - 4 * mm) / 2, _('Parents'), parents,
                                                       toujours=(_('Parent principal'), _('Téléphone'))), L))
    autres = [f for f in fratrie if f.pk != eleve.pk]
    if autres:
        histoire += [Spacer(1, 2 * mm), st.p(_('Frères et sœurs dans l’établissement : %(liste)s') % {
            'liste': ', '.join(f'{f.nom.upper()} {f.prenom} ({f.classe})' for f in autres)}, st.small)]

    # Frais d'inscription
    histoire += _section(st, _('Frais d’inscription'))
    statut = eleve.statut_inscription
    couleur = {'payee': VERT, 'partielle': ORANGE, 'impayee': ROUGE}.get(statut, NEUTRE)
    if eleve.reductions_inscription:
        lignes = [(_('Frais d’inscription'), montant(eleve.frais_inscription))]
        lignes += [(libelle, '−' + montant(m)) for libelle, m in eleve.reductions_inscription]
        lignes += [(_('À payer'), montant(eleve.inscription_due))]
    else:
        lignes = [(_('À payer'), montant(eleve.inscription_due))]
    lignes += [(_('Versé'), montant(eleve.total_inscription)),
               (_('Reste'), montant(eleve.inscription_reste)),
               (_('Statut'), st.p(str(eleve.statut_inscription_libelle),
                                  ParagraphStyle('si', parent=st.cell_b, textColor=couleur)))]
    reduc = [(libelle, '−' + montant(m)) for libelle, m in reductions] or [(_('Aucune'), '')]
    tout = [libelle for libelle, _v in lignes]
    histoire.append(_cote_a_cote(_bloc_infos(st, (L - 4 * mm) / 2, _('Inscription'), lignes, toujours=tout),
                                 _bloc_infos(st, (L - 4 * mm) / 2, _('Réductions sur la scolarité'), reduc,
                                             toujours=[libelle for libelle, _v in reduc]), L))

    # Échéancier
    histoire += _section(st, _('Échéancier de la scolarité'))
    largeurs = [0, 24 * mm, 27 * mm, 27 * mm, 27 * mm, 44 * mm]
    largeurs[0] = L - sum(largeurs)
    data = [[st.p(h, st.head_r if 2 <= i <= 4 else st.head) for i, h in enumerate(
        [_('Tranche'), _('Échéance'), _('Montant'), _('Payé'), _('Reste'), _('Statut')])]]
    style = _style_tableau()
    for i, l in enumerate(situation.lignes, start=1):
        fg, bg = _couleurs_tranche(l)
        data.append([st.p(str(l.echeance), st.cell_b), st.p(f'{l.echeance.echeance:%d/%m/%Y}'),
                     st.p(montant(l.montant), st.cell_r), st.p(montant(l.paye), st.cell_r),
                     st.p(montant(l.reste), st.cell_br),
                     st.p('●\xa0' + str(l.statut_libelle), ParagraphStyle('ts', parent=st.cell_b, textColor=fg, fontSize=8))])
        style.append(('BACKGROUND', (-1, i), (-1, i), bg))
    fin = len(data)
    data.append([st.p(_('TOTAL'), st.cell_b), '', st.p(montant(sum(l.montant for l in situation.lignes)), st.cell_br),
                 st.p(montant(sum(l.paye for l in situation.lignes)), st.cell_br),
                 st.p(montant(situation.reste), st.cell_br), ''])
    style += [('SPAN', (0, fin), (1, fin)), ('BACKGROUND', (0, fin), (-1, fin), GRIS_FOND),
              ('LINEABOVE', (0, fin), (-1, fin), 1, GRIS_FONCE)]
    t = Table(data, colWidths=largeurs, repeatRows=1)
    t.setStyle(TableStyle(style))
    histoire.append(t)

    # Paiements et leur répartition
    histoire += _section(st, _('Paiements'))
    if paiements:
        largeurs = [25 * mm, 19 * mm, 0, 62 * mm, 26 * mm]
        largeurs[2] = L - sum(largeurs)
        data = [[st.p(h, st.head_r if i == 4 else st.head) for i, h in enumerate(
            [_('N° reçu'), _('Date'), _('Motif et mode'), _('Affecté à'), _('Montant')])]]
        style = _style_tableau()
        for i, p in enumerate(paiements, start=1):
            imputations = list(p.imputations.all()) if hasattr(p, 'imputations') else []
            affecte = '<br/>'.join(escape(f'{x.libelle} : {montant(x.montant)}'
                                          + (f' ({_("soldée")})' if x.soldee else '')) for x in imputations)
            motif = escape(p.get_motif_display()) + f'<br/><font size="7.5" color="#6b6b6b">{escape(p.get_mode_display())}' + (
                f' · {escape(p.reference)}' if p.reference else '') + '</font>'
            if not affecte:
                affecte = escape(_('Frais d’inscription')) if p.motif == 'inscription' else '—'
            data.append([st.p(p.numero_recu or '—', st.cell_b), st.p(f'{p.date:%d/%m/%Y}'),
                         Paragraph(motif, st.cell), Paragraph(affecte, st.small),
                         st.p(montant(p.montant), st.cell_br)])
            if i % 2 == 0:
                style.append(('BACKGROUND', (0, i), (-1, i), GRIS_FOND))
        fin = len(data)
        data.append([st.p(_('TOTAL VERSÉ (scolarité et inscription)'), st.cell_b)] + [''] * 3 + [st.p(montant(sum(p.montant for p in paiements)), st.cell_br)])
        style += [('SPAN', (0, fin), (3, fin)), ('BACKGROUND', (0, fin), (-1, fin), BLEU_PALE),
                  ('LINEABOVE', (0, fin), (-1, fin), 1, BLEU), ('VALIGN', (0, 1), (-1, -1), 'TOP')]
        t = Table(data, colWidths=largeurs, repeatRows=1)
        t.setStyle(TableStyle(style))
        histoire.append(t)
    else:
        histoire.append(st.p(_('Aucun paiement enregistré.'), st.sous_titre))

    # Signature
    maintenant = timezone.localtime()
    gauche = [st.p(_('Fiche arrêtée le %(date)s à %(heure)s.') % {'date': f'{maintenant:%d/%m/%Y}', 'heure': f'{maintenant:%H:%M}'}, st.small),
              Spacer(1, 1.5 * mm),
              st.p(_('Les montants tiennent compte de tous les paiements enregistrés à cette date.'), st.small)]
    histoire += [Spacer(1, 5 * mm), _cadre_signature(st, L, gauche, _('La Direction'), _('Signature et cachet'))]
    _construire(doc, histoire, etab, st)
    return buffer.getvalue()


# --- Journal des paiements ---

def pdf_paiements(paiements, etab, sous_titre):
    st = Styles()
    etab = EnTete(etab)
    paiements = sorted(paiements, key=lambda p: (p.date, p.numero_recu or ''))
    buffer = BytesIO()
    titre = _('Journal des paiements')
    doc = _document(buffer, landscape(A4), etab, titre, st)
    total = sum(p.montant for p in paiements)
    par_mode = {}
    for p in paiements:
        par_mode[p.get_mode_display()] = par_mode.get(p.get_mode_display(), 0) + p.montant
    modes = sorted(par_mode.items(), key=lambda kv: -kv[1])
    inscriptions = sum(p.montant for p in paiements if p.motif == 'inscription')

    histoire = _titre(st, titre, sous_titre)
    tuiles = [(_('Total encaissé'), montant(total), None, _('%(n)s paiement(s)') % {'n': len(paiements)}),
              (_('Scolarité'), montant(total - inscriptions), None, None),
              (_('Inscriptions'), montant(inscriptions), None, None)]
    for mode, m in modes[:2]:
        part = round(m * 100 / total) if total else 0
        tuiles.append((mode, montant(m), part, _('%(part)s %% du total') % {'part': part}))
    histoire += [_tuiles(st, doc.width, tuiles), Spacer(1, 6 * mm)]

    entetes = [_('N° reçu'), _('Élève'), _('Classe'), _('Motif'), _('Mode'), _('Référence'), _('Encaissé par'), _('Montant')]
    largeurs = [26 * mm, 0, 28 * mm, 32 * mm, 28 * mm, 28 * mm, 30 * mm, 30 * mm]
    largeurs[1] = doc.width - sum(largeurs)
    data = [[st.p(h, st.head_r if i == 7 else st.head) for i, h in enumerate(entetes)]]
    style = _style_tableau()
    jour_style = ParagraphStyle('jour', parent=st.cell_b, textColor=BLEU_FONCE)
    for jour, items in groupby(paiements, key=lambda p: p.date):
        items = list(items)
        ligne = len(data)
        data.append([st.p(formats.date_format(jour, 'l d/m/Y').capitalize() + '  ·  ' +
                          _('%(n)s paiement(s)') % {'n': len(items)}, jour_style)] + [''] * 6 +
                    [st.p(montant(sum(p.montant for p in items)), st.cell_br)])
        style += [('SPAN', (0, ligne), (6, ligne)), ('BACKGROUND', (0, ligne), (-1, ligne), BLEU_PALE),
                  ('LINEABOVE', (0, ligne), (-1, ligne), 0.8, BLEU)]
        for i, p in enumerate(items, start=1):
            caissier = (p.encaisse_par.get_full_name() or p.encaisse_par.username) if p.encaisse_par else '—'
            data.append([
                st.p(p.numero_recu, st.cell_b), st.p(str(p.eleve)), st.p(_renseigne(p.eleve.classe) or '—'),
                st.p(p.get_motif_display()), st.p(p.get_mode_display()),
                st.p(p.reference or '—', st.small), st.p(caissier, st.small), st.p(montant(p.montant), st.cell_br),
            ])
            if i % 2 == 0:
                style.append(('BACKGROUND', (0, len(data) - 1), (-1, len(data) - 1), GRIS_FOND))
    ligne = len(data)
    data.append([st.p(_('TOTAL'), st.cell_b)] + [''] * 6 + [st.p(montant(total), st.cell_br)])
    style += [('SPAN', (0, ligne), (6, ligne)), ('BACKGROUND', (0, ligne), (-1, ligne), GRIS_FOND),
              ('LINEABOVE', (0, ligne), (-1, ligne), 1.2, GRIS_FONCE)]
    if not paiements:
        histoire.append(st.p(_('Aucun paiement ne correspond à ces critères.'), st.sous_titre))
    else:
        tableau = Table(data, colWidths=largeurs, repeatRows=1)
        tableau.setStyle(TableStyle(style))
        histoire.append(tableau)
        detail = [st.p(_('Journal arrêté à la somme de %(m)s') % {'m': montant(total)}, st.cell_b), Spacer(1, 1 * mm),
                  st.p(en_lettres(total), st.small), Spacer(1, 2 * mm)]
        detail += [st.p(f'{mode} : {montant(m)}', st.small) for mode, m in modes]
        histoire += [Spacer(1, 6 * mm), _cadre_signature(st, doc.width, detail, _('Le caissier — La Direction'),
                                                         _('Signatures et cachet'), hauteur=14 * mm)]
    _construire(doc, histoire, etab, st)
    return buffer.getvalue()


# --- Reçu (A5) ---

def pdf_recu(p):
    st = Styles()
    eleve = p.eleve
    etab = EnTete(eleve.etablissement)
    buffer = BytesIO()
    doc = _document(buffer, A5, etab, _('Reçu n° %(numero_recu)s') % {'numero_recu': p.numero_recu}, st, marge=11 * mm)
    lbl = ParagraphStyle('lbl', parent=st.cell, textColor=GRIS_MOYEN, fontSize=9)
    val = ParagraphStyle('val', parent=st.cell_b, fontSize=9)

    histoire = [
        st.p(_('REÇU DE PAIEMENT'), ParagraphStyle('rt', parent=st.cell_b, alignment=TA_CENTER, fontSize=11,
                                                 textColor=GRIS_MOYEN, leading=14)),
        Spacer(1, 1 * mm),
        st.p(_('N° %(numero_recu)s · %(date)s') % {'numero_recu': p.numero_recu, 'date': format(p.date, '%d/%m/%Y')}, ParagraphStyle('rn', parent=st.cell, alignment=TA_CENTER,
                                                                       textColor=GRIS_MOYEN)),
        Spacer(1, 5 * mm),
    ]
    lignes = [(_('Élève'), str(eleve)), (_('Classe'), eleve.classe)]
    if _renseigne(eleve.educ_master):
        lignes.append((_('N° Educ Master'), eleve.educ_master))
    if _renseigne(eleve.parent):
        lignes.append((_('Parent / tuteur'), eleve.parent + (f' ({eleve.get_nature_parent_display()})' if eleve.nature_parent else '')))
    lignes += [
        (_('Motif'), p.get_motif_display()),
        (_('Mode de paiement'), p.get_mode_display() + (_(' — réf. %(reference)s') % {'reference': p.reference} if p.reference else '')),
    ]
    t = Table([[st.p(a, lbl), st.p(b, val)] for a, b in lignes], colWidths=[40 * mm, doc.width - 40 * mm])
    t.setStyle(TableStyle([('LINEBELOW', (0, 0), (-1, -1), 0.4, GRIS_LIGNE), ('TOPPADDING', (0, 0), (-1, -1), 4),
                           ('BOTTOMPADDING', (0, 0), (-1, -1), 4), ('LEFTPADDING', (0, 0), (-1, -1), 0)]))
    histoire += [t, Spacer(1, 6 * mm)]

    encadre = Table([
        [st.p(montant(p.montant), ParagraphStyle('m', parent=st.cell_b, fontSize=20, leading=24, alignment=TA_CENTER))],
        [st.p(en_lettres(p.montant), ParagraphStyle('ml', parent=st.cell, alignment=TA_CENTER, textColor=GRIS_MOYEN))],
    ], colWidths=[doc.width])
    encadre.setStyle(TableStyle([('BOX', (0, 0), (-1, -1), 1.4, BLEU), ('BACKGROUND', (0, 0), (-1, -1), BLEU_PALE),
                                 ('TOPPADDING', (0, 0), (-1, 0), 9), ('BOTTOMPADDING', (0, -1), (-1, -1), 9),
                                 ('ROUNDEDCORNERS', [4, 4, 4, 4])]))
    histoire += [encadre, Spacer(1, 4 * mm)]
    repartition = _repartition(st, doc.width, p)
    if repartition:
        histoire += [repartition, Spacer(1, 4 * mm)]
    histoire.append(_situation(st, doc.width, eleve, eleve.situation))

    caissier = (p.encaisse_par.get_full_name() or p.encaisse_par.username) if p.encaisse_par else ''
    sign = ParagraphStyle('sg', parent=st.cell, alignment=TA_CENTER, textColor=GRIS_MOYEN)
    signatures = Table([
        ['', '', ''],
        [st.p(_('Le parent'), sign), '',
         Paragraph(_markup(_('Le caissier'), st.unicode_ok) + (f'<br/><b>{_markup(caissier, st.unicode_ok)}</b>' if caissier else ''), sign)],
    ], colWidths=[48 * mm, doc.width - 96 * mm, 48 * mm], rowHeights=[16 * mm, None])
    signatures.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LINEBELOW', (0, 0), (0, 0), 0.6, GRIS_MOYEN),
                                    ('LINEBELOW', (2, 0), (2, 0), 0.6, GRIS_MOYEN)]))
    histoire += [Spacer(1, 4 * mm), KeepTogether(signatures), Spacer(1, 5 * mm),
                 st.p(_('Situation à la date d’édition · Conservez ce reçu.'),
                      ParagraphStyle('f', parent=st.small, alignment=TA_CENTER))]
    _construire(doc, histoire, etab, st)
    return buffer.getvalue()


# --- Facture (A5) ---

def qr_drawing(contenu, taille=26 * mm):
    from reportlab.graphics.barcode.qr import QrCodeWidget
    from reportlab.graphics.shapes import Drawing

    widget = QrCodeWidget(contenu)
    x0, y0, x1, y1 = widget.getBounds()
    dessin = Drawing(taille, taille, transform=[taille / (x1 - x0), 0, 0, taille / (y1 - y0), 0, 0])
    dessin.add(widget)
    return dessin


def montants_taxes(facture):
    """(hors taxe, TVA, TTC) selon le groupe de taxation de la facture."""
    if facture.groupe_taxe == 'B':
        ht = round(facture.montant / 1.18)
        return ht, facture.montant - ht, facture.montant
    return facture.montant, 0, facture.montant


def pdf_facture(facture):
    st = Styles()
    eleve = facture.eleve
    etab_obj = eleve.etablissement
    etab = EnTete(etab_obj)
    buffer = BytesIO()
    titre = (_("Facture d'avoir n° %(numero)s") if facture.est_avoir else _('Facture n° %(numero)s')) % {'numero': facture.numero}
    doc = _document(buffer, A5, etab, titre, st, marge=11 * mm)
    lbl = ParagraphStyle('lbl', parent=st.cell, textColor=GRIS_MOYEN, fontSize=8.5)
    val = ParagraphStyle('val', parent=st.cell_b, fontSize=8.5)
    centre = ParagraphStyle('c', parent=st.cell, alignment=TA_CENTER, textColor=GRIS_MOYEN)

    histoire = [
        st.p(_("FACTURE D'AVOIR") if facture.est_avoir else _('FACTURE'),
             ParagraphStyle('ft', parent=st.cell_b, alignment=TA_CENTER, fontSize=12, leading=15)),
        st.p(_('N° %(numero)s · %(date)s') % {'numero': facture.numero,
                                               'date': timezone.localtime(facture.date).strftime('%d/%m/%Y %H:%M')}, centre),
        Spacer(1, 4 * mm),
    ]
    vendeur = [etab_obj.nom]
    if facture.ifu:
        vendeur.append(_('IFU : %(ifu)s') % {'ifu': facture.ifu})
    if etab_obj.rccm:
        vendeur.append(_('RCCM : %(rccm)s') % {'rccm': etab_obj.rccm})
    vendeur += [x for x in (etab_obj.adresse, etab_obj.telephone) if x]
    client = [x for x in [_renseigne(facture.client_nom)] if x]
    client.append(_('Parent de %(eleve)s — %(classe)s') % {'eleve': eleve, 'classe': eleve.classe})
    if _renseigne(facture.client_contact):
        client.append(facture.client_contact)
    parties = Table([
        [st.p(_('Émetteur'), lbl), st.p(_('Client'), lbl)],
        [Paragraph('<br/>'.join(_markup(x, st.unicode_ok) for x in vendeur), val),
         Paragraph('<br/>'.join(_markup(x, st.unicode_ok) for x in client), val)],
    ], colWidths=[doc.width / 2, doc.width / 2])
    parties.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 0),
                                 ('BOTTOMPADDING', (0, 0), (-1, 0), 1)]))
    histoire += [parties, Spacer(1, 5 * mm)]

    data = [[st.p(_('Désignation'), st.head), st.p(_('Montant'), st.head_r)]]
    for ligne in facture.lignes:
        data.append([st.p(ligne['libelle']), st.p(montant(ligne['montant']), st.cell_r)])
    ht, tva, ttc = montants_taxes(facture)
    if facture.groupe_taxe == 'B':
        data += [[st.p(_('Total hors taxe')), st.p(montant(ht), st.cell_r)],
                 [st.p(_('TVA 18 %')), st.p(montant(tva), st.cell_r)]]
    data.append([st.p(_('TOTAL TTC') if facture.groupe_taxe == 'B' else _('TOTAL'), st.cell_b),
                 st.p(montant(ttc), st.cell_br)])
    style = _style_tableau() + [('LINEABOVE', (0, -1), (-1, -1), 1, GRIS_FONCE), ('BACKGROUND', (0, -1), (-1, -1), GRIS_FOND)]
    tableau = Table(data, colWidths=[doc.width - 32 * mm, 32 * mm])
    tableau.setStyle(TableStyle(style))
    histoire += [tableau, Spacer(1, 2 * mm),
                 st.p(en_lettres(facture.montant), ParagraphStyle('l', parent=st.small, fontSize=8)),
                 Spacer(1, 3 * mm)]
    repartition = None if facture.est_avoir or not facture.paiement_id else _repartition(st, doc.width, facture.paiement)
    if repartition:
        histoire += [repartition, Spacer(1, 3 * mm)]

    infos = [
        (_('Mode de paiement'), facture.get_mode_display()),
        (_('Référence du paiement'), facture.paiement.numero_recu if facture.paiement_id else '—'),
    ]
    if facture.paiement_id and facture.paiement.reference:
        infos.append((_('N° de transaction'), facture.paiement.reference))
    if facture.groupe_taxe == 'A':
        infos.append((_('Taxation'), _('Groupe A — exonéré de TVA')))
    if facture.est_avoir and facture.facture_origine_id:
        infos.append((_('Annule la facture'), facture.facture_origine.numero))
    t = Table([[st.p(a, lbl), st.p(b, val)] for a, b in infos], colWidths=[40 * mm, doc.width - 40 * mm])
    t.setStyle(TableStyle([('LINEBELOW', (0, 0), (-1, -1), 0.4, GRIS_LIGNE), ('TOPPADDING', (0, 0), (-1, -1), 3),
                           ('BOTTOMPADDING', (0, 0), (-1, -1), 3), ('LEFTPADDING', (0, 0), (-1, -1), 0)]))
    histoire += [t, Spacer(1, 4 * mm)]

    if facture.statut == 'normalisee':
        mecef = [
            st.p(_('Éléments de sécurité de la facture normalisée'), st.cell_b),
            st.p(_('Code MECeF/DGI : %(code)s') % {'code': facture.code_mecef}),
            st.p(_('MECeF NIM : %(nim)s') % {'nim': facture.nim}),
            st.p(_('Compteurs : %(c)s') % {'c': facture.compteurs}),
            st.p(_('Heure MECeF : %(h)s') % {'h': facture.date_mecef}),
        ]
        bloc = Table([[mecef, qr_drawing(facture.qr_code) if facture.qr_code else '']],
                     colWidths=[doc.width - 30 * mm, 30 * mm])
        bloc.setStyle(TableStyle([('BOX', (0, 0), (-1, -1), 0.8, GRIS_FONCE), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                                  ('LEFTPADDING', (0, 0), (-1, -1), 6), ('TOPPADDING', (0, 0), (-1, -1), 5),
                                  ('BOTTOMPADDING', (0, 0), (-1, -1), 5)]))
        histoire.append(KeepTogether(bloc))
    else:
        histoire.append(st.p(_('Facture non normalisée : la normalisation e-MECeF n’est pas encore activée pour cet établissement.')
                             if facture.statut == 'en_attente' else
                             _('Normalisation e-MECeF en attente (le service n’a pas répondu) : la facture sera normalisée dès que possible.'),
                             ParagraphStyle('nn', parent=st.small, textColor=GRIS_FONCE)))
    if facture.annulee:
        histoire += [Spacer(1, 3 * mm), _bandeau(st, doc.width, _('Facture annulée'),
                                                 _('Cette facture a été annulée par une facture d’avoir.'), ROUGE, ROUGE_FOND)]
    elif not facture.est_avoir:
        histoire += [Spacer(1, 4 * mm), _situation(st, doc.width, eleve, eleve.situation)]
    caissier = ''
    if facture.paiement_id and facture.paiement.encaisse_par:
        u = facture.paiement.encaisse_par
        caissier = u.get_full_name() or u.username
    gauche = [st.p(_('Encaissé par %(nom)s') % {'nom': caissier}, st.small)] if caissier else []
    gauche += [Spacer(1, 1 * mm), st.p(_('Conservez cette facture : elle prouve le paiement.'), st.small)]
    histoire += [Spacer(1, 4 * mm), _cadre_signature(st, doc.width, gauche, _('Pour l’établissement'),
                                                     _('Signature et cachet'), hauteur=9 * mm)]
    _construire(doc, histoire, etab, st)
    return buffer.getvalue()


def svg_qr(contenu, taille=120):
    """QR code en SVG (pour l'affichage de la facture à l'écran)."""
    from reportlab.graphics import renderSVG

    dessin = qr_drawing(contenu, taille)
    return renderSVG.drawToString(dessin).split('?>', 1)[-1]
