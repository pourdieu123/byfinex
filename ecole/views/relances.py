from urllib.parse import quote

from django.contrib.auth.decorators import login_required, permission_required
from django.http import HttpResponseNotAllowed, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST
from django.utils.translation import gettext as _

from ..models import CanalRelance, Eleve, Parametres, Relance, remplacer_variables
from ..templatetags.ecole_extras import fcfa
from .eleves import contexte_filtres, etab_demande


class RedirectionMessagerie(HttpResponseRedirect):
    """Redirection autorisée vers l'application SMS du téléphone (schéma sms:)."""

    allowed_schemes = ['https', 'sms']


def message_relance(eleve, parametres):
    return remplacer_variables(parametres.message_relance, **dict(
        parent=eleve.parent,
        eleve=f'{eleve.prenom} {eleve.nom.upper()}',
        classe=eleve.classe,
        reste=fcfa(eleve.reste_a_payer),
        ecole=eleve.etablissement.nom,
    ))


@login_required
@permission_required('ecole.view_relance', raise_exception=True)
def relances(request):
    classe = request.GET.get('classe', '')
    etab_id = etab_demande(request)
    eleves = Eleve.objects.actifs().impayes().select_related('etablissement')
    if etab_id:
        eleves = eleves.filter(etablissement_id=etab_id)
    if classe:
        eleves = eleves.filter(classe=classe)
    eleves = sorted(eleves, key=lambda e: (e.derniere_relance is not None, e.derniere_relance, -e.reste_a_payer))
    return render(request, 'ecole/relances.html', {
        'eleves': eleves,
        'reste_total': sum(e.reste_a_payer for e in eleves),
        'historique': Relance.objects.select_related('eleve', 'envoye_par')[:10],
        'apercu': message_relance(eleves[0], Parametres.courant()) if eleves else '',
        **contexte_filtres(request, etab_id, classe),
    })


@login_required
@permission_required('ecole.add_relance', raise_exception=True)
@require_POST
def relancer(request, pk, canal):
    if canal not in CanalRelance.values:
        return HttpResponseNotAllowed(['POST'])
    eleve = get_object_or_404(Eleve.objects.avec_totaux().select_related('etablissement'), pk=pk)
    texte = quote(message_relance(eleve, Parametres.courant()))
    Relance.objects.create(eleve=eleve, canal=canal, reste_a_payer=eleve.reste_a_payer, envoye_par=request.user)
    numero = eleve.telephone_international
    if canal == CanalRelance.WHATSAPP:
        return RedirectionMessagerie(f'https://wa.me/{numero}?text={texte}')
    # « ?&body= » est la forme comprise à la fois par Android et iOS.
    return RedirectionMessagerie(f'sms:+{numero}?&body={texte}')
