from itertools import groupby

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from .. import roles
from ..echeancier import (
    appliquer_reductions, cle_texte, detail_reductions, freres_et_soeurs, generer_echeancier, grille_de,
    recalculer_fratrie,
)
from ..facturation import enregistrer_paiement
from ..forms import (
    AccesParentForm, ContactParentForm, EcheancesFormSet, EleveForm, ImportForm, InscriptionForm, ReductionForm,
)
from ..templatetags.ecole_extras import fcfa
from ..importation import GROUPES, PAR_CHAMP, analyser, empreinte, import_precedent, importer, lire_lignes, modele_xlsx
from ..models import (
    TRANCHES, CompteParent, ContactParent, Echeance, Eleve, Etablissement, GrilleFrais, Paiement, ReductionEleve,
    StatutDemande, TypeFacture,
)


def classes_existantes(etab_id=None):
    qs = Eleve.objects.actifs()
    if etab_id:
        qs = qs.filter(etablissement_id=etab_id)
    return qs.order_by('classe').values_list('classe', flat=True).distinct()


def frais_par_classe():
    """Frais de chaque classe pour le formulaire d'inscription : {« établissement|classe » : {...}}."""
    donnees = {}
    for g in GrilleFrais.objects.select_related('etablissement').prefetch_related('tranches'):
        tranches = list(g.tranches.all())
        if not tranches:
            continue
        donnees[f'{cle_texte(g.etablissement.nom)}|{cle_texte(g.classe)}'] = {
            'etablissement': g.etablissement.nom, 'classe': g.classe,
            'scolarite': sum(t.montant for t in tranches),
            'inscription': g.frais_inscription,
            'tranches': [{'libelle': str(t), 'montant': t.montant, 'echeance': t.echeance.strftime('%d/%m/%Y')}
                         for t in tranches],
        }
    return donnees


def _fratrie_attendue(frere):
    """Pour l'inscription d'un frère / d'une sœur : rang du nouvel enfant et réduction sur l'inscription."""
    if frere is None:
        return {'rang': 0, 'taux': 0}
    rang = len(freres_et_soeurs(frere)) + 1
    etab = frere.etablissement
    taux = etab.reduction_inscription_2 if rang == 2 else etab.reduction_inscription_3 if rang >= 3 else 0
    return {'rang': rang, 'taux': taux}


def classes_proposees():
    """Classes connues : celles des élèves et celles qui ont des frais définis."""
    return sorted(set(classes_existantes()) | set(GrilleFrais.objects.values_list('classe', flat=True)))


def etablissements_utilises():
    return Etablissement.objects.filter(eleves__isnull=False).distinct()


def etab_demande(request):
    """Identifiant de l'établissement choisi dans les filtres (paramètre « etab »), ou None."""
    valeur = request.GET.get('etab', '')
    return int(valeur) if valeur.isdigit() else None


def sans(request, *cles):
    """Paramètres de la requête actuelle, sans les clés données (pour construire les liens de filtre)."""
    params = request.GET.copy()
    for cle in cles + ('page',):
        params.pop(cle, None)
    return params.urlencode()


def contexte_filtres(request, etab_id, classe=''):
    """Variables communes aux barres de filtres « établissement » et « classe »."""
    return {
        'etablissements': etablissements_utilises(),
        'etab_actif': etab_id,
        'classes': classes_existantes(etab_id),
        'classe_active': classe,
        'extra': sans(request, 'classe'),
        'extra_etab': sans(request, 'etab', 'classe'),
    }


def filtrer_recherche(qs, q):
    if not q:
        return qs
    return qs.filter(
        Q(nom__icontains=q) | Q(prenom__icontains=q) | Q(parent__icontains=q)
        | Q(educ_master__icontains=q) | Q(telephone__icontains=q)
    )


