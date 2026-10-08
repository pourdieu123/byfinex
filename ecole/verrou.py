"""Codes d'accès des modules : chaque utilisateur a son propre code pour chaque module.

Une fois connecté, le code est demandé en entrant dans un module (depuis l'accueil ou un autre module).
Sans code pour un module, on n'y entre pas ; la direction crée elle-même son code à sa première entrée.

Un seul module est ouvert à la fois : revenir à l'accueil ou passer dans un autre module le referme,
de même qu'une inactivité de plus de VERROU_MODULE_MINUTES (réglage, 30 min par défaut).
Les codes se définissent dans Administration › Utilisateurs (ou : python manage.py codes_modules).
"""

import time

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core.cache import cache

from .models import CodeModule

CLE = 'module_ouvert'
CLE_ECHECS = 'module_echecs'
MAX_ECHECS = 5
BLOCAGE_SECONDES = 5 * 60


def _inactivite():
    return getattr(settings, 'VERROU_MODULE_MINUTES', 30) * 60


def peut_creer_son_code(user):
    """La direction, tant qu'elle n'a pas de code pour un module, le crée à sa première entrée."""
    return user.has_perm('ecole.gerer_utilisateurs')


def modules_proteges(user):
    """Modules pour lesquels cet utilisateur a un code."""
    return set(CodeModule.objects.filter(utilisateur=user).values_list('module', flat=True))


def est_protege(user, module):
    return CodeModule.objects.filter(utilisateur=user, module=module).exists()


def definir_code(user, module, code):
    CodeModule.objects.update_or_create(utilisateur=user, module=module, defaults={'code': make_password(code)})


def retirer_code(user, module):
    CodeModule.objects.filter(utilisateur=user, module=module).delete()


def module_ouvert(request):
    """Code du dernier module ouvert (même si le délai d'inactivité est dépassé), ou None."""
    return (request.session.get(CLE) or {}).get('module')


def est_ouvert(request, module):
    ouvert = request.session.get(CLE) or {}
    return ouvert.get('module') == module and time.time() - ouvert.get('vu', 0) < _inactivite()


def ouvrir(request, module):
    request.session[CLE] = {'module': module, 'vu': time.time()}


def fermer(request):
    request.session.pop(CLE, None)


def _cle_echecs(request):
    # Compté par utilisateur : se déconnecter / reconnecter ne remet pas le compteur à zéro.
    u = request.user
    return f'verrou-echecs-{u.pk}-{u.date_joined.timestamp():.0f}'


def attente_restante(request):
    """Secondes à attendre après trop d'essais ratés (0 si l'on peut réessayer)."""
    reste = (cache.get(_cle_echecs(request)) or {}).get('jusqua', 0) - time.time()
    return int(reste) + 1 if reste > 0 else 0


def verifier(request, user, module, saisie):
    """Vérifie le code personnel saisi ; ouvre le module s'il est bon, compte les échecs sinon."""
    obj = CodeModule.objects.filter(utilisateur=user, module=module).first()
    if obj is None:
        return False
    if check_password(saisie, obj.code):
        cache.delete(_cle_echecs(request))
        ouvrir(request, module)
        return True
    echecs = (cache.get(_cle_echecs(request)) or {}).get('n', 0) + 1
    etat = {'n': 0, 'jusqua': time.time() + BLOCAGE_SECONDES} if echecs >= MAX_ECHECS else {'n': echecs}
    cache.set(_cle_echecs(request), etat, BLOCAGE_SECONDES * 2)
    return False
