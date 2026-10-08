"""PDF des modules pédagogiques : bulletins, bon de paiement, emploi du temps, planning de composition.

Réutilise la charte des PDF de la scolarité (en-tête gris, accents bleus, pied de page numéroté).
"""

from io import BytesIO

from django.utils import dateformat
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, A5, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, PageBreak, Spacer, Table, TableStyle

from ecole.pdf import (
    BLEU, BLEU_FONCE, BLEU_PALE, GRIS_FOND, GRIS_LIGNE, GRIS_MOYEN, STATUTS, EnTete, Styles, _construire, _document,
    _style_tableau, _titre, _tuiles, montant,
)
from ecole.models import Parametres
from ecole.templatetags.ecole_extras import en_lettres

from .calculs import appreciation, libelle_rang
from .models import Jour
from django.utils.translation import gettext as _


def _note(v):
    return '—' if v is None else f'{v:.2f}'.replace('.', ',')


def _couleur_moyenne(v):
    if v is None:
        return GRIS_MOYEN
    return GRIS_MOYEN if v < 10 else STATUTS['solde'][0]


# --------------------------------------------------------------------------- Bulletins

def bulletins(res, resultats_eleves):
    """Un bulletin par élève (une page A4 chacun), pour une classe et une période."""
    st = Styles()
    etab = res.etablissement
    periode_txt = dict(etab.periodes)[res.periode]
    buffer = BytesIO()
    doc = _document(buffer, A4, EnTete(etab), _('Bulletin de notes · %(periode_txt)s') % {'periode_txt': periode_txt}, st, marge=12 * mm)
    lbl = ParagraphStyle('lbl', parent=st.small)
    histoire = []
    for i, r in enumerate(resultats_eleves):
        if i:
            histoire.append(PageBreak())
        e = r.eleve
        histoire += [
            st.p(_('BULLETIN DE NOTES'), ParagraphStyle('bt', parent=st.cell_b, fontSize=14, leading=18, alignment=TA_CENTER)),
            st.p(_('%(periode_txt)s · Année scolaire %(annee_scolaire)s') % {'periode_txt': periode_txt, 'annee_scolaire': Parametres.courant().annee_scolaire},
                 ParagraphStyle('bs', parent=st.sous_titre, alignment=TA_CENTER)),
            Spacer(1, 4 * mm),
        ]
        identite = Table([
            [st.p(_('Élève'), lbl), st.p(str(e), st.cell_b), st.p(_('Classe'), lbl), st.p(res.classe, st.cell_b)],
            [st.p(_('N° Educ Master'), lbl), st.p(e.educ_master or '—'), st.p(_('Effectif'), lbl), st.p(str(len(res.eleves)))],
            [st.p(_('Parent / tuteur'), lbl), st.p(e.parent), st.p(_('Téléphone'), lbl), st.p(e.telephone)],
        ], colWidths=[28 * mm, 70 * mm, 24 * mm, doc.width - 122 * mm])
        identite.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), GRIS_FOND), ('BOX', (0, 0), (-1, -1), 0.6, GRIS_LIGNE),
                                      ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3)]))
        histoire += [identite, Spacer(1, 5 * mm)]

        entetes = [_('Matière'), 'MI', 'D1', 'D2', _('Moy. /20'), _('Coef.'), _('Moy. × coef.'), _('Rang'), _('Professeur'), _('Appréciation')]
        largeurs = [36 * mm, 12 * mm, 12 * mm, 12 * mm, 16 * mm, 11 * mm, 19 * mm, 13 * mm, 0, 24 * mm]
        largeurs[8] = doc.width - sum(largeurs)
        centre = ParagraphStyle('c', parent=st.cell, alignment=TA_CENTER)
        centre_b = ParagraphStyle('cb', parent=st.cell_b, alignment=TA_CENTER)
        data = [[st.p(h, ParagraphStyle('h', parent=st.head, alignment=TA_CENTER) if j else st.head) for j, h in enumerate(entetes)]]
        style = _style_tableau()
        for j, ligne in enumerate(r.matieres, start=1):
            f, ens = ligne.fiche, ligne.enseignement
            moy = ligne.moyenne
            data.append([
                st.p(str(ens.matiere), st.cell_b),
                st.p(_note(f.mi if f else None), centre), st.p(_note(f.d1 if f else None), centre),
                st.p(_note(f.d2 if f else None), centre),
                st.p(_note(moy), ParagraphStyle('m', parent=centre_b, textColor=_couleur_moyenne(moy))),
                st.p(str(ens.coefficient), centre), st.p(_note(ligne.points), centre),
                st.p(libelle_rang(ligne.rang), centre),
                st.p(str(ens.professeur) if ens.professeur else '—', st.small),
                st.p(appreciation(moy), st.small),
            ])
            if j % 2 == 0:
                style.append(('BACKGROUND', (0, j), (-1, j), GRIS_FOND))
        n = len(data)
        data.append([st.p(_('TOTAUX'), st.cell_b), '', '', '', '', st.p(str(r.total_coef), centre_b),
                     st.p(_note(r.total_points), centre_b), '', '', ''])
        style += [('SPAN', (0, n), (4, n)), ('BACKGROUND', (0, n), (-1, n), BLEU_PALE), ('LINEABOVE', (0, n), (-1, n), 1, BLEU)]
        t = Table(data, colWidths=largeurs, repeatRows=1)
        t.setStyle(TableStyle(style))
        histoire += [t, Spacer(1, 5 * mm)]

        tuiles = [
            (_('Moyenne générale'), f'{_note(r.moyenne)} / 20', None, r.appreciation),
            (_('Rang'), libelle_rang(r.rang), None, _('sur %(eleves)s élève(s)') % {'eleves': len(res.eleves)}),
            (_('Moyenne de la classe'), _note(res.moyenne_classe), None,
             _('Plus forte %(plus_forte)s · plus faible %(plus_faible)s') % {'plus_forte': _note(res.plus_forte), 'plus_faible': _note(res.plus_faible)}),
        ]
        if r.moyenne_annuelle is not None:
            tuiles.append((_('Moyenne annuelle'), f'{_note(r.moyenne_annuelle)} / 20', None, _('Rang %(rang_annuel)s') % {'rang_annuel': libelle_rang(r.rang_annuel)}))
        histoire += [_tuiles(st, doc.width, tuiles), Spacer(1, 8 * mm)]

        sign = ParagraphStyle('sg', parent=st.cell, alignment=TA_CENTER, textColor=GRIS_MOYEN)
        signatures = Table([['', '', ''], [st.p(_('Le professeur principal'), sign), '', st.p(_('Le chef d’établissement'), sign)]],
                           colWidths=[60 * mm, doc.width - 120 * mm, 60 * mm], rowHeights=[16 * mm, None])
        signatures.setStyle(TableStyle([('LINEBELOW', (0, 0), (0, 0), 0.6, GRIS_MOYEN), ('LINEBELOW', (2, 0), (2, 0), 0.6, GRIS_MOYEN)]))
        histoire.append(KeepTogether(signatures))
    if not resultats_eleves:
        histoire.append(st.p(_('Aucun élève dans cette classe.'), st.sous_titre))
    _construire(doc, histoire, EnTete(etab), st)
    return buffer.getvalue()


