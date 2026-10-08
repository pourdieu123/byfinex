"""Les modules de l'application (espaces de travail), et lesquels un utilisateur peut ouvrir.

Chaque module a sa propre barre latérale (classe CSS ``mod-<code>`` sur la page) ; l'interface est en noir et blanc.
"""

from dataclasses import dataclass, field

from django.conf import settings
from django.utils.translation import gettext_lazy as _


@dataclass(frozen=True)
class Module:
    code: str
    nom: str
    description: str
    icone: str
    url: str
    accroche: str = ''
    fonctions: tuple = field(default_factory=tuple)


MODULES = [
    Module('scolarite', _('Scolarité'), _('Grille des frais, échéanciers, encaissements, factures et relances des parents.'),
           'encaisser', 'ecole:accueil', _('Finances des élèves'),
           (_('Inscriptions'), _('Échéanciers'), _('Factures'), _('Relances'), _('Exports'))),
    Module('controle', _('Contrôle des paiements'), _('Statut de paiement des élèves (à jour, en retard, soldé) pour les surveillants.'),
           'suivi', 'ecole:controle', _('Surveillance'),
           (_('À jour'), _('En retard'), _('Soldés'))),
    Module('notes', _('Notes et moyennes'), _('Saisie des notes, moyennes coefficientées, rangs et bulletins.'),
           'notes', 'notes:tableau', _('Suivi pédagogique'),
           (_('Matières'), _('Saisie'), _('Moyennes'), _('Rangs'), _('Bulletins'))),
    Module('profs', _('Professeurs'), _('Fiches, classes attribuées, emplois du temps, heures et paiements.'),
           'professeur', 'profs:liste', _('Équipe enseignante'),
           (_('Fiches'), _('Emplois du temps'), _('Heures'), _('Paiements'))),
    Module('examens', _('Examens'), _('Planning des compositions, salles, surveillants et suivi de la saisie.'),
           'examen', 'examens:liste', _('Compositions internes'),
           (_('Sessions'), _('Épreuves'), _('Surveillances'), _('Planning PDF'))),
]

ADMINISTRATION = Module('admin', _('Administration'), _('Comptes, rôles, établissements et paramètres généraux.'),
                        'utilisateurs', 'ecole:utilisateurs', _('Réglages'), (_('Utilisateurs'), _('Rôles'), _('Paramètres')))

# Espaces de noms d'URL -> module ; les pages d'administration ont leur propre espace.
_NAMESPACES = {'notes': 'notes', 'profs': 'profs', 'examens': 'examens'}
_PAGES_ADMIN = {'utilisateurs', 'utilisateur_nouveau', 'utilisateur_modifier', 'parametres', 'etablissement_renommer',
                'etablissement_modifier', 'regles_relance', 'regle_relance_nouvelle', 'regle_relance_modifier',
                'regle_relance_supprimer', 'relances_lancer', 'sms_essai'}
# Pages hors module : pas de code d'accès (accueil, saisie du code, mise à jour en temps réel).
_HORS_MODULE = {'portail', 'deverrouiller', 'version'}


def bientot(code):
    """Module pas encore ouvert : tuile « Bientôt disponible » et pages fermées (réglage MODULES_BIENTOT)."""
    return code in getattr(settings, 'MODULES_BIENTOT', [])


def accessible(user, code):
    from pedagogie.acces import professeur_de

    if code == 'scolarite':
        return user.has_perm('ecole.view_eleve')
    if code == 'controle':
        return user.has_perm('ecole.voir_statuts')
    if code == 'notes':
        return user.has_perm('pedagogie.view_fichenote')
    if code == 'profs':
        return user.has_perm('pedagogie.view_professeur') and (
            user.has_perm('pedagogie.voir_tout') or professeur_de(user) is not None)
    if code == 'examens':
        return user.has_perm('pedagogie.view_composition')
    if code == 'admin':
        return user.has_perm('ecole.gerer_utilisateurs')
    return False


def modules_de(user):
    """Modules du rôle de l'utilisateur, y compris ceux « Bientôt disponible » (affichés sur l'accueil).
    La tuile « Contrôle » n'est montrée qu'aux surveillants (le personnel voit déjà tout dans la Scolarité)."""
    return [m for m in MODULES if accessible(user, m.code)
            and not (m.code == 'controle' and user.has_perm('ecole.view_eleve'))]


def modules_ouverts(user):
    return [m for m in modules_de(user) if not bientot(m.code)]


def module_courant(request):
    match = getattr(request, 'resolver_match', None)
    if not match:
        return None
    if match.namespace in _NAMESPACES:
        return _NAMESPACES[match.namespace]
    if match.namespace == 'ecole':
        if match.url_name in _HORS_MODULE:
            return None
        if match.url_name == 'controle':
            return 'controle'
        return 'admin' if match.url_name in _PAGES_ADMIN else 'scolarite'
    return None


def info_module(code):
    if code == 'admin':
        return ADMINISTRATION
    return next((m for m in MODULES if m.code == code), None)
