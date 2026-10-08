"""Import en masse des élèves depuis un fichier Excel (.xlsx) ou CSV : une fiche complète par ligne.

Chaque ligne peut tout contenir : identité, classe, parent principal et second parent, frais, réductions
(scolarité et inscription) et paiements déjà effectués (inscription, scolarité). L'import se fait en deux
temps : analyse et aperçu (rien n'est créé), puis confirmation.
"""

import csv
import hashlib
import io
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from .echeancier import cle_texte, generer_echeancier, recalculer_fratrie, trouver_grille
from .models import (
    ContactParent, Eleve, Etablissement, ImportFichier, ModePaiement, Motif, NatureParent, Paiement, ReductionEleve,
    Sexe,
)

MAX_LIGNES = 5000


@dataclass(frozen=True)
class Colonne:
    champ: str
    entete: str            # intitulé du modèle
    synonymes: tuple       # autres intitulés acceptés (comparés sans accents, espaces ni ponctuation)
    obligatoire: bool
    description: str
    exemple: str


def _c(champ, entete, synonymes, description, exemple, obligatoire=False):
    return Colonne(champ, entete, tuple(synonymes), obligatoire, description, exemple)


COLONNES = [
    # Élève
    _c('nom', 'Nom', ['nom', 'nomeleve', 'nomdeleleve'], "Nom de famille de l'élève.", 'DOSSOU', True),
    _c('prenom', 'Prénom(s)', ['prenom', 'prenoms', 'prenomeleve'], "Prénom(s) de l'élève.", 'Koffi Jean', True),
    _c('nom_prenom', 'Nom et prénom', ['nometprenom', 'nometprenoms', 'nomprenom', 'nomprenoms', 'nomsetprenoms',
                                       'nomcomplet', 'eleve', 'nometprenomdeleleve'],
       'À la place de Nom + Prénom(s) : le premier mot est le nom, la suite le(s) prénom(s).', 'DOSSOU Koffi Jean'),
    _c('sexe', 'Sexe', ['sexe', 'genre'], 'M ou F (Masculin / Féminin, Garçon / Fille acceptés).', 'M'),
    _c('date_naissance', 'Date de naissance', ['datedenaissance', 'datenaissance', 'nele', 'nee', 'naissance'],
       'JJ/MM/AAAA.', '14/03/2014'),
    _c('lieu_naissance', 'Lieu de naissance', ['lieudenaissance', 'lieunaissance'], 'Ville ou commune.', 'Cotonou'),
    _c('classe', 'Classe', ['classe', 'cycle'],
       'Classe, écrite comme dans « Frais par classe » (majuscules, accents et espaces ignorés).', 'CM2', True),
    _c('niveau', 'Niveau', ['niveau', 'annee', 'annees', 'anneeetude', 'souscalsse', 'sousclasse'],
       'Facultatif : complète la classe (« Maternelle » + « 1 » = « Maternelle 1 » ; « Primaire » + « CE1 » = « CE1 »).',
       ''),
    _c('etablissement', 'Établissement', ['etablissement', 'ecole', 'college', 'lycee'],
       "Facultatif s'il n'y a qu'un établissement. Un nouveau nom crée l'établissement.", 'CS Romaric'),
    _c('educ_master', 'N° Educ Master', ['educmaster', 'numeroeducmaster', 'noeducmaster', 'matricule'], '', ''),
    _c('residence', 'Résidence', ['residence', 'adresse', 'quartier'], 'Quartier, ville.', 'Akpakpa, Cotonou'),
    # Parent principal
    _c('parent', 'Parent', ['parent', 'parenttuteur', 'tuteur', 'nomduparent', 'nomparent', 'parent1', 'nomparent1',
                            'nometprenomduparent', 'nomprenomduparent', 'nometprenomsduparent', 'parents'],
       'Nom du parent ou tuteur principal (recommandé).', 'DOSSOU Marc'),
    _c('nature_parent', 'Lien', ['lien', 'nature', 'natureduparent', 'lienparente', 'lienparent', 'lienparent1'],
       'Père, Mère, Tuteur, Tutrice ou Autre.', 'Père'),
    _c('telephone', 'Téléphone', ['telephone', 'tel', 'contact', 'telephoneparent', 'telephoneparent1',
                                  'telephoneduparent', 'contactparent', 'telparent', 'numerodetelephone'],
       'Numéro du parent principal (recommandé : factures, rappels et frères et sœurs en dépendent).', '01 97 00 00 00'),
    _c('whatsapp', 'WhatsApp', ['whatsapp', 'numerowhatsapp', 'whatsappparent', 'whatsappparent1'],
       'Vide si c’est le même numéro que le téléphone.', ''),
    _c('email', 'Email', ['email', 'mail', 'courriel', 'emailparent', 'emailparent1'], '', 'parent@exemple.bj'),
    # Second parent
    _c('parent2', 'Parent 2', ['parent2', 'nomparent2', 'secondparent', 'autreparent'],
       'Second parent ou tuteur (facultatif).', 'DOSSOU Awa'),
    _c('lien2', 'Lien parent 2', ['lienparent2', 'lien2'], 'Père, Mère, Tuteur, Tutrice ou Autre.', 'Mère'),
    _c('telephone2', 'Téléphone parent 2', ['telephoneparent2', 'telephone2', 'tel2', 'telparent2'], '', '96 00 00 00'),
    _c('whatsapp2', 'WhatsApp parent 2', ['whatsappparent2', 'whatsapp2'], '', ''),
    _c('email2', 'Email parent 2', ['emailparent2', 'email2', 'mailparent2'], '', ''),
    # Frais et réductions
    _c('montant_scolarite', 'Montant scolarité', ['montant', 'scolarite', 'montantscolarite', 'frais', 'montanttotal',
                                                  'fraisscolarite', 'fraisdescolarite', 'fraisscolaires', 'fraisdescolarite',
                                                  'scolariteannuelle', 'montantdelascolarite', 'contribution'],
       'Montant de la scolarité de l’élève. S’il est vide, celui de la classe (« Frais par classe ») s’applique.',
       '120000'),
    _c('frais_inscription', "Frais d'inscription", ['fraisinscription', 'fraisdinscription', 'inscription',
                                                     'droitsinscription'],
       'S’il est vide, celui de la classe s’applique.', '15000'),
    _c('reduction_scolarite', 'Réduction scolarité', ['reductionscolarite', 'remisescolarite', 'bourse', 'reduction'],
       '« 10 % » pour un pourcentage, « 20000 » pour un montant.', '10 %'),
    _c('reduction_inscription', 'Réduction inscription', ['reductioninscription', 'remiseinscription'],
       '« 50 % » ou un montant ; « 100 % » = inscription offerte.', ''),
    _c('type_reduction', 'Type de réduction', ['typereduction', 'typederemise', 'typedereduction'],
       'Bourse ou Remise (Remise par défaut).', 'Bourse'),
    _c('motif_reduction', 'Motif réduction', ['motifreduction', 'motifremise', 'motif'], '', 'Bourse d’excellence'),
    # Paiements déjà effectués
    _c('detail_paiements', 'Détail des paiements', ['detaildespaiements', 'detailpaiements', 'detaildepaiement',
                                                     'paiements', 'historiquedespaiements', 'versements'],
       'Une ligne par paiement : « 07/09/2026 - Paiement inscription : 5 000 CFA ». Le mot « inscription » range le '
       'paiement dans les frais d’inscription, sinon il va sur la scolarité.',
       '7/9/2026 - Paiement 1er trimestre : 32 000 CFA'),
    _c('total_paye', 'Total payé', ['totalpaye', 'montanttotalpaye', 'totalverse', 'dejapayetotal'],
       'Utilisé seulement sans détail des paiements : l’inscription est considérée payée en premier.', ''),
    _c('inscription_versee', 'Inscription déjà versée', ['inscriptionversee', 'inscriptiondejaversee',
                                                          'fraisinscriptionverses', 'fraisdinscriptionverses',
                                                          'inscriptionpayee'],
       'Montant des frais d’inscription déjà payés (crée un paiement avec facture).', '15000'),
    _c('scolarite_versee', 'Scolarité déjà versée', ['scolariteversee', 'scolaritedejaversee', 'versescolarite',
                                                      'dejaverse', 'montantverse', 'montantpaye'],
       'Montant déjà payé sur la scolarité (affecté automatiquement aux tranches).', '40000'),
    _c('date_versement', 'Date du versement', ['dateversement', 'datedeversement', 'datepaiement', 'datedupaiement'],
       'JJ/MM/AAAA ; aujourd’hui si vide.', '15/09/2026'),
    _c('mode_versement', 'Mode de paiement', ['modepaiement', 'modedepaiement', 'modeversement', 'mode'],
       'Espèces, Mobile Money, Virement ou Chèque (Espèces par défaut).', 'Mobile Money'),
    _c('reference_versement', 'Référence du paiement', ['reference', 'referencepaiement', 'numerotransaction',
                                                         'notransaction'],
       'N° de transaction Mobile Money, de chèque…', 'MM-123456'),
    _c('rappels', 'Rappels automatiques', ['rappels', 'rappelsautomatiques', 'suivi'], 'Oui ou Non (Oui par défaut).',
       'Oui'),
]
PAR_CHAMP = {c.champ: c for c in COLONNES}
OBLIGATOIRES = [c.champ for c in COLONNES if c.obligatoire]  # Nom, Prénom(s), Classe (ou « Nom et prénom »)
ENTETES_MODELE = [c.entete for c in COLONNES]
GROUPES = [
    ('Élève', ['nom', 'prenom', 'nom_prenom', 'sexe', 'date_naissance', 'lieu_naissance', 'classe', 'niveau',
               'etablissement', 'educ_master', 'residence']),
    ('Parent principal', ['parent', 'nature_parent', 'telephone', 'whatsapp', 'email']),
    ('Second parent', ['parent2', 'lien2', 'telephone2', 'whatsapp2', 'email2']),
    ('Frais et réductions', ['montant_scolarite', 'frais_inscription', 'reduction_scolarite', 'reduction_inscription',
                             'type_reduction', 'motif_reduction']),
    ('Paiements déjà effectués', ['detail_paiements', 'total_paye', 'inscription_versee', 'scolarite_versee',
                                  'date_versement', 'mode_versement',
                                  'reference_versement', 'rappels']),
]