# --------------------------------------------------------------------------- Bon de paiement professeur

def bon_paiement(p):
    st = Styles()
    prof = p.professeur
    etab = prof.enseignements.select_related('etablissement').first()
    entete = EnTete(etab.etablissement if etab else None)
    buffer = BytesIO()
    doc = _document(buffer, A5, entete, _('Bon de paiement n° %(numero)s') % {'numero': p.numero}, st, marge=11 * mm)
    lbl = ParagraphStyle('lbl', parent=st.cell, textColor=GRIS_MOYEN, fontSize=9)
    val = ParagraphStyle('val', parent=st.cell_b, fontSize=9)
    lignes = [(_('Professeur'), str(prof)), (_('Statut'), prof.get_statut_display()),
              (_('Période concernée'), p.periode or '—'), (_('Date du paiement'), f'{p.date:%d/%m/%Y}'),
              (_('Mode'), p.get_mode_display() + (_(' — réf. %(reference)s') % {'reference': p.reference} if p.reference else ''))]
    t = Table([[st.p(a, lbl), st.p(b, val)] for a, b in lignes], colWidths=[40 * mm, doc.width - 40 * mm])
    t.setStyle(TableStyle([('LINEBELOW', (0, 0), (-1, -1), 0.4, GRIS_LIGNE), ('LEFTPADDING', (0, 0), (-1, -1), 0)]))
    encadre = Table([
        [st.p(montant(p.montant), ParagraphStyle('m', parent=st.cell_b, fontSize=20, leading=24, alignment=TA_CENTER))],
        [st.p(en_lettres(p.montant), ParagraphStyle('ml', parent=st.cell, alignment=TA_CENTER, textColor=GRIS_MOYEN))],
    ], colWidths=[doc.width])
    encadre.setStyle(TableStyle([('BOX', (0, 0), (-1, -1), 1.4, BLEU), ('BACKGROUND', (0, 0), (-1, -1), BLEU_PALE),
                                 ('TOPPADDING', (0, 0), (-1, 0), 9), ('BOTTOMPADDING', (0, -1), (-1, -1), 9)]))
    sign = ParagraphStyle('sg', parent=st.cell, alignment=TA_CENTER, textColor=GRIS_MOYEN)
    signatures = Table([['', '', ''], [st.p(_('Le professeur'), sign), '', st.p(_('La direction'), sign)]],
                       colWidths=[48 * mm, doc.width - 96 * mm, 48 * mm], rowHeights=[16 * mm, None])
    signatures.setStyle(TableStyle([('LINEBELOW', (0, 0), (0, 0), 0.6, GRIS_MOYEN), ('LINEBELOW', (2, 0), (2, 0), 0.6, GRIS_MOYEN)]))
    histoire = [
        st.p(_('BON DE PAIEMENT'), ParagraphStyle('t', parent=st.cell_b, alignment=TA_CENTER, fontSize=11, textColor=GRIS_MOYEN)),
        Spacer(1, 5 * mm), t, Spacer(1, 6 * mm), encadre, Spacer(1, 5 * mm),
        _tuiles(st, doc.width, [(_('Heures cumulées'), _('%(total_heures)s h') % {'total_heures': format(prof.total_heures, 'g')}, None, _('à %(taux_horaire)s / h') % {'taux_horaire': montant(prof.taux_horaire)}),
                                (_('Déjà versé'), montant(prof.total_paye), None, None),
                                (_('Reste dû'), montant(max(prof.solde, 0)), None, None)]),
        Spacer(1, 6 * mm), KeepTogether(signatures),
    ]
    _construire(doc, histoire, entete, st)
    return buffer.getvalue()


