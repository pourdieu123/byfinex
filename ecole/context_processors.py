from django.conf import settings
from django.utils.translation import gettext as _

from . import roles
from .models import Etablissement, Parametres
from .modules import ADMINISTRATION, accessible, info_module, module_courant, modules_ouverts


def _nb_corrections(user):
    if not user.has_perm('ecole.valider_corrections'):
        return 0
    from .models import DemandeCorrection, StatutDemande

    return DemandeCorrection.objects.filter(statut=StatutDemande.EN_ATTENTE).count()


def ecole(request):
    if not request.user.is_authenticated:
        return {'app_nom': settings.APP_NOM}
    from pedagogie.acces import professeur_de

    return {
        'app_nom': settings.APP_NOM,
        'parametres': Parametres.courant(),
        # Nom affiché dans le menu quand un seul établissement est utilisé.
        'etablissement_unique': Etablissement.objects.first() if Etablissement.objects.count() == 1 else None,
        'role': roles.libelle_role(roles.role_de(request.user)),
        'mes_modules': modules_ouverts(request.user),
        'module': module_courant(request),
        'module_info': info_module(module_courant(request)),
        'admin_module': ADMINISTRATION if accessible(request.user, 'admin') else None,
        'moi_prof': professeur_de(request.user),
        'nb_corrections': _nb_corrections(request.user),
    }