@login_required
@permission_required('ecole.view_eleve', raise_exception=True)
def liste_par_classe(request):
    classe = request.GET.get('classe', '')
    q = request.GET.get('q', '').strip()
    etab_id = etab_demande(request)
    eleves = filtrer_recherche(Eleve.objects.actifs().avec_totaux().select_related('etablissement'), q)
    if etab_id:
        eleves = eleves.filter(etablissement_id=etab_id)
    if classe:
        eleves = eleves.filter(classe=classe)
    groupes = []
    for (etab, nom_classe), items in groupby(eleves, key=lambda e: (e.etablissement, e.classe)):
        items = list(items)
        groupes.append({
            'etablissement': etab,
            'classe': nom_classe,
            'eleves': items,
            'nb_soldes': sum(1 for e in items if e.statut == 'solde'),
        })
    return render(request, 'ecole/eleves_liste.html', {
        'groupes': groupes,
        'nb_resultats': sum(len(g['eleves']) for g in groupes),
        'q': q,
        'aucun_eleve': not Eleve.objects.actifs().exists(),
        'nb_archives': Eleve.objects.archives().count(),
        'params': sans(request),
        **contexte_filtres(request, etab_id, classe),
    })




@login_required
@permission_required('ecole.add_eleve', raise_exception=True)
def eleve_nouveau(request):
    avec_paiements = request.user.has_perm('ecole.add_paiement')
    initial, frere = {}, None
    if request.GET.get('frere', '').isdigit():
        # Inscription d'un frère ou d'une sœur : parent, contacts et établissement repris de la fiche.
        frere = Eleve.objects.filter(pk=int(request.GET['frere'])).select_related('etablissement').first()
        if frere:
            initial = {'etablissement_nom': frere.etablissement.nom, 'nom': frere.nom, 'parent': frere.parent,
                       'nature_parent': frere.nature_parent, 'telephone': frere.telephone, 'whatsapp': frere.whatsapp,
                       'email': frere.email, 'residence': frere.residence}
    form = InscriptionForm(request.POST if request.method == 'POST' else None, avec_paiements=avec_paiements,
                           initial=initial)
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            eleve = form.save()
            generer_echeancier(eleve, montant=None if form.grille else form.cleaned_data['montant_scolarite'])
            recalculer_fratrie(eleve)
        factures = []
        for motif, montant in (form.paiements_initiaux() if avec_paiements else []):
            eleve = Eleve.objects.avec_totaux().get(pk=eleve.pk)
            if motif in TRANCHES:
                montant = min(montant, eleve.reste_a_payer)
                if not montant:
                    continue
            elif eleve.frais_inscription:
                montant = min(montant, eleve.inscription_reste)
                if not montant:
                    continue
            paiement = Paiement(eleve=eleve, motif=motif, montant=montant, mode=form.cleaned_data['mode'])
            factures.append(enregistrer_paiement(paiement, request.user))
        messages.success(request, _('%(eleve)s a été inscrit(e).') % {'eleve': eleve})
        eleve = Eleve.objects.avec_totaux().get(pk=eleve.pk)
        if eleve.reduction_inscription:
            messages.info(request, _("Réduction sur les frais d'inscription : %(detail)s — à payer %(du)s au lieu de %(brut)s.") % {
                'detail': ', '.join(l for l, _m in eleve.reductions_inscription), 'du': fcfa(eleve.inscription_due),
                'brut': fcfa(eleve.frais_inscription)})
        if factures:
            messages.info(request, _('Facture(s) %(numeros)s émise(s) et envoyée(s) au parent.') % {
                'numeros': ', '.join(f.numero for f in factures)})
        return redirect('ecole:eleve_detail', pk=eleve.pk)
    return render(request, 'ecole/eleve_form.html', {
        'form': form,
        'titre': _('Inscrire un frère / une sœur de %(eleve)s') % {'eleve': frere} if frere else _('Inscrire un élève'),
        'bouton': _("Enregistrer l'inscription"),
        'classes': classes_proposees(),
        'etablissements': Etablissement.objects.all(),
        'avec_paiements': avec_paiements,
        'frais_classes': frais_par_classe(),
        'fratrie_inscription': _fratrie_attendue(frere),
    })


