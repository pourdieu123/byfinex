"""Rôles de l'application, implémentés avec les groupes et permissions de Django."""

from django.contrib.auth.models import Group, Permission
from django.utils.translation import gettext_lazy as _

DIRECTION = 'Direction'
SECRETARIAT = 'Secrétariat'
CAISSIER = 'Caissier'
PROFESSEUR = 'Professeur'
SURVEILLANT = 'Surveillant'
PARENT = 'Parent'

ROLES = [DIRECTION, SECRETARIAT, CAISSIER, PROFESSEUR, SURVEILLANT, PARENT]
# Rôles proposés à la création d'un compte (les accès parents se créent depuis la fiche de l'élève).
ROLES_PERSONNEL = [DIRECTION, SECRETARIAT, CAISSIER, PROFESSEUR, SURVEILLANT]

# Libellés affichés (traduits) ; les noms ci-dessus servent d'identifiants de groupes.
LIBELLES = {DIRECTION: _('Direction'), SECRETARIAT: _('Secrétariat'), CAISSIER: _('Caissier'), PROFESSEUR: _('Professeur'),
            SURVEILLANT: _('Surveillant'), PARENT: _('Parent')}

DESCRIPTIONS = {
    DIRECTION: _('Accès complet : suppression, utilisateurs, paramètres, paiement des professeurs.'),
    SECRETARIAT: _('Élèves, relances, exports, matières, professeurs, emplois du temps, compositions et bulletins.'),
    CAISSIER: _('Encaisse la scolarité, imprime les reçus, relance les parents, paie les professeurs.'),
    PROFESSEUR: _('Saisit les notes de ses classes, consulte son emploi du temps, ses heures et ses surveillances.'),
    SURVEILLANT: _('Consulte le statut de paiement (à jour, en retard, soldé) des élèves de ses classes.'),
    PARENT: _('Espace parents : situation de ses enfants, historique des paiements et factures.'),
}

_TOUT = ['view', 'add', 'change', 'delete']


def _perms(modele, actions=_TOUT):
    return [f'{a}_{modele}' for a in actions]


PERMISSIONS = {
    DIRECTION: [
        'view_eleve', 'add_eleve', 'change_eleve', 'archiver_eleve',
        'view_paiement', 'add_paiement', 'delete_paiement',
        'view_relance', 'add_relance',
        'exporter_donnees', 'gerer_utilisateurs', 'voir_statuts',
        'change_paiement', 'valider_corrections', 'view_demandecorrection', 'view_historiquepaiement',
        *_perms('grillefrais'), 'add_reductioneleve', 'delete_reductioneleve', 'change_echeance',
        'view_facture', 'view_notification', *_perms('reglerelance'),
        'add_compteparent', 'delete_compteparent', 'add_contactparent', 'delete_contactparent',
        *_perms('matiere'), *_perms('professeur'), *_perms('enseignement'), *_perms('fichenote'),
        *_perms('creneau'), *_perms('heurecours'), *_perms('paiementprofesseur'),
        *_perms('composition'), *_perms('epreuve'), 'voir_tout',
    ],
    SECRETARIAT: [
        'view_eleve', 'add_eleve', 'change_eleve',
        'view_paiement',
        'view_relance', 'add_relance',
        'exporter_donnees',
        'add_demandecorrection', 'view_demandecorrection', 'view_historiquepaiement',
        'view_grillefrais', 'add_grillefrais', 'change_grillefrais', 'add_reductioneleve', 'delete_reductioneleve',
        'view_facture', 'view_notification',
        'add_compteparent', 'delete_compteparent', 'add_contactparent', 'delete_contactparent',
        *_perms('matiere'), *_perms('professeur', ['view', 'add', 'change']), *_perms('enseignement'),
        'view_fichenote', 'change_fichenote', *_perms('creneau'), 'view_heurecours', 'add_heurecours',
        *_perms('composition'), *_perms('epreuve'), 'voir_tout',
    ],
    CAISSIER: [
        'view_eleve',
        'view_paiement', 'add_paiement',
        'view_relance', 'add_relance',
        'view_professeur', 'view_heurecours', 'view_paiementprofesseur', 'add_paiementprofesseur', 'voir_tout',
        'add_demandecorrection', 'view_demandecorrection', 'view_historiquepaiement',
        'view_grillefrais', 'view_facture', 'view_notification',
    ],
    # Le professeur ne voit que ses propres classes, cours et paiements (filtrage dans les vues).
    PROFESSEUR: [
        'view_fichenote', 'change_fichenote', 'view_creneau', 'view_composition', 'view_epreuve',
        'view_heurecours', 'view_paiementprofesseur', 'view_professeur',
    ],
    SURVEILLANT: ['voir_statuts'],
    # Le parent ne voit que ses enfants (filtrage dans l'espace parents).
    PARENT: [],
}


def synchroniser_groupes(**kwargs):
    """Crée ou met à jour les groupes de rôles (appelé après chaque migrate)."""
    for role, codenames in PERMISSIONS.items():
        groupe, _cree = Group.objects.get_or_create(name=role)
        groupe.permissions.set(
            Permission.objects.filter(content_type__app_label__in=['ecole', 'pedagogie'], codename__in=codenames)
        )


def role_de(user):
    if user.is_superuser:
        return DIRECTION
    noms = set(user.groups.values_list('name', flat=True))
    return next((r for r in ROLES if r in noms), None)


def libelle_role(role):
    return LIBELLES.get(role, role) if role else None


def definir_role(user, role):
    user.groups.remove(*Group.objects.filter(name__in=ROLES))
    if role:
        user.groups.add(Group.objects.get(name=role))