# --------------------------------------------------------------------------- Lecture des valeurs

def _cle(texte):
    texte = unicodedata.normalize('NFKD', str(texte or '')).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]', '', texte)


def _cle_entete(texte):
    """Intitulé de colonne comparable : « Frais Scolarité (CFA) » -> « fraisscolarite »."""
    return re.sub(r'(en)?f?cfa$', '', _cle(texte))


RE_PAIEMENT = re.compile(
    r'(?P<date>\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})\s*[-–—:]?\s*(?P<libelle>.*?)\s*:\s*(?P<montant>\d[\d\s\u00a0\u202f.,]*)\s*(f?cfa|f)?\s*$',
    re.IGNORECASE)


def _detail_paiements(texte):
    """« 1. 7/9/2026 - Paiement inscription : 2 000 CFA » (une ligne par paiement)
    -> ([(date, motif, montant, libellé)], [lignes illisibles])."""
    paiements, illisibles = [], []
    for ligne in str(texte or '').replace('\r', '\n').split('\n'):
        ligne = re.sub(r'^\s*\d+\s*[.)-]\s+', '', ligne.strip())  # numéro « 1. » en tête
        if not ligne:
            continue
        m = RE_PAIEMENT.search(ligne)
        try:
            montant = _montant(m.group('montant')) if m else None
            jour = _date(m.group('date')) if m else None
        except ValueError:
            montant = jour = None
        if not (montant and jour):
            illisibles.append(ligne)
            continue
        motif = Motif.INSCRIPTION if 'inscription' in _cle(m.group('libelle')) else Motif.SCOLARITE
        paiements.append((jour, motif, montant, m.group('libelle').strip(' -')[:150]))
    return paiements, illisibles


