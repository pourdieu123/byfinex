from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.utils.translation import gettext as _

from ecole import roles
from ecole.models import Etablissement

from .. import pdf
from ..acces import professeur_de, verifier_professeur, voit_tout
from ..forms import CompteProfesseurForm, CreneauForm, HeureCoursForm, PaiementProfesseurForm, ProfesseurForm
from ..models import Creneau, Enseignement, HeureCours, Jour, PaiementProfesseur, Professeur


def semaine(creneaux):
    """[(jour, libellé, [créneaux])] du lundi au samedi."""
    par_jour = {j: [] for j in Jour.values}
    for c in sorted(creneaux, key=lambda c: (c.jour, c.heure_debut)):
        par_jour[c.jour].append(c)
    return [(j, Jour(j).label, par_jour[j]) for j in Jour.values]


@login_required
@permission_required('pedagogie.view_professeur', raise_exception=True)
def liste(request):
    if not voit_tout(request.user):
        prof = professeur_de(request.user)
        if not prof:
            raise PermissionDenied
        return redirect('profs:detail', pk=prof.pk)
    q = request.GET.get('q', '').strip()
    profs = Professeur.objects.prefetch_related('matieres', 'enseignements')
    if q:
        profs = profs.filter(Q(nom__icontains=q) | Q(prenoms__icontains=q) | Q(telephone__icontains=q)
                             | Q(matieres__nom__icontains=q)).distinct()
    profs = list(profs)
    return render(request, 'pedagogie/profs/liste.html', {
        'profs': profs, 'q': q,
        'nb_actifs': sum(1 for p in profs if p.actif),
        'total_du': sum(max(p.solde, 0) for p in profs),
        'sans_compte': sum(1 for p in profs if not p.utilisateur_id),
    })


@login_required
@permission_required('pedagogie.add_professeur', raise_exception=True)
def nouveau(request):
    form = ProfesseurForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        prof = form.save()
        messages.success(request, _('%(prof)s a été ajouté(e).') % {'prof': prof})
        return redirect('profs:detail', pk=prof.pk)
    return render(request, 'pedagogie/profs/form.html', {'form': form, 'titre': _('Nouveau professeur')})


@login_required
@permission_required('pedagogie.change_professeur', raise_exception=True)
def modifier(request, pk):
    prof = get_object_or_404(Professeur, pk=pk)
    form = ProfesseurForm(request.POST if request.method == 'POST' else None, instance=prof)
    if request.method == 'POST' and form.is_valid():
        etait_actif = Professeur.objects.filter(pk=prof.pk).values_list('actif', flat=True).first()
        form.save()
        compte = prof.utilisateur
        # Seule la désactivation suit la fiche, et seulement pour un compte « Professeur » ;
        # réactiver un compte se fait dans Administration › Utilisateurs.
        if (compte and etait_actif and not prof.actif and compte.is_active
                and roles.role_de(compte) == roles.PROFESSEUR):
            compte.is_active = False
            compte.save(update_fields=['is_active'])
        messages.success(request, _('Fiche mise à jour.'))
        return redirect('profs:detail', pk=prof.pk)
    return render(request, 'pedagogie/profs/form.html', {'form': form, 'titre': _('Modifier %(prof)s') % {'prof': prof}, 'prof': prof})


@login_required
@permission_required('pedagogie.view_professeur', raise_exception=True)
def detail(request, pk):
    prof = get_object_or_404(Professeur.objects.prefetch_related('matieres'), pk=pk)
    verifier_professeur(request.user, prof)
    enseignements = list(prof.enseignements.select_related('etablissement', 'matiere').order_by('etablissement__nom', 'classe'))
    creneaux = Creneau.objects.filter(enseignement__professeur=prof).select_related('enseignement__matiere', 'enseignement__etablissement')
    return render(request, 'pedagogie/profs/detail.html', {
        'prof': prof,
        'enseignements': enseignements,
        'semaine': semaine(creneaux),
        'heures_semaine': sum(c.duree_heures for c in creneaux),
        'heures': prof.heures.select_related('enseignement__matiere')[:15] if request.user.has_perm('pedagogie.view_heurecours') else [],
        'paiements': prof.paiements.all()[:15] if request.user.has_perm('pedagogie.view_paiementprofesseur') else [],
        'form_heure': HeureCoursForm(professeur=prof) if request.user.has_perm('pedagogie.add_heurecours') else None,
        'form_paiement': PaiementProfesseurForm(initial={'montant': max(prof.solde, 0) or None})
        if request.user.has_perm('pedagogie.add_paiementprofesseur') else None,
        'form_compte': CompteProfesseurForm(initial={'username': prof.nom.lower()})
        if request.user.has_perm('ecole.gerer_utilisateurs') and not prof.utilisateur_id else None,
        'surveillances': prof.surveillances.select_related('composition', 'enseignement__matiere').order_by('date', 'heure_debut'),
    })


