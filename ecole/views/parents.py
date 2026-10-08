"""Espace parents : situation de chaque enfant, échéancier, historique des paiements, factures."""

from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render

from .. import pdf
from ..models import Eleve, Facture, TypeFacture


def compte_parent_requis(vue):
    @wraps(vue)
    @login_required
    def enveloppe(request, *args, **kwargs):
        compte = getattr(request.user, 'compte_parent', None)
        if compte is None:
            raise PermissionDenied
        request.compte_parent = compte
        return vue(request, *args, **kwargs)
    return enveloppe


def _enfants(request):
    return (Eleve.objects.filter(pk__in=request.compte_parent.eleves.values('pk')).avec_totaux()
            .select_related('etablissement').prefetch_related('echeances').order_by('nom', 'prenom'))


@compte_parent_requis
def accueil(request):
    return render(request, 'ecole/parents/accueil.html', {'enfants': list(_enfants(request)), 'temps_reel': True})


@compte_parent_requis
def enfant(request, pk):
    eleve = get_object_or_404(_enfants(request), pk=pk)
    return render(request, 'ecole/parents/enfant.html', {
        'eleve': eleve, 'sit': eleve.situation,
        'paiements': eleve.paiements.order_by('-date', '-cree_le'),
        'factures': eleve.factures.order_by('-date'),
        'temps_reel': True,
    })


@compte_parent_requis
def facture_pdf(request, pk):
    facture = get_object_or_404(Facture.objects.select_related('eleve__etablissement', 'paiement'), pk=pk,
                                eleve__in=request.compte_parent.eleves.all())
    reponse = HttpResponse(pdf.pdf_facture(facture), content_type='application/pdf')
    reponse['Content-Disposition'] = f'inline; filename="facture-{facture.numero}.pdf"'
    return reponse


__all__ = ['accueil', 'enfant', 'facture_pdf', 'TypeFacture']