def _cellule(valeur):
    if valeur is None:
        return ''
    if isinstance(valeur, float) and valeur.is_integer():
        return str(int(valeur))
    if isinstance(valeur, (datetime, date)):
        return valeur
    return str(valeur).strip()


def _montant(valeur):
    if valeur in (None, ''):
        return None
    if isinstance(valeur, (int, float)):
        return int(valeur)
    texte = re.split(r'[.,](?=\d{1,2}$)', str(valeur).strip())[0]  # « 60 000,00 » -> « 60 000 »
    chiffres = re.sub(r'\D', '', texte)
    return int(chiffres) if chiffres else None


def _date(valeur):
    if valeur in (None, ''):
        return None
    if isinstance(valeur, datetime):
        return valeur.date()
    if isinstance(valeur, date):
        return valeur
    texte = str(valeur).strip()
    for fmt in ('%d/%m/%Y', '%d-%m-%Y', '%d.%m.%Y', '%Y-%m-%d', '%d/%m/%y', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(texte, fmt).date()
        except ValueError:
            continue
    raise ValueError(texte)


def _reduction(valeur):
    """« 10 % » -> ('pourcentage', 10) ; « 20000 » -> ('montant', 20000) ; vide -> None."""
    if valeur in (None, ''):
        return None
    if isinstance(valeur, float) and 0 < valeur < 1:  # cellule Excel au format pourcentage (0,1 = 10 %)
        return ('pourcentage', round(valeur * 100))
    texte = str(valeur).strip()
    n = _montant(texte.replace('%', ''))
    if not n:
        raise ValueError(texte)
    if '%' in texte:
        if n > 100:
            raise ValueError(texte)
        return ('pourcentage', n)
    return ('montant', n)


def _choix(valeur, choix, alias=None):
    v = _cle(valeur)
    if not v:
        return ''
    for code, libelle in choix:
        if v in (_cle(code), _cle(libelle)):
            return code
    for code, mots in (alias or {}).items():
        if v in mots:
            return code
    raise ValueError(str(valeur))


SEXE_ALIAS = {Sexe.MASCULIN: ('m', 'masculin', 'garcon', 'homme', 'h'), Sexe.FEMININ: ('f', 'feminin', 'fille', 'femme')}
MODE_ALIAS = {
    ModePaiement.ESPECES: ('espece', 'especes', 'cash', 'liquide'),
    ModePaiement.MOBILE_MONEY: ('mobilemoney', 'momo', 'mtn', 'moov', 'mtnmomo', 'moovmoney', 'celtiis'),
    ModePaiement.VIREMENT: ('virement', 'virementbancaire', 'banque'),
    ModePaiement.CHEQUE: ('cheque', 'cheques'),
}
OUI_NON_ALIAS = {'oui': ('o', 'yes', '1', 'vrai', 'x'), 'non': ('n', 'no', '0', 'faux')}


def lire_lignes(fichier):
    """Renvoie la liste des lignes (listes de cellules) d'un fichier .xlsx ou .csv."""
    nom = (getattr(fichier, 'name', '') or '').lower()
    contenu = fichier.read()
    if nom.endswith('.xlsx') or contenu[:2] == b'PK':
        classeur = load_workbook(io.BytesIO(contenu), read_only=True, data_only=True)
        feuille = classeur['Élèves'] if 'Élèves' in classeur.sheetnames else classeur.worksheets[0]
        lignes = []
        for r in feuille.iter_rows(values_only=True, max_col=60):
            lignes.append(list(r))
            if len(lignes) > MAX_LIGNES + 1:
                break
        return lignes
    for encodage in ('utf-8-sig', 'cp1252', 'latin-1'):
        try:
            texte = contenu.decode(encodage)
            break
        except UnicodeDecodeError:
            continue
    try:
        dialecte = csv.Sniffer().sniff(texte[:4096], delimiters=';,\t')
    except csv.Error:
        class dialecte(csv.excel):
            delimiter = ';' if texte.count(';') > texte.count(',') else ','
    return [r for r in csv.reader(io.StringIO(texte), dialecte)]


def empreinte(lignes):
    """Empreinte du contenu du fichier (indépendante de son nom) : le même tableau donne la même empreinte."""
    lignes = [l for l in lignes if any(c not in (None, '') for c in l)]
    return hashlib.sha256(json.dumps(lignes, default=str, ensure_ascii=False).encode()).hexdigest()


def import_precedent(lignes):
    return ImportFichier.objects.select_related('importe_par').filter(empreinte=empreinte(lignes)).first()


def _cle_nom(nom, prenom):
    """« DOSSOU Koffi Jean » = « Koffi Jean DOSSOU » = « dossou koffi-jean » (ordre, accents, casse ignorés)."""
    return ''.join(sorted(cle_texte(m) for m in re.split(r'[\s\-]+', f'{nom} {prenom}') if cle_texte(m)))


class Existants:
    """Élèves déjà dans la base (archivés compris), pour qu'une même personne ne soit jamais importée deux fois."""

    def __init__(self):
        self.par_nom, self.par_educ = {}, {}
        for e in Eleve.objects.select_related('etablissement', 'import_fichier', 'import_fichier__importe_par'):
            self.ajouter(e)

    def ajouter(self, e):
        self.par_nom.setdefault(_cle_nom(e.nom, e.prenom), []).append(e)
        if cle_texte(e.educ_master):
            self.par_educ[cle_texte(e.educ_master)] = e

    def trouver(self, d):
        educ = cle_texte(d.get('educ_master'))
        if educ and educ in self.par_educ:
            return self.par_educ[educ]
        for e in self.par_nom.get(_cle_nom(d['nom'], d['prenom']), []):
            ecoles = {cle_texte(e.etablissement.nom), cle_texte(d['etablissement'])}
            if len(ecoles) > 1 and cle_texte(NEANT) not in ecoles:
                continue  # homonyme dans un autre établissement
            if d.get('date_naissance') and e.date_naissance and e.date_naissance.isoformat() != d['date_naissance']:
                continue  # homonyme (date de naissance différente)
            if educ and cle_texte(e.educ_master) and cle_texte(e.educ_master) != educ:
                continue  # homonyme (n° Educ Master différent)
            return e
        return None


def message_deja(e):
    valeurs = {'eleve': f'{e.nom} {e.prenom}', 'classe': e.classe, 'ecole': e.etablissement.nom}
    if e.import_fichier_id:
        f = e.import_fichier
        message = _('Déjà dans la base : %(eleve)s (%(classe)s, %(ecole)s), importé(e) le %(date)s par %(par)s.') % {
            **valeurs, 'date': timezone.localtime(f.importe_le).strftime('%d/%m/%Y %H:%M'),
            'par': f.importe_par.get_full_name() or f.importe_par.username if f.importe_par else '—'}
    else:
        message = _('Déjà dans la base : %(eleve)s (%(classe)s, %(ecole)s), enregistré(e) le %(date)s.') % {
            **valeurs, 'date': timezone.localtime(e.cree_le).strftime('%d/%m/%Y')}
    if e.archive:
        message += ' ' + _('(élève archivé)')
    return message + ' ' + _('Non importé une seconde fois.')


# --------------------------------------------------------------------------- Analyse

@dataclass
class Rapport:
    erreurs: list = field(default_factory=list)         # [(n° de ligne, message)]
    ignores: list = field(default_factory=list)         # [(n° de ligne, message)] : lignes vides, doublons du fichier
    deja: list = field(default_factory=list)            # [(n° de ligne, message)] : élèves déjà dans la base
    avertissements: list = field(default_factory=list)  # [(n° de ligne, message)] : informations, sans blocage
    a_creer: list = field(default_factory=list)         # [dict] sérialisable (dates en ISO)
    resultats: list = field(default_factory=list)       # une entrée par ligne : statut ok / erreur / deja / ignore
    colonnes_lues: list = field(default_factory=list)   # intitulés reconnus
    colonnes_ignorees: list = field(default_factory=list)
    ligne_entetes: int = 1
    sans_contact: int = 0  # élèves sans téléphone ni e-mail de parent
    sans_etablissement: int = 0  # élèves rangés dans l'établissement « Néant »
    crees: int = 0

    @property
    def nb_erreurs(self):
        return sum(1 for r in self.resultats if r['statut'] == 'erreur')


def _associer_colonnes(entetes):
    index = {}
    cles = [_cle_entete(c) for c in entetes]
    for col in COLONNES:
        noms = {_cle_entete(col.entete), *col.synonymes}
        for i, e in enumerate(cles):
            if e and e in noms and col.champ not in index and i not in index.values():
                index[col.champ] = i
    return index


def _trouver_entetes(lignes):
    """Ligne d'en-tête : celle (parmi les 10 premières) qui reconnaît le plus de colonnes (un titre peut précéder)."""
    meilleure, score = 0, -1
    for i, ligne in enumerate(lignes[:10]):
        n = len(_associer_colonnes(ligne))
        if n > score:
            meilleure, score = i, n
    return meilleure


def _composer_classe(classe, niveau):
    classe = ' '.join(str(classe or '').split())
    niveau = ' '.join(str(niveau or '').split())
    if not niveau:
        return classe
    if niveau.isdigit():  # « Maternelle » + « 1 »
        return f'{classe} {niveau}'.strip()
    return niveau  # « Primaire » + « CE1 » -> « CE1 »


def _montants_par_classe(lignes, index):
    """Pour chaque classe du fichier, le montant le plus fréquent (scolarité, inscription) : sert à compléter
    une ligne dont le montant est vide."""
    from collections import Counter

    compte = {}
    for ligne in lignes:
        val = lambda c: ligne[index[c]] if c in index and index[c] < len(ligne) else None  # noqa: E731
        cle = cle_texte(_composer_classe(_cellule(val('classe')), _cellule(val('niveau'))))
        if not cle:
            continue
        for champ in ('montant_scolarite', 'frais_inscription'):
            m = _montant(val(champ))
            if m:
                compte.setdefault((cle, champ), Counter())[m] += 1
    return {k: c.most_common(1)[0][0] for k, c in compte.items()}


NEANT = 'Néant'


def _analyser_ligne(n, brut, etabs, aujourdhui, rapport, etab_defaut='', montants_classe=None):
    """Renvoie (données à créer, [erreurs], [avertissements]) pour une ligne du fichier.

    Rien ne bloque l'import : une information absente devient « Néant » et une valeur illisible est laissée
    vide ; chaque cas est signalé (en orange) pour être complété ensuite sur la fiche de l'élève.
    """
    val = {c: _cellule(v) for c, v in brut.items()}
    infos = []
    avertir = infos.append

    # Nom / prénom (deux colonnes, ou une seule « Nom et prénom »)
    nom, prenom = str(val.get('nom') or '').strip(), str(val.get('prenom') or '').strip()
    if not nom and val.get('nom_prenom'):
        mots = str(val['nom_prenom']).split()
        nom, prenom = mots[0], ' '.join(mots[1:])
    if not nom:
        nom = prenom or NEANT
        prenom = '' if nom == prenom else prenom
    if not prenom:
        prenom = NEANT
        avertir(_('Prénom absent : « Néant ».'))
    classe_saisie = _composer_classe(val.get('classe'), val.get('niveau'))
    if not classe_saisie:
        classe_saisie = NEANT
        avertir(_('Classe absente : « Néant » (à corriger sur la fiche).'))

    # Établissement et classe
    nom_etab = ' '.join(str(val.get('etablissement') or etab_defaut or '').split())
    if not nom_etab:
        if len(etabs) == 1:
            nom_etab = etabs[0].nom
        else:
            nom_etab = NEANT  # signalé une seule fois pour tout le fichier (rapport.sans_etablissement)
    etab = next((e for e in etabs if cle_texte(e.nom) == cle_texte(nom_etab)), None)
    if etab is None and nom_etab != NEANT:
        avertir(_('Nouvel établissement « %(nom)s » : il sera créé.') % {'nom': nom_etab})
    elif etab:
        nom_etab = etab.nom
    grille = trouver_grille(nom_etab, classe_saisie) if classe_saisie != NEANT else None
    classe = grille.classe if grille else classe_saisie

    # Identité
    try:
        sexe = _choix(val.get('sexe'), Sexe.choices, SEXE_ALIAS)
    except ValueError:
        avertir(_('Sexe « %(v)s » non reconnu : laissé vide.') % {'v': val.get('sexe')})
        sexe = ''
    try:
        naissance = _date(val.get('date_naissance'))
        if naissance and naissance >= aujourdhui:
            avertir(_('Date de naissance dans le futur : laissée vide.'))
            naissance = None
    except ValueError:
        avertir(_('Date de naissance « %(v)s » illisible : laissée vide.') % {'v': val.get('date_naissance')})
        naissance = None

    # Parents
    natures = {}
    for suffixe, champ_lien in (('', 'nature_parent'), ('2', 'lien2')):
        try:
            natures[suffixe] = _choix(val.get(champ_lien), NatureParent.choices)
        except ValueError:
            avertir(_('Lien « %(v)s » non reconnu : laissé vide.') % {'v': val.get(champ_lien)})
            natures[suffixe] = ''
        for champ in ('telephone', 'whatsapp'):
            numero = str(val.get(champ + suffixe) or '')
            if numero and len(re.sub(r'\D', '', numero)) < 8:
                avertir(_('Numéro « %(v)s » trop court : à vérifier.') % {'v': numero})
        courriel = str(val.get('email' + suffixe) or '')
        if courriel:
            try:
                validate_email(courriel)
            except ValidationError:
                avertir(_('E-mail « %(v)s » invalide : laissé vide.') % {'v': courriel})
                val['email' + suffixe] = ''
    parent2 = str(val.get('parent2') or '')
    if (val.get('telephone2') or val.get('whatsapp2') or val.get('email2')) and not parent2:
        parent2 = NEANT
    sans_contact = not val.get('telephone') and not val.get('email')
    parent = str(val.get('parent') or '') or NEANT
    telephone = str(val.get('telephone') or '') or NEANT

    # Frais
    montant_fichier = _montant(brut.get('montant_scolarite'))
    insc_fichier = _montant(brut.get('frais_inscription'))
    if not grille and montants_classe:
        cle_classe = cle_texte(classe)
        if montant_fichier is None and (cle_classe, 'montant_scolarite') in montants_classe:
            montant_fichier = montants_classe[(cle_classe, 'montant_scolarite')]
            avertir(_('Scolarité vide : %(m)s repris des autres élèves de la classe %(classe)s dans le fichier.') % {
                'm': montant_fichier, 'classe': classe})
        if insc_fichier is None and (cle_classe, 'frais_inscription') in montants_classe:
            insc_fichier = montants_classe[(cle_classe, 'frais_inscription')]
    montant_impose = None
    if grille:
        montant = montant_fichier if montant_fichier is not None else grille.total
        frais_insc = insc_fichier if insc_fichier is not None else grille.frais_inscription
        if montant_fichier not in (None, grille.total):
            montant_impose = montant_fichier
            avertir(_('Scolarité du fichier (%(f)s) différente de celle de la classe (%(c)s) : les tranches de la '
                      'classe sont gardées, avec des montants ajustés au fichier.') % {'f': montant_fichier,
                                                                                       'c': grille.total})
    else:
        montant, frais_insc = montant_fichier, insc_fichier or 0
        if montant is None:
            montant = 0
            avertir(_('Montant de la scolarité absent : 0 (à compléter sur la fiche ou dans « Frais par classe »).'))

    # Réductions
    reductions, type_red = [], 'remise'
    if val.get('type_reduction'):
        try:
            type_red = _choix(val.get('type_reduction'), [('bourse', 'Bourse'), ('remise', 'Remise')]) or 'remise'
        except ValueError:
            avertir(_('Type de réduction « %(v)s » non reconnu : « Remise ».') % {'v': val.get('type_reduction')})
    for champ, porte_sur in (('reduction_scolarite', 'scolarite'), ('reduction_inscription', 'inscription')):
        try:
            red = _reduction(brut.get(champ))
        except ValueError:
            avertir(_('Réduction « %(v)s » illisible : non appliquée.') % {'v': val.get(champ)})
            continue
        if red:
            reductions.append({'porte_sur': porte_sur, 'mode': red[0], 'valeur': red[1], 'type': type_red,
                               'motif': str(val.get('motif_reduction') or '')[:150]})

    # Paiements déjà effectués
    try:
        mode = _choix(val.get('mode_versement'), ModePaiement.choices, MODE_ALIAS) or ModePaiement.ESPECES
    except ValueError:
        avertir(_('Mode de paiement « %(v)s » non reconnu : « Espèces ».') % {'v': val.get('mode_versement')})
        mode = ModePaiement.ESPECES
    try:
        date_versement = _date(val.get('date_versement')) or aujourdhui
    except ValueError:
        avertir(_('Date du versement « %(v)s » illisible : date du jour.') % {'v': val.get('date_versement')})
        date_versement = aujourdhui
    reference = str(val.get('reference_versement') or '')[:80]
    paiements = []
    if val.get('detail_paiements'):
        trouves, illisibles = _detail_paiements(val['detail_paiements'])
        for jour, motif, somme, libelle in trouves:
            paiements.append({'date': jour.isoformat(), 'motif': motif, 'montant': somme, 'libelle': libelle})
        for ligne in illisibles:
            avertir(_('Paiement « %(v)s » illisible : non repris.') % {'v': ligne[:80]})
    else:
        insc_versee = _montant(brut.get('inscription_versee')) or 0
        scol_versee = _montant(brut.get('scolarite_versee')) or 0
        total = _montant(brut.get('total_paye')) or 0
        if total and not (insc_versee or scol_versee):
            insc_versee = min(total, frais_insc)
            scol_versee = total - insc_versee
            avertir(_('Total payé réparti : %(i)s sur l’inscription, %(s)s sur la scolarité.') % {
                'i': insc_versee, 's': scol_versee})
        for motif, somme in ((Motif.INSCRIPTION, insc_versee), (Motif.SCOLARITE, scol_versee)):
            if somme:
                paiements.append({'date': date_versement.isoformat(), 'motif': motif, 'montant': somme, 'libelle': ''})
    for p in paiements:
        if date.fromisoformat(p['date']) > aujourdhui:
            avertir(_('Paiement daté du %(d)s (dans le futur) : enregistré à la date du jour.') % {'d': p['date']})
            p['date'] = aujourdhui.isoformat()
    insc_total = sum(p['montant'] for p in paiements if p['motif'] == Motif.INSCRIPTION)
    scol_total = sum(p['montant'] for p in paiements if p['motif'] == Motif.SCOLARITE)
    if scol_total and scol_total > montant:
        avertir(_('Scolarité payée (%(v)s) supérieure au montant (%(m)s) : seuls %(m)s seront enregistrés.') % {
            'v': scol_total, 'm': montant})
    if insc_total and frais_insc and insc_total > frais_insc:
        avertir(_("Inscription payée (%(v)s) supérieure aux frais (%(m)s) : seuls %(m)s seront enregistrés.") % {
            'v': insc_total, 'm': frais_insc})
    try:
        rappels = _choix(val.get('rappels'), [('oui', 'Oui'), ('non', 'Non')], OUI_NON_ALIAS) != 'non'
    except ValueError:
        avertir(_('Rappels « %(v)s » non reconnu : « Oui ».') % {'v': val.get('rappels')})
        rappels = True

    return {
        'ligne': n, 'nom': nom[:100], 'prenom': prenom[:150], 'sexe': sexe,
        'date_naissance': naissance.isoformat() if naissance else '',
        'lieu_naissance': str(val.get('lieu_naissance') or '')[:100],
        'classe': classe[:50], 'etablissement': nom_etab, 'educ_master': str(val.get('educ_master') or '')[:50],
        'residence': str(val.get('residence') or '')[:200],
        'parent': parent[:150], 'nature_parent': natures[''], 'telephone': telephone[:30],
        'whatsapp': str(val.get('whatsapp') or '')[:30], 'email': str(val.get('email') or ''),
        'parent2': parent2[:150], 'nature2': natures['2'],
        'telephone2': str(val.get('telephone2') or '')[:30], 'whatsapp2': str(val.get('whatsapp2') or '')[:30],
        'email2': str(val.get('email2') or ''),
        'montant_scolarite': montant, 'frais_inscription': frais_insc, 'grille': bool(grille),
        'montant_impose': montant_impose,
        'reductions': reductions, 'paiements': paiements, 'mode_versement': mode, 'reference_versement': reference,
        'inscription_versee': insc_total, 'scolarite_versee': scol_total, 'rappels': rappels,
        'sans_contact': sans_contact,
    }, [], infos


def analyser(lignes, etab_defaut=''):
    rapport = Rapport()
    lignes = [l for l in lignes if any(c not in (None, '') for c in l)]
    if len(lignes) > MAX_LIGNES + 1:
        rapport.erreurs.append((0, _('Fichier trop long : %(n)s lignes au maximum par import.') % {'n': MAX_LIGNES}))
        return rapport
    if not lignes:
        rapport.erreurs.append((0, _('Le fichier est vide.')))
        return rapport
    i_entetes = _trouver_entetes(lignes)
    rapport.ligne_entetes = i_entetes + 1
    entetes = lignes[i_entetes]
    index = _associer_colonnes(entetes)
    rapport.colonnes_lues = [PAR_CHAMP[c].entete for c in index]
    rapport.colonnes_ignorees = [str(entetes[i]) for i, e in enumerate(entetes)
                                 if _cle(e) and i not in index.values()]
    a_nom = 'nom' in index or 'nom_prenom' in index or 'prenom' in index
    manquantes = [] if a_nom else [_('Nom (ou « Nom et prénom »)')]
    if manquantes:
        rapport.erreurs.append((rapport.ligne_entetes, _('Colonnes obligatoires introuvables : %(colonnes)s.') % {
            'colonnes': ', '.join(manquantes)}))
        return rapport

    etabs = list(Etablissement.objects.all())
    aujourdhui = timezone.localdate()
    vus = {}
    existants = Existants()
    montants_classe = _montants_par_classe(lignes[i_entetes + 1:], index)
    for n, ligne in enumerate(lignes[i_entetes + 1:], start=i_entetes + 2):
        brut = {c: (ligne[i] if i < len(ligne) else None) for c, i in index.items()}
        libelle = ' '.join(str(_cellule(brut.get(c)) or '') for c in ('nom', 'prenom', 'nom_prenom')).strip()
        if not libelle:
            message = _('Ligne sans nom d’élève (total ou ligne vide) : ignorée.')
            rapport.ignores.append((n, message))
            rapport.resultats.append({'ligne': n, 'eleve': '—', 'statut': 'ignore', 'messages': [message]})
            continue
        donnees, erreurs, infos = _analyser_ligne(n, brut, etabs, aujourdhui, rapport, etab_defaut, montants_classe)
        rapport.avertissements.extend((n, m) for m in infos)
        if erreurs:
            rapport.erreurs.extend((n, m) for m in erreurs)
            rapport.resultats.append({'ligne': n, 'eleve': libelle, 'statut': 'erreur', 'messages': erreurs})
            continue
        present = existants.trouver(donnees)
        if present:
            message = message_deja(present)
            rapport.deja.append((n, message))
            rapport.resultats.append({'ligne': n, 'eleve': libelle, 'statut': 'deja', 'messages': [message],
                                      'eleve_id': present.pk})
            continue
        cle = (_cle_nom(donnees['nom'], donnees['prenom']), cle_texte(donnees['etablissement']),
               donnees['date_naissance'] or cle_texte(donnees['classe']))
        if cle in vus:
            message = _('Même élève que la ligne %(n)s du fichier : ligne non importée (doublon).') % {'n': vus[cle]}
            rapport.ignores.append((n, message))
            rapport.resultats.append({'ligne': n, 'eleve': libelle, 'statut': 'ignore', 'messages': [message]})
            continue
        vus[cle] = n
        rapport.sans_contact += donnees['sans_contact']
        rapport.sans_etablissement += donnees['etablissement'] == NEANT
        rapport.a_creer.append(donnees)
        rapport.resultats.append({'ligne': n, 'eleve': libelle, 'statut': 'ok', 'messages': infos, 'donnees': donnees})
    return rapport


# --------------------------------------------------------------------------- Création

def _ajuster_tranches(eleve, total, frais_inscription):
    """Garde les dates des tranches de la classe mais ramène leurs montants au total donné (échéancier personnalisé)."""
    from .echeancier import _repartir, appliquer_reductions

    tranches = list(eleve.echeances.order_by('numero'))
    if not tranches:
        return
    for t, part in zip(tranches, _repartir(total, [t.montant_brut for t in tranches])):
        t.montant_brut = part
        t.save(update_fields=['montant_brut'])
    Eleve.objects.filter(pk=eleve.pk).update(echeancier_personnalise=True, frais_inscription=frais_inscription)
    eleve.echeancier_personnalise, eleve.frais_inscription = True, frais_inscription
    appliquer_reductions(eleve)


def importer(lignes, utilisateur=None, envoyer_factures=False, fichier=None):
    """Crée les fiches (contacts, réductions, paiements avec factures). Renvoie (élèves créés, factures).

    Un élève déjà dans la base (même s'il a été ajouté entre l'aperçu et la confirmation) n'est jamais recréé.
    `fichier` = {'empreinte', 'nom'} : garde la trace du fichier pour prévenir s'il est chargé de nouveau.
    """
    from .facturation import enregistrer_paiement
    from .notifications import envoyer_facture

    crees, factures = [], []
    with transaction.atomic():
        existants = Existants()
        trace = None
        if fichier:
            trace = ImportFichier.objects.create(empreinte=fichier['empreinte'], nom=fichier.get('nom', '')[:200],
                                                 importe_par=utilisateur)
        for d in lignes:
            if existants.trouver(d):
                continue
            etab = Etablissement.depuis_nom(d['etablissement'])
            eleve = Eleve.objects.create(import_fichier=trace,
                etablissement=etab, nom=d['nom'], prenom=d['prenom'], sexe=d['sexe'],
                date_naissance=date.fromisoformat(d['date_naissance']) if d['date_naissance'] else None,
                lieu_naissance=d['lieu_naissance'], classe=d['classe'], educ_master=d['educ_master'],
                residence=d['residence'], parent=d['parent'], nature_parent=d['nature_parent'],
                telephone=d['telephone'], whatsapp=d['whatsapp'], email=d['email'], suivi_actif=d['rappels'],
                montant_scolarite=d['montant_scolarite'], frais_inscription=d['frais_inscription'],
            )
            if d['parent2']:
                ContactParent.objects.create(eleve=eleve, nom=d['parent2'], nature=d['nature2'],
                                             telephone=d['telephone2'], whatsapp=d['whatsapp2'], email=d['email2'])
            for r in d['reductions']:
                ReductionEleve.objects.create(eleve=eleve, porte_sur=r['porte_sur'], type=r['type'], mode=r['mode'],
                                              valeur=r['valeur'], motif=r['motif'], cree_par=utilisateur)
            generer_echeancier(eleve, montant=None if d['grille'] else d['montant_scolarite'])
            if d.get('montant_impose'):
                _ajuster_tranches(eleve, d['montant_impose'], d['frais_inscription'])
            existants.ajouter(eleve)
            crees.append((eleve, d))
        for eleve, _d in crees:
            recalculer_fratrie(eleve)
        for eleve, d in crees:
            for p in sorted(d.get('paiements', []), key=lambda p: (p['date'], p['motif'] != Motif.INSCRIPTION)):
                eleve = Eleve.objects.avec_totaux().get(pk=eleve.pk)
                if p['motif'] == Motif.SCOLARITE:
                    plafond = eleve.reste_a_payer
                else:
                    plafond = eleve.inscription_reste if eleve.frais_inscription else p['montant']
                montant = min(p['montant'], plafond)
                if montant <= 0:
                    continue
                paiement = Paiement(eleve=eleve, motif=p['motif'], montant=montant, mode=d['mode_versement'],
                                    date=date.fromisoformat(p['date']), reference=d['reference_versement'],
                                    observation=(p.get('libelle') or str(_('Repris à l’import')))[:200])
                factures.append(enregistrer_paiement(paiement, utilisateur, notifier=False))
        if trace:
            if crees:
                ImportFichier.objects.filter(pk=trace.pk).update(nb_eleves=len(crees), nb_paiements=len(factures))
            else:
                trace.delete()
    if envoyer_factures:
        for f in factures:
            try:
                envoyer_facture(f)
            except Exception:  # noqa: BLE001 — un envoi raté ne bloque pas l'import
                pass
    return [e for e, _d in crees], factures


# --------------------------------------------------------------------------- Modèle à télécharger

def modele_xlsx():
    classeur = Workbook()
    feuille = classeur.active
    feuille.title = 'Élèves'
    feuille.append(ENTETES_MODELE)
    exemples = [
        {'nom': 'DOSSOU', 'prenom': 'Koffi Jean', 'sexe': 'M', 'date_naissance': '14/03/2014', 'lieu_naissance': 'Cotonou',
         'classe': 'CM2', 'etablissement': 'École Exemple', 'residence': 'Akpakpa, Cotonou',
         'parent': 'DOSSOU Marc', 'nature_parent': 'Père', 'telephone': '01 97 00 00 00', 'email': 'parent@exemple.bj',
         'parent2': 'DOSSOU Awa', 'lien2': 'Mère', 'telephone2': '01 96 00 00 00',
         'montant_scolarite': 120000, 'frais_inscription': 15000, 'reduction_scolarite': '10 %', 'type_reduction': 'Bourse',
         'motif_reduction': 'Bourse d’excellence',
         'detail_paiements': '15/09/2026 - Paiement inscription : 15 000 CFA\n'
                             '15/09/2026 - Paiement 1er trimestre : 40 000 CFA',
         'mode_versement': 'Mobile Money', 'reference_versement': 'MM-123456', 'rappels': 'Oui'},
        {'nom': 'DOSSOU', 'prenom': 'Afi', 'sexe': 'F', 'date_naissance': '02/07/2016', 'lieu_naissance': 'Cotonou',
         'classe': 'Primaire', 'niveau': 'CE1', 'etablissement': 'École Exemple', 'residence': 'Akpakpa, Cotonou',
         'parent': 'DOSSOU Marc', 'nature_parent': 'Père', 'telephone': '01 97 00 00 00', 'email': 'parent@exemple.bj',
         'montant_scolarite': 100000, 'frais_inscription': 15000, 'reduction_inscription': '50 %', 'type_reduction': 'Remise',
         'motif_reduction': 'Deuxième enfant', 'inscription_versee': 7500, 'mode_versement': 'Espèces', 'rappels': 'Oui'},
    ]
    for exemple in exemples:
        feuille.append([exemple.get(c.champ, '') for c in COLONNES])
    noir, gris = PatternFill('solid', fgColor='0A0A0A'), PatternFill('solid', fgColor='6B6B6B')
    for i, col in enumerate(COLONNES, start=1):
        cellule = feuille.cell(row=1, column=i)
        cellule.font = Font(bold=True, color='FFFFFF')
        cellule.fill = noir if col.obligatoire else gris
        cellule.alignment = Alignment(wrap_text=True, vertical='center')
        feuille.column_dimensions[cellule.column_letter].width = max(14, min(28, len(col.entete) + 4))
    feuille.row_dimensions[1].height = 32
    feuille.freeze_panes = 'C2'
    listes = {
        'sexe': '"M,F"', 'nature_parent': '"Père,Mère,Tuteur,Tutrice,Autre"', 'lien2': '"Père,Mère,Tuteur,Tutrice,Autre"',
        'type_reduction': '"Bourse,Remise"', 'mode_versement': '"Espèces,Mobile Money,Virement bancaire,Chèque"',
        'rappels': '"Oui,Non"',
    }
    for i, col in enumerate(COLONNES, start=1):
        if col.champ in listes:
            dv = DataValidation(type='list', formula1=listes[col.champ], allow_blank=True)
            lettre = feuille.cell(row=1, column=i).column_letter
            dv.add(f'{lettre}2:{lettre}{MAX_LIGNES + 1}')
            feuille.add_data_validation(dv)

    aide = classeur.create_sheet("Mode d'emploi")
    aide.append(['Colonne', 'Obligatoire', 'Description', 'Exemple'])
    for cellule in aide[1]:
        cellule.font = Font(bold=True, color='FFFFFF')
        cellule.fill = noir
    for titre, champs in GROUPES:
        aide.append([titre])
        aide.cell(row=aide.max_row, column=1).font = Font(bold=True)
        for champ in champs:
            col = PAR_CHAMP[champ]
            aide.append([col.entete, 'Oui' if col.obligatoire else '', col.description, col.exemple])
    aide.append([])
    aide.append(['Une ligne = un élève. Seules les colonnes en noir (Nom, Prénom(s), Classe, Parent, Téléphone) sont '
                 'obligatoires ; les autres peuvent être laissées vides ou supprimées. Les intitulés peuvent varier '
                 '(« Prénoms », « Tél », « Mail »…). Les élèves déjà dans la base ne sont jamais importés une seconde fois. Si les frais de la classe '
                 'sont définis dans l’application, ils remplacent les montants du fichier.'])
    for lettre, largeur in (('A', 26), ('B', 12), ('C', 80), ('D', 22)):
        aide.column_dimensions[lettre].width = largeur
    for ligne in aide.iter_rows(min_row=2):
        for cellule in ligne:
            cellule.alignment = Alignment(wrap_text=True, vertical='top')
    sortie = io.BytesIO()
    classeur.save(sortie)
    return sortie.getvalue()
