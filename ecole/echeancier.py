"""Échéancier des élèves : tranches issues de la grille, réductions, imputation des paiements, retards.

- Tout élève a des tranches : celles de la grille de sa classe, sinon une tranche unique (montant de sa
  fiche, échéance en fin d'année scolaire), ou un échéancier personnalisé.
- Les réductions (fratrie, bourses, remises) se répartissent sur les tranches pas encore réglées,
  au prorata de leur montant : les tranches déjà payées ne bougent pas.
- Les paiements de scolarité sont imputés dans l'ordre des tranches ; une tranche dont la date
  est passée et qui n'est pas entièrement réglée met l'élève « en retard ».
"""

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from .models import TRANCHES, Echeance, Eleve, GrilleFrais, ImputationPaiement, Motif, numero_international


# --------------------------------------------------------------------------- Grille et réductions

def cle_texte(texte):
    """Forme de comparaison d'un nom de classe ou d'établissement : « 6ème A » = « 6eme a » = « 6EMEA »."""
    texte = unicodedata.normalize('NFKD', str(texte or '')).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]', '', texte)


def trouver_grille(etablissement, classe):
    """Grille (avec tranches) de la classe dans l'établissement (objet ou nom), sans tenir compte
    des majuscules, accents ni espaces."""
    if not classe or not etablissement:
        return None
    grilles = GrilleFrais.objects.select_related('etablissement').prefetch_related('tranches').filter(tranches__isnull=False)
    if isinstance(etablissement, str):
        cle_etab = cle_texte(etablissement)
        grilles = [g for g in grilles.distinct() if cle_texte(g.etablissement.nom) == cle_etab]
    else:
        grilles = grilles.filter(etablissement=etablissement).distinct()
    cle_classe = cle_texte(classe)
    return next((g for g in grilles if cle_texte(g.classe) == cle_classe), None)


def grille_de(eleve):
    return trouver_grille(eleve.etablissement, eleve.classe)


def freres_et_soeurs(eleve):
    """Élèves du même établissement dont le parent a le même numéro (y compris l'élève), du plus ancien au plus récent."""
    numero = eleve.telephone_international
    if not numero:
        return [eleve]
    candidats = (Eleve.objects.filter(etablissement_id=eleve.etablissement_id)
                 .filter(models.Q(archive=False) | models.Q(pk=eleve.pk)).order_by('cree_le', 'pk'))
    return [e for e in candidats if e.telephone_international == numero]


def rang_fratrie(eleve):
    fratrie = freres_et_soeurs(eleve)
    return next((i for i, e in enumerate(fratrie, start=1) if e.pk == eleve.pk), 1)


def taux_fratrie(eleve):
    rang = rang_fratrie(eleve)
    etab = eleve.etablissement
    if rang == 2:
        return etab.reduction_fratrie_2
    if rang >= 3:
        return etab.reduction_fratrie_3
    return 0


def detail_reductions(eleve, brut):
    """[(libellé, montant)] des réductions de l'élève sur un total brut, plafonnées au total."""
    lignes = []
    taux = taux_fratrie(eleve)
    if taux:
        lignes.append((_('Fratrie (%(rang)se enfant) : %(taux)s %%') % {'rang': rang_fratrie(eleve), 'taux': taux},
                       round(brut * taux / 100)))
    for r in eleve.reductions.all():
        if r.porte_sur != 'scolarite':
            continue
        valeur = f'{r.valeur} %' if r.mode == 'pourcentage' else f'{r.valeur:,} F'.replace(',', ' ')
        libelle = f'{r.get_type_display()} ({valeur})' + (f' — {r.motif}' if r.motif else '')
        lignes.append((libelle, r.montant_sur(brut)))
    reste, plafonnees = brut, []
    for libelle, montant in lignes:
        m = min(montant, reste)
        plafonnees.append((libelle, m))
        reste -= m
    return plafonnees


def taux_fratrie_inscription(eleve):
    rang = rang_fratrie(eleve)
    etab = eleve.etablissement
    if rang == 2:
        return etab.reduction_inscription_2
    if rang >= 3:
        return etab.reduction_inscription_3
    return 0