@login_required
@permission_required('ecole.view_eleve', raise_exception=True)
def eleve_detail(request, pk):
    eleve = get_object_or_404(Eleve.objects.avec_totaux().select_related('etablissement')
                              .prefetch_related('echeances', 'reductions', 'contacts'), pk=pk)
    paiements = list(eleve.paiements.select_related('encaisse_par').prefetch_related('factures', 'imputations'))
    for p in paiements:
        p.facture_active = next((f for f in p.factures.all() if f.type == TypeFacture.VENTE and not f.annulee), None)
    brut = sum(e.montant_brut for e in eleve.echeances.all())
    return render(request, 'ecole/eleve_detail.html', {
        'eleve': eleve,
        'sit': eleve.situation,
        'paiements': paiements,
        'inscription': eleve.total_inscription,
        'grille': grille_de(eleve),
        'fratrie': freres_et_soeurs(eleve),
        'reductions': detail_reductions(eleve, brut) if brut else [],
        'total_brut': brut,
        'reduction_form': ReductionForm(),
        'contact_form': ContactParentForm(),
        'acces_form': AccesParentForm(initial={'identifiant': eleve.telephone_international}),
        'comptes_parents': eleve.comptes_parents.select_related('utilisateur'),
        'historique': eleve.historique_paiements.select_related('valide_par', 'demande_par')[:10],
        'demandes': eleve.demandes_correction.filter(statut=StatutDemande.EN_ATTENTE),
        'notifications': eleve.notifications.all()[:8],
        'relances': eleve.relances.select_related('envoye_par')[:5],
        'temps_reel': True,
    })


@login_required
@permission_required('ecole.view_eleve', raise_exception=True)
def eleve_fiche_pdf(request, pk):
    """Télécharge la fiche complète de l'élève en PDF."""
    from .. import pdf

    eleve = get_object_or_404(Eleve.objects.avec_totaux().select_related('etablissement')
                              .prefetch_related('echeances', 'reductions', 'contacts'), pk=pk)
    brut = sum(e.montant_brut for e in eleve.echeances.all())
    paiements = list(eleve.paiements.prefetch_related('imputations').order_by('date', 'cree_le', 'pk'))
    contenu = pdf.pdf_fiche_eleve(eleve, eleve.situation, paiements, detail_reductions(eleve, brut) if brut else [],
                                  fratrie=freres_et_soeurs(eleve))
    nom = slugify(f'fiche {eleve.nom} {eleve.prenom}') or 'fiche-eleve'
    reponse = HttpResponse(contenu, content_type='application/pdf')
    reponse['Content-Disposition'] = f'attachment; filename="{nom}.pdf"'
    return reponse


@login_required
@permission_required('ecole.change_eleve', raise_exception=True)
def eleve_modifier(request, pk):
    eleve = get_object_or_404(Eleve, pk=pk)
    avant = (eleve.etablissement_id, eleve.classe, eleve.telephone_international)
    form = EleveForm(request.POST if request.method == 'POST' else None, instance=eleve, utilisateur=request.user)
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            eleve = form.save()
            apres = (eleve.etablissement_id, eleve.classe, eleve.telephone_international)
            if not form.grille and not eleve.echeancier_personnalise:
                generer_echeancier(eleve, montant=form.cleaned_data['montant_scolarite'])  # tranche unique
            elif avant[:2] != apres[:2] or not eleve.echeances.exists():
                generer_echeancier(eleve)
            if avant != apres:
                detacher_acces_obsoletes(eleve)
                recalculer_fratrie(eleve)
                for ancien in Eleve.objects.filter(etablissement_id=avant[0]).exclude(pk=eleve.pk):
                    if ancien.telephone_international == avant[2]:
                        recalculer_fratrie(ancien)
                        break
            appliquer_reductions(eleve)
        messages.success(request, _('Fiche mise à jour.'))
        return redirect('ecole:eleve_detail', pk=eleve.pk)
    return render(request, 'ecole/eleve_form.html', {
        'form': form,
        'eleve': eleve,
        'titre': _('Modifier la fiche'),
        'bouton': _('Enregistrer les modifications'),
        'classes': classes_existantes(),
        'etablissements': Etablissement.objects.all(),
        'frais_classes': frais_par_classe(),
    })


