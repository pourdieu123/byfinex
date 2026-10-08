"""Journal des messages échangés avec les parents, contrôle des surveillants, mise à jour en temps réel."""

from django.contrib.auth.decorators import login_required, permission_required
from django.core.paginator import Paginator
from django.db.models import Count, Max, Q
from django.http import JsonResponse
from django.shortcuts import render

from ..models import (
    CanalMessage, DemandeCorrection, Eleve, Facture, HistoriquePaiement, Notification, Paiement, StatutMessage,
    TypeMessage,
)
from .eleves import filtrer_recherche


@login_required
@permission_required('ecole.view_notification', raise_exception=True)
def journal_messages(request):
    qs = Notification.objects.select_related('eleve', 'facture')
    canal, type_message, statut = request.GET.get('canal', ''), request.GET.get('type', ''), request.GET.get('statut', '')
    if canal in CanalMessage.values:
        qs = qs.filter(canal=canal)
    if type_message in TypeMessage.values:
        qs = qs.filter(type=type_message)
    if statut in StatutMessage.values:
        qs = qs.filter(statut=statut)
    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(Q(destinataire__icontains=q) | Q(eleve__nom__icontains=q) | Q(eleve__prenom__icontains=q))
    page = Paginator(qs, 50).get_page(request.GET.get('page'))
    params = request.GET.copy()
    params.pop('page', None)
    return render(request, 'ecole/messages.html', {
        'page': page, 'params': params.urlencode(), 'f': request.GET, 'q': q,
        'canaux': CanalMessage.choices, 'types': TypeMessage.choices, 'statuts': StatutMessage.choices,
        'nb_echecs': Notification.objects.filter(statut=StatutMessage.ECHEC).count(),
    })


@login_required
@permission_required('ecole.voir_statuts', raise_exception=True)
def controle(request):
    """Surveillants : statut de paiement (à jour / en retard / soldé) des élèves de leur périmètre, sans montants."""
    eleves = Eleve.objects.actifs().avec_totaux().select_related('etablissement').prefetch_related('echeances')
    perimetre = getattr(request.user, 'perimetre', None)
    if perimetre is not None and not request.user.has_perm('ecole.gerer_utilisateurs'):
        eleves = perimetre.filtrer(eleves)
    classes = sorted({(e.etablissement.nom, e.classe) for e in eleves})
    classe = request.GET.get('classe', '')
    q = request.GET.get('q', '').strip()
    eleves = filtrer_recherche(eleves, q)
    if classe:
        eleves = eleves.filter(classe=classe)
    eleves = list(eleves)
    filtre = request.GET.get('statut', '')
    compte = {s: sum(1 for e in eleves if e.situation.statut == s) for s in ('a_jour', 'retard', 'solde')}
    if filtre in compte:
        eleves = [e for e in eleves if e.situation.statut == filtre]
    return render(request, 'ecole/controle.html', {
        'eleves': eleves, 'classes': classes, 'classe_active': classe, 'q': q, 'statut': filtre, 'compte': compte,
        'temps_reel': True,
    })


@login_required
def version(request):
    """Empreinte des données : la page se recharge quand elle change (paiement, correction, élève...)."""
    p = Paiement.objects.aggregate(n=Count('id'), m=Max('id'))
    empreinte = '-'.join(str(x) for x in (
        p['n'], p['m'], Facture.objects.count(), HistoriquePaiement.objects.count(),
        DemandeCorrection.objects.aggregate(m=Max('traite_le'))['m'], DemandeCorrection.objects.count(),
        Eleve.objects.aggregate(m=Max('modifie_le'))['m'], Eleve.objects.count(),
    ))
    import hashlib

    from django.conf import settings

    # Empreinte opaque : ne révèle aucun chiffre (nombre d'élèves, de paiements...).
    return JsonResponse({'v': hashlib.sha256(f'{settings.SECRET_KEY}{empreinte}'.encode()).hexdigest()[:16]})
