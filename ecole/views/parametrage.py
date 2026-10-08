"""Paramétrage de l'établissement : identité fiscale, grilles des frais, règles de relance (Administration)."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from ..echeancier import appliquer_reductions, regenerer_classe
from ..forms import EtablissementForm, GrilleForm, RegleRelanceForm, TranchesFormSet
from ..models import Eleve, Etablissement, GrilleFrais, RegleRelance, TrancheGrille
from ..models import CanalMessage, Notification, StatutMessage, TypeMessage, numero_international
from ..notifications import canal_configure, executer_relances, fournisseur_sms, nombre_sms, texte_sms, transmettre
from .eleves import classes_existantes


@login_required
@permission_required('ecole.gerer_utilisateurs', raise_exception=True)
def etablissement_modifier(request, pk):
    etab = get_object_or_404(Etablissement, pk=pk)
    fratrie_avant = (etab.reduction_fratrie_2, etab.reduction_fratrie_3)
    form = EtablissementForm(request.POST or None, instance=etab)
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            etab = form.save()
            if fratrie_avant != (etab.reduction_fratrie_2, etab.reduction_fratrie_3):
                for eleve in Eleve.objects.filter(etablissement=etab):
                    appliquer_reductions(eleve)
        messages.success(request, _('« %(nom)s » enregistré.') % {'nom': etab.nom})
        return redirect('ecole:parametres')
    return render(request, 'ecole/etablissement_form.html', {'form': form, 'etab': etab})


# --------------------------------------------------------------------------- Grilles des frais

@login_required
@permission_required('ecole.view_grillefrais', raise_exception=True)
def grilles(request):
    liste = list(GrilleFrais.objects.select_related('etablissement').prefetch_related('tranches'))
    avec_grille = {(g.etablissement_id, g.classe) for g in liste}
    sans_grille = [
        (e, nom, c) for e, nom, c in Eleve.objects.actifs().values_list('etablissement_id', 'etablissement__nom', 'classe')
        .order_by('etablissement__nom', 'classe').distinct() if (e, c) not in avec_grille
    ]
    return render(request, 'ecole/grilles.html', {'grilles': liste, 'sans_grille': sans_grille,
                                                   'plusieurs': Etablissement.objects.count() > 1})


@login_required
@permission_required('ecole.change_grillefrais', raise_exception=True)
def grille_form(request, pk=None):
    grille = get_object_or_404(GrilleFrais, pk=pk) if pk else GrilleFrais()
    initial = {}
    if not pk:
        etab = request.GET.get('etab', '')
        initial = {'classe': request.GET.get('classe', ''),
                   'etablissement': int(etab) if etab.isdigit() else (Etablissement.objects.first() if Etablissement.objects.count() == 1 else None)}
    form = GrilleForm(request.POST or None, instance=grille, initial=initial)
    formset = TranchesFormSet(request.POST or None, instance=grille, prefix='tranches')
    if request.method == 'POST' and form.is_valid() and formset.is_valid():
        with transaction.atomic():
            ancienne = (grille.etablissement_id, grille.classe) if pk else None
            grille = form.save()
            formset.instance = grille
            tranches = formset.save(commit=False)
            for t in formset.deleted_objects:
                t.delete()
            for i, t in enumerate(tranches, start=1):
                if not t.pk:
                    t.numero = 500 + i
                t.grille = grille
                t.save()
            ordre = list(grille.tranches.order_by('echeance', 'numero'))
            for n, t in enumerate(ordre, start=1):
                TrancheGrille.objects.filter(pk=t.pk).update(numero=1000 + n)
            for n, t in enumerate(ordre, start=1):
                TrancheGrille.objects.filter(pk=t.pk).update(numero=n)
                if not t.libelle:
                    TrancheGrille.objects.filter(pk=t.pk).update(libelle=_('Tranche %(n)s') % {'n': n})
            regenerer_classe(grille.etablissement, grille.classe)
            if ancienne and ancienne != (grille.etablissement_id, grille.classe):
                regenerer_classe(Etablissement.objects.get(pk=ancienne[0]), ancienne[1])
        nb = Eleve.objects.filter(etablissement=grille.etablissement, classe=grille.classe).count()
        messages.success(request, _('Grille de la %(classe)s enregistrée ; échéancier mis à jour pour %(n)s élève(s).') % {
            'classe': grille.classe, 'n': nb})
        return redirect('ecole:grilles')
    return render(request, 'ecole/grille_form.html', {
        'form': form, 'formset': formset, 'grille': grille if pk else None, 'classes': classes_existantes(),
    })


@login_required
@permission_required('ecole.delete_grillefrais', raise_exception=True)
def grille_supprimer(request, pk):
    grille = get_object_or_404(GrilleFrais, pk=pk)
    if request.method == 'POST':
        etab, classe = grille.etablissement, grille.classe
        grille.delete()
        regenerer_classe(etab, classe)
        messages.success(request, _('Grille de la %(classe)s supprimée.') % {'classe': classe})
        return redirect('ecole:grilles')
    return render(request, 'ecole/confirmer_suppression.html', {
        'titre': _('Supprimer la grille'),
        'message': _('Supprimer la grille de la %(classe)s ? Les élèves gardent le montant actuel mais n’ont plus de dates d’échéance (sauf échéancier personnalisé).') % {'classe': grille.classe},
        'annuler_url': reverse('ecole:grilles'),
    })


@login_required
@permission_required('ecole.add_grillefrais', raise_exception=True)
@require_POST
def grille_copier(request, pk):
    """Copie une grille vers d'autres classes (mêmes tranches, montants et dates)."""
    source = get_object_or_404(GrilleFrais.objects.prefetch_related('tranches'), pk=pk)
    copiees = 0
    for cle in request.POST.getlist('classes'):
        etab_id, _sep, classe = cle.partition('|')
        if not etab_id.isdigit() or not classe:
            continue
        with transaction.atomic():
            grille, _cree = GrilleFrais.objects.update_or_create(
                etablissement_id=int(etab_id), classe=classe, defaults={'frais_inscription': source.frais_inscription})
            grille.tranches.all().delete()
            TrancheGrille.objects.bulk_create([
                TrancheGrille(grille=grille, numero=t.numero, libelle=t.libelle, montant=t.montant, echeance=t.echeance)
                for t in source.tranches.all()
            ])
            regenerer_classe(grille.etablissement, classe)
        copiees += 1
    messages.success(request, _('Grille copiée vers %(n)s classe(s).') % {'n': copiees})
    return redirect('ecole:grilles')