@login_required
@permission_required('ecole.archiver_eleve', raise_exception=True)
def eleve_archiver(request, pk):
    """Un élève n'est jamais supprimé : il est archivé (son historique, ses paiements et factures sont conservés)."""
    eleve = get_object_or_404(Eleve, pk=pk, archive=False)
    if request.method == 'POST':
        motif = request.POST.get('motif', '').strip()[:200]
        if not motif:
            messages.error(request, _("Indiquez le motif de l'archivage (départ, transfert, abandon…)."))
            return redirect('ecole:eleve_archiver', pk=eleve.pk)
        fratrie = [e for e in freres_et_soeurs(eleve) if e.pk != eleve.pk]
        eleve.archive, eleve.archive_le, eleve.archive_par, eleve.motif_archive = True, timezone.now(), request.user, motif
        eleve.save(update_fields=['archive', 'archive_le', 'archive_par', 'motif_archive', 'modifie_le'])
        for e in fratrie:
            appliquer_reductions(e)  # le rang des frères et sœurs restants peut changer
        messages.success(request, _('%(eleve)s a été archivé(e). Sa fiche reste consultable dans « Élèves archivés ».') % {
            'eleve': eleve})
        return redirect('ecole:eleves')
    return render(request, 'ecole/eleve_archiver.html', {'eleve': eleve})


@login_required
@permission_required('ecole.archiver_eleve', raise_exception=True)
@require_POST
def eleve_reactiver(request, pk):
    eleve = get_object_or_404(Eleve, pk=pk, archive=True)
    eleve.archive, eleve.archive_le, eleve.archive_par, eleve.motif_archive = False, None, None, ''
    eleve.save(update_fields=['archive', 'archive_le', 'archive_par', 'motif_archive', 'modifie_le'])
    recalculer_fratrie(eleve)
    messages.success(request, _('%(eleve)s est de nouveau actif(ve).') % {'eleve': eleve})
    return redirect('ecole:eleve_detail', pk=eleve.pk)


@login_required
@permission_required('ecole.view_eleve', raise_exception=True)
def eleves_archives(request):
    q = request.GET.get('q', '').strip()
    eleves = filtrer_recherche(Eleve.objects.archives().avec_totaux().select_related('etablissement', 'archive_par'), q)
    return render(request, 'ecole/eleves_archives.html', {'eleves': eleves.order_by('-archive_le'), 'q': q})


# --------------------------------------------------------------------------- Échéancier, réductions, contacts

