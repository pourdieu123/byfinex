from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from .. import pdf
from ..facturation import (
    annuler_paiement, corriger_paiement, enregistrer_paiement, facture_active, normaliser_facture, valider_demande,
)
from ..forms import CorrectionForm, MotifForm, PaiementForm
from ..models import (
    ActionCorrection, DemandeCorrection, Eleve, Facture, ModePaiement, Motif, Paiement, Parametres, StatutDemande,
    StatutFacture, TypeFacture,
)
from .eleves import classes_existantes, contexte_filtres, etab_demande, etablissements_utilises, filtrer_recherche


def _date(valeur):
    try:
        return date.fromisoformat(valeur)
    except (TypeError, ValueError):
        return None


def filtrer_paiements(params):
    """Applique les filtres du journal (aussi utilisés par l'export Excel)."""
    qs = Paiement.objects.select_related('eleve', 'eleve__etablissement', 'encaisse_par')
    etab = params.get('etab', '')
    if etab.isdigit():
        qs = qs.filter(eleve__etablissement_id=int(etab))
    du, au = _date(params.get('du')), _date(params.get('au'))
    if du:
        qs = qs.filter(date__gte=du)
    if au:
        qs = qs.filter(date__lte=au)
    if params.get('mode') in ModePaiement.values:
        qs = qs.filter(mode=params['mode'])
    if params.get('motif') in Motif.values:
        qs = qs.filter(motif=params['motif'])
    if params.get('classe'):
        qs = qs.filter(eleve__classe=params['classe'])
    q = (params.get('q') or '').strip()
    if q:
        qs = qs.filter(
            Q(eleve__nom__icontains=q) | Q(eleve__prenom__icontains=q)
            | Q(numero_recu__icontains=q) | Q(reference__icontains=q)
        )
    return qs


@login_required
@permission_required('ecole.add_paiement', raise_exception=True)
def encaisser_recherche(request):
    q = request.GET.get('q', '').strip()
    eleves = filtrer_recherche(Eleve.objects.actifs().avec_totaux(), q)[:30] if q else []
    return render(request, 'ecole/encaisser_recherche.html', {'q': q, 'eleves': eleves})


@login_required
@permission_required('ecole.add_paiement', raise_exception=True)
def encaisser(request, pk):
    eleve = get_object_or_404(Eleve.objects.avec_totaux(), pk=pk)
    if eleve.archive:
        messages.error(request, _('Élève archivé : réactivez-le avant d’encaisser un paiement.'))
        return redirect('ecole:eleve_detail', pk=eleve.pk)
    form = PaiementForm(request.POST if request.method == 'POST' else None, eleve=eleve)
    if request.method == 'POST' and form.is_valid():
        paiement = form.save(commit=False)
        paiement.eleve = eleve
        facture = enregistrer_paiement(paiement, request.user)
        messages.success(request, _('Paiement %(numero_recu)s enregistré — facture %(facture)s envoyée au parent.') % {
            'numero_recu': paiement.numero_recu, 'facture': facture.numero})
        return redirect('ecole:recu', pk=paiement.pk)
    sit = eleve.situation
    tranches = [{'libelle': str(l.echeance), 'reste': l.reste, 'echeance': l.echeance.echeance.strftime('%d/%m/%Y')}
                for l in sit.lignes if l.reste]
    return render(request, 'ecole/encaisser.html', {'eleve': eleve, 'form': form, 'sit': sit, 'tranches_dues': tranches})


@login_required
@permission_required('ecole.view_paiement', raise_exception=True)
def recu(request, pk):
    paiement = get_object_or_404(Paiement.objects.select_related('eleve__etablissement', 'encaisse_par'), pk=pk)
    facture = facture_active(paiement)
    eleve = Eleve.objects.avec_totaux().get(pk=paiement.eleve_id)
    return render(request, 'ecole/recu.html', {
        'p': paiement,
        'f': facture,
        'eleve': eleve,
        'ecole': eleve.etablissement,
        'parametres': Parametres.courant(),
        'qr': mark_safe(pdf.svg_qr(facture.qr_code)) if facture and facture.qr_code else '',
        'taxes': pdf.montants_taxes(facture) if facture else None,
        'factures': paiement.factures.order_by('date'),
        'affectations': paiement.imputations.all(),
        'historique': paiement.historique.select_related('valide_par', 'demande_par'),
        'demandes': paiement.demandes.filter(statut=StatutDemande.EN_ATTENTE),
    })


@login_required
@permission_required('ecole.view_paiement', raise_exception=True)
def recu_pdf(request, pk):
    paiement = get_object_or_404(Paiement.objects.select_related('eleve__etablissement', 'encaisse_par'), pk=pk)
    facture = facture_active(paiement)
    if facture:
        return _pdf_facture(facture)
    reponse = HttpResponse(pdf.pdf_recu(paiement), content_type='application/pdf')
    reponse['Content-Disposition'] = f'inline; filename="recu-{paiement.numero_recu}.pdf"'
    return reponse


