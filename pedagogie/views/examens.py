from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.utils.translation import gettext as _

from .. import pdf
from ..acces import professeur_de, voit_tout
from ..forms import CompositionForm, EpreuveForm
from ..models import Composition, Epreuve


@login_required
@permission_required('pedagogie.view_composition', raise_exception=True)
def liste(request):
    compositions = Composition.objects.select_related('etablissement').prefetch_related('epreuves')
    moi = professeur_de(request.user)
    if not voit_tout(request.user):
        compositions = compositions.filter(epreuves__surveillants=moi).distinct() if moi else compositions.none()
    compositions = list(compositions)
    return render(request, 'pedagogie/examens/liste.html', {
        'a_venir': [c for c in compositions if c.statut != 'terminee'][::-1],
        'terminees': [c for c in compositions if c.statut == 'terminee'],
        'mes_surveillances': Epreuve.objects.filter(surveillants=moi).select_related(
            'composition', 'enseignement__matiere').order_by('date', 'heure_debut') if moi else [],
    })


@login_required
@permission_required('pedagogie.add_composition', raise_exception=True)
def nouvelle(request):
    form = CompositionForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        c = form.save()
        messages.success(request, _('« %(c)s » créée : ajoutez maintenant les épreuves.') % {'c': c})
        return redirect('examens:detail', pk=c.pk)
    return render(request, 'pedagogie/examens/form.html', {'form': form, 'titre': _('Nouvelle composition')})


@login_required
@permission_required('pedagogie.change_composition', raise_exception=True)
def modifier(request, pk):
    comp = get_object_or_404(Composition, pk=pk)
    form = CompositionForm(request.POST if request.method == 'POST' else None, instance=comp)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, _('Composition mise à jour.'))
        return redirect('examens:detail', pk=comp.pk)
    return render(request, 'pedagogie/examens/form.html', {'form': form, 'titre': _('Modifier « %(comp)s »') % {'comp': comp}, 'comp': comp})


@login_required
@permission_required('pedagogie.view_composition', raise_exception=True)
def detail(request, pk):
    comp = get_object_or_404(Composition.objects.select_related('etablissement'), pk=pk)
    epreuves = list(comp.epreuves.select_related('enseignement__matiere', 'enseignement__professeur')
                    .prefetch_related('surveillants'))
    moi = professeur_de(request.user)
    if not voit_tout(request.user):
        epreuves = [e for e in epreuves if moi in e.surveillants.all() or e.enseignement.professeur_id == getattr(moi, 'pk', None)]
    form = None
    if request.user.has_perm('pedagogie.add_epreuve'):
        form = EpreuveForm(request.POST if request.method == 'POST' else None, composition=comp)
        if request.method == 'POST' and form.is_valid():
            e = form.save()
            messages.success(request, _('Épreuve de %(matiere)s (%(classe)s) ajoutée.') % {'matiere': e.enseignement.matiere, 'classe': e.enseignement.classe})
            return redirect('examens:detail', pk=comp.pk)
    if request.GET.get('format') == 'pdf':
        reponse = HttpResponse(pdf.planning_composition(comp, epreuves), content_type='application/pdf')
        reponse['Content-Disposition'] = 'inline; filename="planning-composition.pdf"'
        return reponse
    lignes = []
    for e in epreuves:
        saisies, effectif = e.avancement_saisie()
        lignes.append({'e': e, 'saisies': saisies, 'effectif': effectif,
                       'pct': round(saisies * 100 / effectif) if effectif else 0})
    jours = []
    for l in lignes:
        if not jours or jours[-1][0] != l['e'].date:
            jours.append((l['e'].date, []))
        jours[-1][1].append(l)
    return render(request, 'pedagogie/examens/detail.html', {
        'comp': comp, 'jours': jours, 'form': form, 'nb_epreuves': len(lignes),
        'libelle_periode': dict(comp.etablissement.periodes).get(comp.periode, f'Période {comp.periode}'),
    })


@login_required
@permission_required('pedagogie.delete_epreuve', raise_exception=True)
@require_POST
def epreuve_supprimer(request, pk):
    e = get_object_or_404(Epreuve, pk=pk)
    e.delete()
    messages.success(request, _('Épreuve supprimée.'))
    return redirect('examens:detail', pk=e.composition_id)


@login_required
@permission_required('pedagogie.delete_composition', raise_exception=True)
def supprimer(request, pk):
    comp = get_object_or_404(Composition, pk=pk)
    if request.method == 'POST':
        comp.delete()
        messages.success(request, _('Composition supprimée (les notes déjà saisies sont conservées).'))
        return redirect('examens:liste')
    return render(request, 'ecole/confirmer_suppression.html', {
        'titre': _('Supprimer la composition'),
        'message': _('Supprimer « %(comp)s » et ses %(epreuves)s épreuve(s) ? Les notes déjà saisies sont conservées.') % {'comp': comp, 'epreuves': comp.epreuves.count()},
        'annuler_url': reverse('examens:detail', args=[comp.pk]),
    })