@login_required
@permission_required('ecole.change_echeance', raise_exception=True)
def eleve_echeancier(request, pk):
    """Personnalise l'échéancier d'un élève (montants et dates), ou revient à la grille de la classe."""
    eleve = get_object_or_404(Eleve.objects.avec_totaux(), pk=pk)
    if request.method == 'POST' and 'grille' in request.POST:
        generer_echeancier(eleve, force=True)
        messages.success(request, _('Échéancier recalculé depuis la grille de la classe.'))
        return redirect('ecole:eleve_detail', pk=eleve.pk)
    formset = EcheancesFormSet(request.POST if request.method == 'POST' else None,
                               queryset=eleve.echeances.all(), prefix='ech')
    if request.method == 'POST' and formset.is_valid():
        with transaction.atomic():
            instances = formset.save(commit=False)
            for obj in formset.deleted_objects:
                obj.delete()
            for i, obj in enumerate(instances, start=1):
                obj.eleve = eleve
                if not obj.pk:
                    obj.numero = 5000 + i  # numéro provisoire, renuméroté ci-dessous
                obj.save()
            for n, e in enumerate(eleve.echeances.order_by('echeance', 'pk'), start=1):
                Echeance.objects.filter(pk=e.pk).update(numero=n + 1000)
            for n, e in enumerate(eleve.echeances.order_by('numero'), start=1):
                Echeance.objects.filter(pk=e.pk).update(numero=n)
            eleve.echeancier_personnalise = True
            eleve.save(update_fields=['echeancier_personnalise'])
            appliquer_reductions(eleve)
        eleve.refresh_from_db()
        if eleve.montant_scolarite < Eleve.objects.avec_totaux().get(pk=eleve.pk).total_verse:
            messages.warning(request, _('Attention : le total de l’échéancier est inférieur à ce qui est déjà versé.'))
        messages.success(request, _('Échéancier personnalisé enregistré.'))
        return redirect('ecole:eleve_detail', pk=eleve.pk)
    return render(request, 'ecole/eleve_echeancier.html', {'eleve': eleve, 'formset': formset, 'grille': grille_de(eleve)})


@login_required
@permission_required('ecole.add_reductioneleve', raise_exception=True)
@require_POST
def reduction_ajouter(request, pk):
    eleve = get_object_or_404(Eleve, pk=pk)
    form = ReductionForm(request.POST)
    if form.is_valid():
        reduction = form.save(commit=False)
        reduction.eleve, reduction.cree_par = eleve, request.user
        reduction.save()
        appliquer_reductions(eleve)
        messages.success(request, _('Réduction ajoutée : elle est répartie sur les tranches restantes.'))
    else:
        messages.error(request, ' '.join(e for errs in form.errors.values() for e in errs))
    return redirect(f"{reverse('ecole:eleve_detail', args=[eleve.pk])}#echeancier")


@login_required
@permission_required('ecole.delete_reductioneleve', raise_exception=True)
@require_POST
def reduction_supprimer(request, pk):
    reduction = get_object_or_404(ReductionEleve.objects.select_related('eleve'), pk=pk)
    eleve = reduction.eleve
    reduction.delete()
    appliquer_reductions(eleve)
    messages.success(request, _('Réduction retirée.'))
    return redirect(f"{reverse('ecole:eleve_detail', args=[eleve.pk])}#echeancier")


@login_required
@permission_required('ecole.add_contactparent', raise_exception=True)
@require_POST
def contact_ajouter(request, pk):
    eleve = get_object_or_404(Eleve, pk=pk)
    form = ContactParentForm(request.POST)
    if form.is_valid():
        contact = form.save(commit=False)
        contact.eleve = eleve
        contact.save()
        messages.success(request, _('Contact ajouté.'))
    else:
        messages.error(request, ' '.join(e for errs in form.errors.values() for e in errs))
    return redirect(f"{reverse('ecole:eleve_detail', args=[eleve.pk])}#parents")


@login_required
@permission_required('ecole.delete_contactparent', raise_exception=True)
@require_POST
def contact_supprimer(request, pk):
    contact = get_object_or_404(ContactParent, pk=pk)
    contact.delete()
    messages.success(request, _('Contact retiré.'))
    return redirect(f"{reverse('ecole:eleve_detail', args=[contact.eleve_id])}#parents")