def _pdf_facture(facture):
    reponse = HttpResponse(pdf.pdf_facture(facture), content_type='application/pdf')
    reponse['Content-Disposition'] = f'inline; filename="facture-{facture.numero}.pdf"'
    return reponse


@login_required
@permission_required('ecole.view_paiement', raise_exception=True)
def facture_pdf(request, pk):
    return _pdf_facture(get_object_or_404(Facture.objects.select_related('eleve__etablissement', 'paiement'), pk=pk))


@login_required
@permission_required('ecole.valider_corrections', raise_exception=True)
@require_POST
def facture_normaliser(request, pk):
    facture = get_object_or_404(Facture, pk=pk)
    if facture.statut != StatutFacture.NORMALISEE:
        normaliser_facture(facture, request.user)
        if facture.statut == StatutFacture.NORMALISEE:
            messages.success(request, _('Facture %(numero)s normalisée.') % {'numero': facture.numero})
        else:
            messages.error(request, _('Normalisation impossible : %(erreur)s') % {'erreur': facture.erreur or _('e-MECeF n’est pas activé.')})
    suivant = request.POST.get('next', '')
    if not url_has_allowed_host_and_scheme(suivant, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        suivant = reverse('ecole:factures')
    return redirect(suivant)


@login_required
@permission_required('ecole.view_facture', raise_exception=True)
def factures(request):
    qs = Facture.objects.select_related('eleve', 'paiement').order_by('-date')
    statut = request.GET.get('statut', '')
    if statut in StatutFacture.values:
        qs = qs.filter(statut=statut)
    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(Q(numero__icontains=q) | Q(eleve__nom__icontains=q) | Q(eleve__prenom__icontains=q)
                       | Q(code_mecef__icontains=q))
    page = Paginator(qs, 50).get_page(request.GET.get('page'))
    params = request.GET.copy()
    params.pop('page', None)
    return render(request, 'ecole/factures.html', {
        'page': page, 'statut': statut, 'q': q, 'statuts': StatutFacture.choices, 'params': params.urlencode(),
        'nb_a_normaliser': Facture.objects.exclude(statut=StatutFacture.NORMALISEE).filter(
            eleve__etablissement__emecef_actif=True).count(),
    })


@login_required
@permission_required('ecole.view_paiement', raise_exception=True)
def journal(request):
    qs = filtrer_paiements(request.GET)
    page = Paginator(qs, 50).get_page(request.GET.get('page'))
    params = request.GET.copy()
    params.pop('page', None)
    return render(request, 'ecole/journal.html', {
        'page': page,
        'total': qs.aggregate(t=Sum('montant'))['t'] or 0,
        'nb': qs.count(),
        'f': request.GET,
        'params': params.urlencode(),
        'modes': ModePaiement.choices,
        'motifs': Motif.choices,
        'classes': classes_existantes(),
        'etablissements': etablissements_utilises(),
    })


@login_required
@permission_required('ecole.delete_paiement', raise_exception=True)
def paiement_supprimer(request, pk):
    paiement = get_object_or_404(Paiement.objects.select_related('eleve'), pk=pk)
    form = MotifForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        eleve = paiement.eleve
        numero = paiement.numero_recu
        annuler_paiement(paiement, form.cleaned_data['motif'], request.user)
        messages.success(request, _('Paiement %(numero_recu)s annulé ; une facture d’avoir a été émise.') % {'numero_recu': numero})
        return redirect('ecole:eleve_detail', pk=eleve.pk)
    return render(request, 'ecole/paiement_annuler.html', {
        'p': paiement, 'form': form,
        'annuler_url': reverse('ecole:recu', args=[paiement.pk]),
    })


@login_required
def paiement_corriger(request, pk):
    """Direction : corrige directement. Caisse / secrétariat : envoie une demande à valider par la direction."""
    paiement = get_object_or_404(Paiement.objects.select_related('eleve__etablissement'), pk=pk)
    directe = request.user.has_perm('ecole.valider_corrections')
    if not (directe or request.user.has_perm('ecole.add_demandecorrection')):
        raise PermissionDenied
    form = CorrectionForm(request.POST or None, paiement=paiement)
    if request.method == 'POST' and form.is_valid():
        action, motif = form.cleaned_data['action'], form.cleaned_data['motif']
        if directe:
            if action == ActionCorrection.ANNULATION:
                eleve = paiement.eleve
                annuler_paiement(paiement, motif, request.user)
                messages.success(request, _('Paiement annulé ; une facture d’avoir a été émise.'))
                return redirect('ecole:eleve_detail', pk=eleve.pk)
            nouvelle = corriger_paiement(paiement, form.valeurs(), motif, request.user)
            messages.success(request, _('Paiement corrigé : avoir émis et nouvelle facture %(numero)s envoyée.') % {
                'numero': nouvelle.numero})
            return redirect('ecole:recu', pk=paiement.pk)
        DemandeCorrection.objects.create(
            eleve=paiement.eleve, paiement=paiement, numero_recu=paiement.numero_recu, action=action, motif=motif,
            nouvelles_valeurs=form.valeurs_json() if action == ActionCorrection.MODIFICATION else {},
            demande_par=request.user,
        )
        messages.success(request, _('Demande de correction envoyée à la direction pour validation.'))
        return redirect('ecole:recu', pk=paiement.pk)
    return render(request, 'ecole/paiement_corriger.html', {'p': paiement, 'form': form, 'directe': directe})


@login_required
def corrections(request):
    peut_valider = request.user.has_perm('ecole.valider_corrections')
    if not (peut_valider or request.user.has_perm('ecole.view_demandecorrection')):
        raise PermissionDenied
    qs = DemandeCorrection.objects.select_related('eleve', 'paiement', 'demande_par', 'traite_par')
    if not peut_valider:
        qs = qs.filter(demande_par=request.user)
    return render(request, 'ecole/corrections.html', {
        'en_attente': qs.filter(statut=StatutDemande.EN_ATTENTE),
        'traitees': qs.exclude(statut=StatutDemande.EN_ATTENTE)[:30],
        'peut_valider': peut_valider,
    })


@login_required
@permission_required('ecole.valider_corrections', raise_exception=True)
@require_POST
@transaction.atomic
def correction_traiter(request, pk):
    # select_for_update : deux validations simultanées (double clic, deux directeurs) ne s'appliquent qu'une fois.
    demande = get_object_or_404(DemandeCorrection.objects.select_for_update(), pk=pk, statut=StatutDemande.EN_ATTENTE)
    if demande.paiement_id is None:
        demande.statut, demande.commentaire = StatutDemande.REFUSEE, _('Paiement déjà annulé.')
        demande.save()
        messages.error(request, _('Ce paiement n’existe plus.'))
        return redirect('ecole:corrections')
    accepter = request.POST.get('decision') == 'valider'
    if accepter and demande.action == ActionCorrection.MODIFICATION:
        # Revalide les valeurs demandées sur la situation actuelle de l'élève (reste à payer, date...).
        controle = CorrectionForm({**demande.nouvelles_valeurs, 'motif': demande.motif}, paiement=demande.paiement,
                                  avec_action=False)
        if not controle.is_valid():
            erreurs = ' '.join(e for errs in controle.errors.values() for e in errs)
            messages.error(request, _('Demande non applicable en l’état : %(erreurs)s') % {'erreurs': erreurs})
            return redirect('ecole:corrections')
    valider_demande(demande, request.user, accepter, request.POST.get('commentaire', '')[:200])
    messages.success(request, _('Demande validée et appliquée.') if accepter else _('Demande refusée.'))
    return redirect('ecole:corrections')


@login_required
@permission_required('ecole.view_eleve', raise_exception=True)
def suivi(request):
    statut = request.GET.get('statut') if request.GET.get('statut') in ('payes', 'retard', 'inscription') else 'impayes'
    classe = request.GET.get('classe', '')
    etab_id = etab_demande(request)
    base = Eleve.objects.actifs().filter(etablissement_id=etab_id) if etab_id else Eleve.objects.actifs()
    eleves = base.soldes() if statut == 'payes' else base.avec_totaux() if statut == 'inscription' else base.impayes()
    if classe:
        eleves = eleves.filter(classe=classe)
    eleves = list(eleves.select_related('etablissement').prefetch_related('echeances'))
    impayes = list(base.impayes().prefetch_related('echeances'))
    en_retard = [e for e in impayes if e.situation.en_retard]
    if statut == 'retard':
        eleves = sorted((e for e in eleves if e.situation.en_retard), key=lambda e: -e.situation.montant_en_retard)
    inscriptions_dues = [e for e in base.avec_totaux() if e.inscription_reste]
    if statut == 'inscription':
        eleves = sorted((e for e in eleves if e.inscription_reste), key=lambda e: -e.inscription_reste)
    return render(request, 'ecole/suivi.html', {
        'eleves': eleves,
        'statut': statut,
        'reste_total': sum(e.reste_a_payer for e in eleves),
        'nb_payes': base.soldes().count(),
        'nb_impayes': len(impayes),
        'nb_retard': len(en_retard),
        'nb_inscription': len(inscriptions_dues),
        'inscription_total': sum(e.inscription_reste for e in eleves) if statut == 'inscription' else 0,
        'retard_total': sum(e.situation.montant_en_retard for e in eleves) if statut == 'retard' else 0,
        'temps_reel': True,
        'params_onglet': sans_statut(request),
        **contexte_filtres(request, etab_id, classe),
    })


def sans_statut(request):
    params = request.GET.copy()
    params.pop('statut', None)
    return params.urlencode()