# --------------------------------------------------------------------------- Emploi du temps

def emploi_du_temps(titre, creneaux, etab, par_prof=False):
    st = Styles()
    entete = EnTete(etab)
    buffer = BytesIO()
    doc = _document(buffer, landscape(A4), entete, titre, st)
    plages = sorted({(c.heure_debut, c.heure_fin) for c in creneaux})
    jours = list(Jour.choices)
    largeur_h = 24 * mm
    lc = (doc.width - largeur_h) / len(jours)
    data = [[st.p(_('Horaire'), st.head)] + [st.p(lib, st.head) for _j, lib in jours]]
    style = _style_tableau() + [('VALIGN', (0, 1), (-1, -1), 'TOP'), ('GRID', (1, 1), (-1, -1), 0.4, GRIS_LIGNE)]
    cours = ParagraphStyle('cours', parent=st.cell_b, textColor=BLEU_FONCE)
    for i, (debut, fin) in enumerate(plages, start=1):
        ligne = [st.p(f'{debut:%H:%M} – {fin:%H:%M}', st.cell_b)]
        for j, _lib in jours:
            c = next((c for c in creneaux if c.jour == j and (c.heure_debut, c.heure_fin) == (debut, fin)), None)
            if c:
                sous = f'{c.enseignement.classe}' if par_prof else f'{c.enseignement.professeur or "—"}'
                ligne.append([st.p(str(c.enseignement.matiere), cours),
                              st.p(sous + (f' · {c.salle}' if c.salle else ''), st.small)])
                style.append(('BACKGROUND', (j, i), (j, i), BLEU_PALE))
            else:
                ligne.append('')
        data.append(ligne)
    histoire = _titre(st, titre, _('%(creneaux)s cours par semaine · %(valeur)s heure(s)') % {'creneaux': len(creneaux), 'valeur': format(sum(c.duree_heures for c in creneaux), 'g')})
    if plages:
        t = Table(data, colWidths=[largeur_h] + [lc] * len(jours), repeatRows=1)
        t.setStyle(TableStyle(style))
        histoire.append(t)
    else:
        histoire.append(st.p(_('Aucun cours programmé.'), st.sous_titre))
    _construire(doc, histoire, entete, st)
    return buffer.getvalue()


# --------------------------------------------------------------------------- Planning de composition

def planning_composition(comp, epreuves):
    st = Styles()
    entete = EnTete(comp.etablissement)
    buffer = BytesIO()
    doc = _document(buffer, landscape(A4), entete, _('Planning des compositions'), st)
    periode = dict(comp.etablissement.periodes).get(comp.periode, '')
    histoire = _titre(st, str(comp), _('%(periode)s · du %(date_debut)s au %(date_fin)s · notes reportées en %(colonne)s') % {'periode': periode, 'date_debut': format(comp.date_debut, '%d/%m/%Y'), 'date_fin': format(comp.date_fin, '%d/%m/%Y'), 'colonne': comp.get_colonne_display().lower()})
    entetes = [_('Date'), _('Horaire'), _('Classe'), _('Matière'), _('Salle'), _('Surveillant(s)')]
    largeurs = [34 * mm, 28 * mm, 26 * mm, 50 * mm, 26 * mm, 0]
    largeurs[-1] = doc.width - sum(largeurs)
    data = [[st.p(h, st.head) for h in entetes]]
    style = _style_tableau()
    for i, e in enumerate(epreuves, start=1):
        data.append([
            st.p(dateformat.format(e.date, 'l d/m').capitalize(), st.cell_b),
            st.p(f'{e.heure_debut:%H:%M} – {e.heure_fin:%H:%M}'), st.p(e.enseignement.classe, st.cell_b),
            st.p(str(e.enseignement.matiere)), st.p(e.salle or '—'),
            st.p(', '.join(str(p) for p in e.surveillants.all()) or '—', st.small),
        ])
        if i % 2 == 0:
            style.append(('BACKGROUND', (0, i), (-1, i), GRIS_FOND))
    if epreuves:
        t = Table(data, colWidths=largeurs, repeatRows=1)
        t.setStyle(TableStyle(style))
        histoire.append(t)
    else:
        histoire.append(st.p(_('Aucune épreuve programmée.'), st.sous_titre))
    _construire(doc, histoire, entete, st)
    return buffer.getvalue()