@login_required
@permission_required('ecole.add_compteparent', raise_exception=True)
@require_POST
def acces_parent(request, pk):
    """Crée l'accès à l'espace parents (ou rattache l'élève au compte existant du même numéro)."""
    eleve = get_object_or_404(Eleve, pk=pk)
    numero = eleve.telephone_international
    compte = next((c for c in CompteParent.objects.all() if c.telephone == numero), None) if numero else None
    if compte is None:
        form = AccesParentForm(request.POST)
        if not form.is_valid():
            messages.error(request, ' '.join(e for errs in form.errors.values() for e in errs))
            return redirect(f"{reverse('ecole:eleve_detail', args=[eleve.pk])}#parents")
        with transaction.atomic():
            nom, _sep, prenom = eleve.parent.partition(' ')
            user = User.objects.create_user(form.cleaned_data['identifiant'], email=eleve.email,
                                            password=form.cleaned_data['mot_de_passe'],
                                            first_name=prenom[:150], last_name=nom[:150])
            roles.definir_role(user, roles.PARENT)
            compte = CompteParent.objects.create(utilisateur=user, telephone=numero)
        messages.success(request, _('Accès parent créé : identifiant « %(identifiant)s ».') % {'identifiant': user.username})
    fratrie = [e for e in Eleve.objects.all() if numero and e.telephone_international == numero] or [eleve]
    compte.eleves.add(*fratrie)
    if compte.utilisateur.username != request.POST.get('identifiant', compte.utilisateur.username):
        messages.info(request, _('Ce numéro avait déjà un accès (« %(identifiant)s ») : l’élève y a été rattaché.') % {
            'identifiant': compte.utilisateur.username})
    return redirect(f"{reverse('ecole:eleve_detail', args=[eleve.pk])}#parents")


@login_required
@permission_required('ecole.delete_compteparent', raise_exception=True)
@require_POST
def acces_parent_retirer(request, pk, compte_pk):
    eleve = get_object_or_404(Eleve, pk=pk)
    compte = get_object_or_404(CompteParent, pk=compte_pk)
    compte.eleves.remove(eleve)
    messages.success(request, _('L’accès « %(identifiant)s » ne voit plus cet élève.') % {
        'identifiant': compte.utilisateur.username})
    return redirect(f"{reverse('ecole:eleve_detail', args=[eleve.pk])}#parents")


def detacher_acces_obsoletes(eleve):
    """Après un changement de téléphone : les accès parents d'un autre numéro ne voient plus l'élève."""
    for compte in eleve.comptes_parents.all():
        if compte.telephone != eleve.telephone_international:
            compte.eleves.remove(eleve)


# --------------------------------------------------------------------------- Import

CLE_IMPORT = 'import_eleves'


