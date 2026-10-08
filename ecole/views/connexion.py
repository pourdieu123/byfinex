"""Connexion protégée contre les essais de mots de passe en série (par identifiant et par adresse IP)."""

import time

from django.conf import settings
from django.contrib.auth import views as auth_views
from django.contrib.auth.forms import AuthenticationForm
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _


def _ip(request):
    return request.META.get('REMOTE_ADDR', '')


def _cles(request, identifiant):
    return [f'connexion-ip-{_ip(request)}', f'connexion-id-{(identifiant or "").strip().lower()}']


def _bloque(cles):
    maintenant = time.time()
    return max((cache.get(c) or {}).get('jusqua', 0) - maintenant for c in cles)


class ConnexionForm(AuthenticationForm):
    def clean(self):
        cles = _cles(self.request, self.data.get('username'))
        attente = _bloque(cles)
        if attente > 0:
            raise ValidationError(_('Trop d’essais incorrects. Réessayez dans %(n)s min.') % {'n': int(attente // 60) + 1},
                                  code='bloque')
        try:
            return super().clean()
        except ValidationError:
            maximum = getattr(settings, 'CONNEXION_MAX_ECHECS', 5)
            duree = getattr(settings, 'CONNEXION_BLOCAGE_MINUTES', 15) * 60
            for cle in cles:
                etat = cache.get(cle) or {'n': 0}
                etat['n'] = etat.get('n', 0) + 1
                if etat['n'] >= maximum:
                    etat = {'n': 0, 'jusqua': time.time() + duree}
                cache.set(cle, etat, duree * 2)
            raise


class ConnexionView(auth_views.LoginView):
    form_class = ConnexionForm

    def form_invalid(self, form):
        reponse = super().form_invalid(form)
        bloque = [e.message for e in form.non_field_errors().as_data() if e.code == 'bloque']
        reponse.context_data['bloque'] = bloque[0] if bloque else ''
        return reponse

    def form_valid(self, form):
        for cle in _cles(self.request, form.cleaned_data.get('username')):
            cache.delete(cle)
        return super().form_valid(form)