@login_required
@permission_required('pedagogie.add_heurecours', raise_exception=True)
@require_POST
def heure_ajouter(request, pk):
    prof = get_object_or_404(Professeur, pk=pk)
    form = HeureCoursForm(request.POST, professeur=prof)
    if form.is_valid():
        h = form.save(commit=False)
        h.professeur, h.saisi_par = prof, request.user
        h.save()
        messages.success(request, _('%(duree)s h enregistrée(s) pour le %(date)s.') % {'duree': format(h.duree, 'g'), 'date': format(h.date, '%d/%m/%Y')})
    else:
        messages.error(request, _('Heures non enregistrées : ') + ' '.join(e for errs in form.errors.values() for e in errs))
    return redirect(f"{reverse('profs:detail', args=[prof.pk])}#heures")


@login_required
@permission_required('pedagogie.delete_heurecours', raise_exception=True)
@require_POST
def heure_supprimer(request, pk):
    h = get_object_or_404(HeureCours, pk=pk)
    h.delete()
    messages.success(request, _('Ligne d’heures supprimée.'))
    return redirect(f"{reverse('profs:detail', args=[h.professeur_id])}#heures")


@login_required
@permission_required('pedagogie.add_paiementprofesseur', raise_exception=True)
@require_POST
def paiement_ajouter(request, pk):
    prof = get_object_or_404(Professeur, pk=pk)
    form = PaiementProfesseurForm(request.POST)
    if form.is_valid():
        p = form.save(commit=False)
        p.professeur, p.verse_par = prof, request.user
        p.save()
        messages.success(request, _('Paiement %(numero)s enregistré.') % {'numero': p.numero})
        return redirect('profs:bon_pdf', pk=p.pk)
    messages.error(request, _('Paiement non enregistré : ') + ' '.join(e for errs in form.errors.values() for e in errs))
    return redirect(f"{reverse('profs:detail', args=[prof.pk])}#paiements")


@login_required
@permission_required('pedagogie.view_paiementprofesseur', raise_exception=True)
def bon_pdf(request, pk):
    paiement = get_object_or_404(PaiementProfesseur.objects.select_related('professeur', 'verse_par'), pk=pk)
    verifier_professeur(request.user, paiement.professeur)
    reponse = HttpResponse(pdf.bon_paiement(paiement), content_type='application/pdf')
    reponse['Content-Disposition'] = f'inline; filename="bon-{paiement.numero}.pdf"'
    return reponse


@login_required
@permission_required('ecole.gerer_utilisateurs', raise_exception=True)
@require_POST
def compte_creer(request, pk):
    prof = get_object_or_404(Professeur, pk=pk)
    if prof.utilisateur_id:
        messages.error(request, _('Ce professeur a déjà un compte.'))
        return redirect('profs:detail', pk=prof.pk)
    form = CompteProfesseurForm(request.POST)
    if form.is_valid():
        with transaction.atomic():
            user = User.objects.create_user(form.cleaned_data['username'], password=form.cleaned_data['mot_de_passe'],
                                            first_name=prof.prenoms, last_name=prof.nom, email=prof.email)
            roles.definir_role(user, roles.PROFESSEUR)
            prof.utilisateur = user
            prof.save(update_fields=['utilisateur'])
        messages.success(request, _('Compte « %(username)s » créé : %(prof)s peut se connecter et saisir ses notes.') % {'username': user.username, 'prof': prof})
    else:
        messages.error(request, _('Compte non créé : ') + ' '.join(e for errs in form.errors.values() for e in errs))
    return redirect('profs:detail', pk=prof.pk)