def detail_reductions_inscription(eleve):
    """[(libellé, montant)] des réductions sur les frais d'inscription, plafonnées aux frais."""
    brut = eleve.frais_inscription
    if not brut:
        return []
    lignes = []
    taux = taux_fratrie_inscription(eleve)
    if taux:
        lignes.append((_('Fratrie (%(rang)se enfant) : %(taux)s %%') % {'rang': rang_fratrie(eleve), 'taux': taux},
                       round(brut * taux / 100)))
    for r in eleve.reductions.all():
        if r.porte_sur != 'inscription':
            continue
        valeur = f'{r.valeur} %' if r.mode == 'pourcentage' else f'{r.valeur:,} F'.replace(',', ' ')
        lignes.append((f'{r.get_type_display()} ({valeur})' + (f' — {r.motif}' if r.motif else ''), r.montant_sur(brut)))
    reste, plafonnees = brut, []
    for libelle, montant in lignes:
        m = min(montant, reste)
        plafonnees.append((libelle, m))
        reste -= m
    return plafonnees


def _repartir(total, poids):
    """Répartit un entier au prorata des poids (le reste d'arrondi va à la dernière part)."""
    somme = sum(poids)
    if not somme or not total:
        return [0] * len(poids)
    parts = [total * p // somme for p in poids]
    parts[-1] += total - sum(parts)
    return parts


@transaction.atomic
def appliquer_reductions(eleve):
    """Recalcule la réduction de chaque tranche et le montant total de la scolarité de l'élève."""
    lignes = list(eleve.echeances.order_by('numero'))
    if not lignes:
        recalculer_imputations(eleve)
        return
    brut = sum(l.montant_brut for l in lignes)
    total_reduction = sum(m for _l, m in detail_reductions(eleve, brut))
    paye = Eleve.objects.filter(pk=eleve.pk).avec_totaux().values_list('verse_tranches', flat=True)[0]

    # Tranches déjà entièrement réglées : leur montant ne change plus.
    reste_paye, gelees = paye, 0
    for ligne in lignes:
        if ligne.montant and reste_paye >= ligne.montant:
            reste_paye -= ligne.montant
            gelees += 1
        else:
            break
    ouvertes = lignes[gelees:]
    deja = sum(l.reduction for l in lignes[:gelees])
    a_repartir = max(0, total_reduction - deja)
    # Le net des tranches ouvertes ne peut pas descendre sous ce qui y est déjà versé.
    a_repartir = min(a_repartir, max(0, sum(l.montant_brut for l in ouvertes) - reste_paye))
    for ligne, part in zip(ouvertes, _repartir(a_repartir, [l.montant_brut for l in ouvertes])):
        part = min(part, ligne.montant_brut)
        if ligne.reduction != part:
            ligne.reduction = part
            ligne.save(update_fields=['reduction'])
    total = sum(l.montant for l in lignes)
    if eleve.montant_scolarite != total:
        eleve.montant_scolarite = total
        Eleve.objects.filter(pk=eleve.pk).update(montant_scolarite=total)
    recalculer_imputations(eleve)


def fin_annee_scolaire():
    """Date d'échéance par défaut d'une tranche unique : 30 juin de la fin de l'année scolaire."""
    from .models import Parametres

    annee = Parametres.courant().annee_scolaire
    fin = annee.split('-')[-1].strip()
    if fin.isdigit() and len(fin) == 4:
        return date(int(fin), 6, 30)
    aujourdhui = timezone.localdate()
    return date(aujourdhui.year + (1 if aujourdhui.month >= 7 else 0), 6, 30)


def montant_brut(eleve):
    """Montant de la scolarité avant réductions (somme des tranches), ou montant de la fiche."""
    lignes = list(eleve.echeances.all())
    return sum(l.montant_brut for l in lignes) if lignes else eleve.montant_scolarite


@transaction.atomic
def generer_echeancier(eleve, force=False, montant=None):
    """(Re)crée l'échéancier de l'élève, puis applique les réductions et recalcule les affectations.

    - classe avec grille : tranches de la grille ;
    - classe sans grille : une tranche unique (``montant`` saisi sur la fiche, échéance en fin d'année) ;
    - échéancier personnalisé : conservé tel quel (sauf ``force``).
    """
    if eleve.echeancier_personnalise and eleve.echeances.exists() and not force:
        appliquer_reductions(eleve)
        return
    grille = grille_de(eleve)
    if grille is None or not grille.tranches.exists():
        brut = montant if montant is not None else montant_brut(eleve)
        eleve.echeances.all().delete()
        if brut:
            Echeance.objects.create(eleve=eleve, numero=1, libelle=str(_('Tranche unique')), montant_brut=brut,
                                    echeance=fin_annee_scolaire())
        if force:
            Eleve.objects.filter(pk=eleve.pk).update(echeancier_personnalise=False)
            eleve.echeancier_personnalise = False
        appliquer_reductions(eleve)
        return
    if eleve.frais_inscription != grille.frais_inscription:
        eleve.frais_inscription = grille.frais_inscription
        Eleve.objects.filter(pk=eleve.pk).update(frais_inscription=grille.frais_inscription)
    eleve.echeances.all().delete()
    Echeance.objects.bulk_create([
        Echeance(eleve=eleve, numero=t.numero, libelle=t.libelle, montant_brut=t.montant, echeance=t.echeance)
        for t in grille.tranches.all()
    ])
    if force:
        Eleve.objects.filter(pk=eleve.pk).update(echeancier_personnalise=False)
        eleve.echeancier_personnalise = False
    appliquer_reductions(eleve)


def regenerer_classe(etablissement, classe):
    cle_classe = cle_texte(classe)
    for eleve in Eleve.objects.filter(etablissement=etablissement):
        if cle_texte(eleve.classe) == cle_classe:
            generer_echeancier(eleve)


def recalculer_fratrie(eleve):
    """Après une inscription, un changement de numéro ou un départ : recalcule toute la fratrie."""
    for e in freres_et_soeurs(eleve):
        appliquer_reductions(e)


# --------------------------------------------------------------------------- Imputation et retards

def paiements_scolarite(eleve):
    """Paiements de scolarité dans l'ordre d'affectation : date du paiement, puis ordre de saisie."""
    return eleve.paiements.filter(motif__in=TRANCHES).order_by('date', 'cree_le', 'pk')


@transaction.atomic
def recalculer_imputations(eleve):
    """Réaffecte tous les paiements de scolarité de l'élève à ses tranches et enregistre le détail.

    Chaque paiement remplit d'abord la plus ancienne tranche non soldée ; le surplus passe à la tranche
    suivante. Le montant versé n'est jamais modifié : ce qui dépasse le total des tranches est noté
    « Excédent ».
    """
    ImputationPaiement.objects.filter(eleve=eleve).delete()
    tranches = list(eleve.echeances.order_by('numero'))
    paye = [0] * len(tranches)
    total = sum(t.montant for t in tranches) if tranches else eleve.montant_scolarite
    verse, ordre, lignes = 0, 0, []
    for p in paiements_scolarite(eleve):
        reste_p = p.montant
        verse += p.montant
        for i, t in enumerate(tranches):
            if not reste_p:
                break
            manque = t.montant - paye[i]
            if manque <= 0:
                continue
            part = min(manque, reste_p)
            paye[i] += part
            reste_p -= part
            ordre += 1
            lignes.append(ImputationPaiement(
                eleve=eleve, paiement=p, echeance=t, ordre=ordre, libelle=str(t)[:80], date_echeance=t.echeance,
                montant_tranche=t.montant, montant=part, paye_tranche=paye[i], reste_tranche=t.montant - paye[i],
                reste_total=max(0, total - verse)))
        if reste_p:
            ordre += 1
            lignes.append(ImputationPaiement(
                eleve=eleve, paiement=p, ordre=ordre,
                libelle=str(_('Excédent') if tranches else _('Scolarité'))[:80],
                montant=reste_p, reste_total=max(0, total - verse)))
    ImputationPaiement.objects.bulk_create(lignes)

@dataclass
class LigneSituation:
    echeance: Echeance
    montant: int
    paye: int
    statut: str  # 'payee', 'partielle', 'en_cours', 'retard', 'a_venir'

    @property
    def reste(self):
        return self.montant - self.paye

    @property
    def statut_libelle(self):
        if self.statut == 'retard' and self.paye:
            return _('En retard — partiellement payée')
        return {'payee': _('Soldée'), 'partielle': _('Partiellement payée'), 'en_cours': _('En cours'),
                'retard': _('En retard'), 'a_venir': _('À venir')}[self.statut]


@dataclass
class Situation:
    lignes: list = field(default_factory=list)
    reste: int = 0
    aujourdhui: object = None

    @property
    def en_retard(self):
        return any(l.statut == 'retard' for l in self.lignes)

    @property
    def montant_en_retard(self):
        return sum(l.reste for l in self.lignes if l.statut == 'retard')

    @property
    def jours_de_retard(self):
        dates = [l.echeance.echeance for l in self.lignes if l.statut == 'retard']
        return (self.aujourdhui - min(dates)).days if dates else 0

    @property
    def prochaine(self):
        return next((l for l in self.lignes if l.reste and l.statut != 'retard'), None)

    @property
    def statut(self):
        """'solde', 'retard' ou 'a_jour' (rien d'échu n'est impayé)."""
        if self.reste <= 0:
            return 'solde'
        return 'retard' if self.en_retard else 'a_jour'

    @property
    def statut_libelle(self):
        return {'solde': _('Soldé'), 'retard': _('En retard'), 'a_jour': _('À jour')}[self.statut]


def situation(eleve, aujourdhui=None):
    aujourdhui = aujourdhui or timezone.localdate()
    reste_paye = eleve.total_verse
    sit = Situation(reste=eleve.reste_a_payer, aujourdhui=aujourdhui)
    en_cours_vu = False
    for e in eleve.echeances.all():
        paye = min(reste_paye, e.montant)
        reste_paye -= paye
        if paye >= e.montant:
            statut = 'payee'
        elif e.echeance < aujourdhui:
            statut = 'retard'
        elif paye:
            statut, en_cours_vu = 'partielle', True
        elif not en_cours_vu:
            statut, en_cours_vu = 'en_cours', True  # prochaine tranche attendue
        else:
            statut = 'a_venir'
        sit.lignes.append(LigneSituation(e, e.montant, paye, statut))
    return sit


def imputation_paiement(paiement):
    """[(libellé, montant)] : sur quelles tranches porte ce paiement (pour la facture)."""
    if paiement.motif == Motif.INSCRIPTION:
        return [(str(Motif.INSCRIPTION.label), paiement.montant)]
    lignes = [(_('Scolarité — %(tranche)s (échéance du %(date)s)') % {
        'tranche': i.libelle, 'date': i.date_echeance.strftime('%d/%m/%Y')} if i.date_echeance else i.libelle, i.montant)
        for i in paiement.imputations.all()]
    if lignes:
        return lignes
    eleve = paiement.eleve
    precedents = (eleve.paiements.filter(motif__in=TRANCHES)
                  .order_by('date', 'cree_le', 'pk'))
    avant = 0
    for p in precedents:
        if p.pk == paiement.pk:
            break
        avant += p.montant
    echeances = list(eleve.echeances.all())
    if not echeances:
        return [(_('Scolarité %(annee)s') % {'annee': _annee()}, paiement.montant)]
    lignes, debut, fin, borne = [], avant, avant + paiement.montant, 0
    for e in echeances:
        bas, haut = borne, borne + e.montant
        borne = haut
        part = max(0, min(fin, haut) - max(debut, bas))
        if part:
            lignes.append((_('Scolarité — %(tranche)s (échéance du %(date)s)') % {
                'tranche': e, 'date': e.echeance.strftime('%d/%m/%Y')}, part))
    surplus = paiement.montant - sum(m for _l, m in lignes)
    if surplus > 0:
        lignes.append((_('Scolarité'), surplus))
    return lignes


def _annee():
    from .models import Parametres

    return Parametres.courant().annee_scolaire


def eleves_par_situation(eleves, statut):
    """Filtre une liste d'élèves sur leur situation ('retard', 'a_jour', 'solde')."""
    return [e for e in eleves if e.situation.statut == statut]


def contacts_du_parent(eleve):
    """Numéros et e-mails à prévenir : contact principal puis autres contacts qui reçoivent les messages."""
    whatsapp, sms, emails = [], [], []
    for numero in [eleve.whatsapp_international]:
        if numero:
            whatsapp.append(numero)
    if eleve.telephone_international:
        sms.append(eleve.telephone_international)
    if eleve.email:
        emails.append(eleve.email)
    for c in eleve.contacts.filter(notifier=True):
        wa = numero_international(c.whatsapp) or numero_international(c.telephone)
        if wa and wa not in whatsapp:
            whatsapp.append(wa)
        tel = numero_international(c.telephone)
        if tel and tel not in sms:
            sms.append(tel)
        if c.email and c.email not in emails:
            emails.append(c.email)
    return {'whatsapp': whatsapp, 'sms': sms, 'email': emails}