# --------------------------------------------------------------------------- Règles de relance

@login_required
@permission_required('ecole.view_reglerelance', raise_exception=True)
def regles_relance(request):
    regles = list(RegleRelance.objects.select_related('etablissement'))
    for r in regles:
        exemple = texte_sms(r.message.replace('{parent}', 'Mme HOUNSOU').replace('{eleve}', 'Rachel HOUNSOU')
                            .replace('{classe}', 'CM2').replace('{tranche}', 'Tranche 2').replace('{montant}', '35 000 FCFA')
                            .replace('{echeance}', '15/01/2027').replace('{reste}', '70 000 FCFA').replace('{retard}', '3')
                            .replace('{ecole}', 'CS Romaric'))
        r.nb_sms = nombre_sms(exemple)
    return render(request, 'ecole/regles_relance.html', {
        'regles': regles,
        'canaux': {c: canal_configure(c) for c in ('whatsapp', 'sms', 'email')},
        'fournisseur_sms': fournisseur_sms(),
        'essai_texte': _('Test FinMob : ce SMS de relance est un essai. Merci de ne pas repondre.'),
    })


@login_required
@permission_required('ecole.change_reglerelance', raise_exception=True)
@require_POST
def sms_essai(request):
    """Envoie un SMS d'essai à un numéro saisi, pour vérifier que les relances partent vraiment."""
    numero = numero_international(request.POST.get('numero', ''))
    texte = texte_sms(request.POST.get('message', '').strip())[:300]
    if len(numero) < 8 or not texte:
        messages.error(request, _('Indiquez un numéro de téléphone et un message.'))
        return redirect('ecole:regles_relance')
    statut, erreur = StatutMessage.ECHEC, ''
    try:
        statut = transmettre(CanalMessage.SMS, numero, texte)
    except Exception as e:  # noqa: BLE001 — affiché à l'écran et noté au journal
        erreur = str(e)[:1000]
    Notification.objects.create(canal=CanalMessage.SMS, type=TypeMessage.RAPPEL, destinataire=numero,
                                objet=_('SMS d’essai'), message=texte, statut=statut, erreur=erreur)
    if statut == StatutMessage.ENVOYE:
        messages.success(request, _('SMS d’essai transmis au +%(n)s : vérifiez qu’il est bien arrivé sur le téléphone.') % {
            'n': numero})
    elif statut == StatutMessage.SIMULE:
        messages.warning(request, _('SMS simulé : aucun moyen d’envoi n’est encore branché, rien n’est parti. '
                                    'Suivez la procédure « Brancher les SMS » sur cette page.'))
    else:
        messages.error(request, _('Échec de l’envoi : %(e)s') % {'e': erreur})
    return redirect('ecole:regles_relance')


@login_required
@permission_required('ecole.change_reglerelance', raise_exception=True)
def regle_relance_form(request, pk=None):
    regle = get_object_or_404(RegleRelance, pk=pk) if pk else RegleRelance(
        message=_('Bonjour {parent}, la {tranche} de la scolarité de {eleve} ({classe}), soit {montant}, '
                  'est attendue le {echeance}. Merci. — {ecole}'))
    form = RegleRelanceForm(request.POST or None, instance=regle)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, _('Règle de relance enregistrée.'))
        return redirect('ecole:regles_relance')
    return render(request, 'ecole/regle_relance_form.html', {'form': form, 'regle': regle if pk else None})


@login_required
@permission_required('ecole.delete_reglerelance', raise_exception=True)
@require_POST
def regle_relance_supprimer(request, pk):
    get_object_or_404(RegleRelance, pk=pk).delete()
    messages.success(request, _('Règle supprimée.'))
    return redirect('ecole:regles_relance')


@login_required
@permission_required('ecole.change_reglerelance', raise_exception=True)
@require_POST
def relances_lancer(request):
    n = executer_relances()
    messages.success(request, _('%(n)s message(s) de rappel envoyé(s) aujourd’hui.') % {'n': n} if n
                     else _('Aucun rappel à envoyer aujourd’hui (ou déjà envoyés).'))
    return redirect('ecole:regles_relance')