@login_required
@permission_required('ecole.add_eleve', raise_exception=True)
def eleves_import(request):
    """Étape 1 : analyse du fichier et aperçu (rien n'est créé). Étape 2 : confirmation."""
    if request.method == 'POST' and 'confirmer' in request.POST:
        attente = request.session.get(CLE_IMPORT)
        if not attente or attente.get('jeton') != request.POST.get('jeton'):
            if request.POST.get('jeton') and request.POST.get('jeton') in request.session.get(CLE_IMPORT + '_faits', []):
                messages.error(request, _('Cet import a déjà été enregistré : il ne peut pas être importé une seconde fois.'))
                return redirect('ecole:eleves')
            messages.error(request, _('Aperçu expiré : chargez de nouveau le fichier.'))
            return redirect('ecole:eleves_import')
        envoyer = bool(request.POST.get('envoyer_factures')) and request.user.has_perm('ecole.add_paiement')
        lignes = attente['lignes'] if request.user.has_perm('ecole.add_paiement') else [
            {**d, 'inscription_versee': 0, 'scolarite_versee': 0} for d in attente['lignes']]
        crees, factures = importer(lignes, request.user, envoyer_factures=envoyer,
                                   fichier={'empreinte': attente.get('empreinte', ''), 'nom': attente.get('nom', '')})
        del request.session[CLE_IMPORT]
        request.session[CLE_IMPORT + '_faits'] = (request.session.get(CLE_IMPORT + '_faits', []) + [attente['jeton']])[-20:]
        messages.success(request, _('%(n)s élève(s) importé(s), %(f)s paiement(s) repris avec leur facture.') % {
            'n': len(crees), 'f': len(factures)})
        if len(crees) < len(lignes):
            messages.warning(request, _('%(n)s élève(s) déjà dans la base entre-temps : non importé(s) une seconde fois.') % {
                'n': len(lignes) - len(crees)})
        return redirect('ecole:eleves')
    if request.method == 'POST' and 'annuler' in request.POST:
        request.session.pop(CLE_IMPORT, None)
        return redirect('ecole:eleves_import')

    form = ImportForm(request.POST or None, request.FILES or None)
    rapport = None
    if request.method == 'POST' and form.is_valid():
        try:
            lignes = lire_lignes(form.cleaned_data['fichier'])
        except Exception:  # noqa: BLE001 — fichier illisible
            messages.error(request, _('Fichier illisible : enregistrez-le au format .xlsx ou .csv.'))
            return redirect('ecole:eleves_import')
        rapport = analyser(lignes, etab_defaut=form.cleaned_data.get('etablissement', ''))
        precedent = import_precedent(lignes)
        if precedent:
            messages.warning(request, _('Ce fichier a déjà été importé le %(date)s par %(par)s (%(n)s élève(s) créé(s)). '
                                        'Les élèves déjà dans la base ne seront pas importés une seconde fois.') % {
                'date': timezone.localtime(precedent.importe_le).strftime('%d/%m/%Y à %H:%M'),
                'par': (precedent.importe_par.get_full_name() or precedent.importe_par.username) if precedent.importe_par else '—',
                'n': precedent.nb_eleves})
        if rapport.a_creer:
            import secrets

            request.session[CLE_IMPORT] = {'jeton': secrets.token_urlsafe(16), 'lignes': rapport.a_creer,
                                           'empreinte': empreinte(lignes), 'nom': form.cleaned_data['fichier'].name}
        else:
            request.session.pop(CLE_IMPORT, None)
            if rapport.deja and not rapport.erreurs:
                messages.error(request, _('Rien à importer : les %(n)s élève(s) de ce fichier sont déjà dans la base.') % {
                    'n': len(rapport.deja)})
            elif not rapport.erreurs:
                messages.info(request, _('Aucun nouvel élève à importer.'))
    attente = request.session.get(CLE_IMPORT) if rapport and rapport.a_creer else None
    return render(request, 'ecole/eleves_import.html', {
        'form': form, 'rapport': rapport, 'etablissements': Etablissement.objects.all(),
        'nb_ok': sum(1 for r in rapport.resultats if r['statut'] == 'ok') if rapport else 0,
        'nb_erreur': sum(1 for r in rapport.resultats if r['statut'] == 'erreur') if rapport else 0,
        'nb_ignore': sum(1 for r in rapport.resultats if r['statut'] == 'ignore') if rapport else 0,
        'nb_deja': len(rapport.deja) if rapport else 0, 'groupes': [(titre, [PAR_CHAMP[c] for c in champs]) for titre, champs in GROUPES],
        'jeton': attente['jeton'] if attente else '',
        'apercu': rapport.a_creer[:200] if rapport else [],
        'totaux': {
            'scolarite': sum(d['scolarite_versee'] for d in rapport.a_creer),
            'inscription': sum(d['inscription_versee'] for d in rapport.a_creer),
            'parents2': sum(1 for d in rapport.a_creer if d['parent2']),
            'reductions': sum(1 for d in rapport.a_creer if d['reductions']),
        } if rapport else {},
    })


@login_required
@permission_required('ecole.add_eleve', raise_exception=True)
def eleves_import_modele(request):
    reponse = HttpResponse(modele_xlsx(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    reponse['Content-Disposition'] = 'attachment; filename="modele-import-eleves.xlsx"'
    return reponse