# --------------------------------------------------------------------------- Emploi du temps

def _classes_config():
    """[(établissement, classe)] ayant au moins une matière configurée."""
    lignes = Enseignement.objects.values_list('etablissement', 'classe').order_by('etablissement__nom', 'classe').distinct()
    etabs = {e.pk: e for e in Etablissement.objects.all()}
    return [(etabs[e], c) for e, c in lignes]


@login_required
@permission_required('pedagogie.view_creneau', raise_exception=True)
def emploi_du_temps(request):
    etab_id, classe = request.GET.get('etab', ''), request.GET.get('classe', '')
    prof_id = request.GET.get('prof', '')
    if 'classe_cle' in request.GET and not prof_id:
        # Formulaire de choix : « id_établissement|classe » -> ?etab=&classe=
        e, _sep, c = request.GET['classe_cle'].partition('|')
        return redirect(f"{reverse('profs:emploi_du_temps')}?etab={e}&classe={c}")
    classes = _classes_config() if voit_tout(request.user) else []
    etab, prof, form = None, None, None

    if prof_id.isdigit():
        prof = get_object_or_404(Professeur, pk=int(prof_id))
        verifier_professeur(request.user, prof)
        creneaux = Creneau.objects.filter(enseignement__professeur=prof)
    elif etab_id.isdigit() and classe:
        if not voit_tout(request.user):
            raise PermissionDenied
        etab = get_object_or_404(Etablissement, pk=int(etab_id))
        creneaux = Creneau.objects.filter(enseignement__etablissement=etab, enseignement__classe=classe)
        if request.user.has_perm('pedagogie.add_creneau'):
            cours = Enseignement.objects.filter(etablissement=etab, classe=classe).select_related('matiere', 'professeur')
            form = CreneauForm(request.POST if request.method == 'POST' else None, enseignements=cours)
            if request.method == 'POST' and form.is_valid():
                c = form.save()
                messages.success(request, _('%(matiere)s ajouté le %(jour)s de %(heure_debut)s à %(heure_fin)s.') % {'matiere': c.enseignement.matiere, 'jour': c.get_jour_display().lower(), 'heure_debut': format(c.heure_debut, '%H:%M'), 'heure_fin': format(c.heure_fin, '%H:%M')})
                return redirect(f"{reverse('profs:emploi_du_temps')}?etab={etab.pk}&classe={classe}")
    else:
        moi = professeur_de(request.user)
        if moi and not voit_tout(request.user):
            return redirect(f"{reverse('profs:emploi_du_temps')}?prof={moi.pk}")
        if classes:
            e, c = classes[0]
            return redirect(f"{reverse('profs:emploi_du_temps')}?etab={e.pk}&classe={c}")
        creneaux = Creneau.objects.none()

    creneaux = list(creneaux.select_related('enseignement__matiere', 'enseignement__professeur', 'enseignement__etablissement'))
    if request.GET.get('format') == 'pdf':
        titre = f'Emploi du temps — {prof}' if prof else f'Emploi du temps — {classe}'
        reponse = HttpResponse(pdf.emploi_du_temps(titre, creneaux, etab or (creneaux[0].enseignement.etablissement if creneaux else None), par_prof=bool(prof)),
                               content_type='application/pdf')
        reponse['Content-Disposition'] = 'inline; filename="emploi-du-temps.pdf"'
        return reponse
    return render(request, 'pedagogie/profs/emploi_du_temps.html', {
        'semaine': semaine(creneaux), 'classes': classes, 'etab': etab, 'classe': classe, 'prof': prof,
        'form': form, 'profs': Professeur.objects.filter(actif=True) if voit_tout(request.user) else [],
        'total_heures': sum(c.duree_heures for c in creneaux),
        'params': request.GET.urlencode(),
    })


@login_required
@permission_required('pedagogie.delete_creneau', raise_exception=True)
@require_POST
def creneau_supprimer(request, pk):
    c = get_object_or_404(Creneau.objects.select_related('enseignement'), pk=pk)
    c.delete()
    messages.success(request, _('Créneau supprimé.'))
    return redirect(f"{reverse('profs:emploi_du_temps')}?etab={c.enseignement.etablissement_id}&classe={c.enseignement.classe}")
