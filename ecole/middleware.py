from urllib.parse import urlencode

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.urls import reverse

from . import verrou
from .modules import accessible, bientot, info_module, module_courant


class VerrouModulesMiddleware:
    """Demande le code d'un module protégé avant d'afficher l'une de ses pages."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if not request.user.is_authenticated:
            return None
        module = module_courant(request)
        if module and bientot(module):
            return render(request, 'ecole/bientot.html', {'m': info_module(module)})
        if not getattr(settings, 'VERROU_MODULES', True):
            return None
        match = request.resolver_match
        if match.namespace == 'ecole' and match.url_name == 'portail':
            verrou.fermer(request)  # retour à l'accueil : le module est refermé
            return None
        module = module_courant(request)
        if module is None:
            return None
        if not accessible(request.user, module):
            # Défense en profondeur : même une page sans contrôle de droits reste fermée à ce rôle.
            raise PermissionDenied
        # Un formulaire envoyé depuis le module ouvert passe, même après l'inactivité, pour ne pas perdre la saisie.
        en_cours = request.method == 'POST' and verrou.module_ouvert(request) == module
        if verrou.est_ouvert(request, module) or en_cours:
            verrou.ouvrir(request, module)
            return None
        verrou.fermer(request)  # on quitte le module ouvert : il faudra retaper son code pour y revenir
        url = reverse('ecole:deverrouiller', args=[module])
        return redirect(f"{url}?{urlencode({'next': request.get_full_path()})}")
