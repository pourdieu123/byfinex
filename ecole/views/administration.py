from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.contrib.auth.models import User
from django.db.models import Count
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.utils.translation import gettext as _

from .. import roles
from ..forms import CodesModulesForm, ParametresForm, PerimetreForm, UtilisateurCreationForm, UtilisateurModifForm
from ..models import Decoupage, Etablissement, Parametres, PerimetreSurveillant


@login_required
@permission_required('ecole.gerer_utilisateurs', raise_exception=True)
def utilisateurs(request):
    comptes = User.objects.order_by('-is_active', 'username').prefetch_related('groups')
    return render(request, 'ecole/utilisateurs.html', {
        'comptes': [(u, roles.libelle_role(roles.role_de(u))) for u in comptes],
        'descriptions': [(roles.LIBELLES[r], d) for r, d in roles.DESCRIPTIONS.items()],
    })


@login_required
@permission_required('ecole.gerer_utilisateurs', raise_exception=True)
def utilisateur_nouveau(request):
    form = UtilisateurCreationForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        user = form.save()
        messages.success(request, _("Compte « %(username)s » créé : définissez maintenant ses codes d'accès aux modules.") % {'username': user.username})
        return redirect(f"{reverse('ecole:utilisateur_modifier', args=[user.pk])}#codes")
    return render(request, 'ecole/utilisateur_form.html', {'form': form, 'titre': _('Nouvel utilisateur')})


@login_required
@permission_required('ecole.gerer_utilisateurs', raise_exception=True)
def utilisateur_modifier(request, pk):
    compte = get_object_or_404(User, pk=pk)
    if compte.is_superuser and not request.user.is_superuser:
        # Un compte Direction ne peut pas prendre la main sur un super-administrateur (mot de passe, rôle, codes).
        raise PermissionDenied
    envoi_codes = request.method == 'POST' and 'codes' in request.POST
    envoi_perimetre = request.method == 'POST' and 'perimetre' in request.POST
    envoi_compte = request.method == 'POST' and not (envoi_codes or envoi_perimetre)
    form = UtilisateurModifForm(request.POST if envoi_compte else None, instance=compte)
    codes = CodesModulesForm(request.POST if envoi_codes else None, prefix='codes', utilisateur=compte)
    perimetre = None
    if roles.role_de(compte) == roles.SURVEILLANT:
        actuel = PerimetreSurveillant.objects.filter(utilisateur=compte).first()
        perimetre = PerimetreForm(request.POST if envoi_perimetre else None, prefix='perimetre',
                                  initial={'classes': actuel.classes if actuel else []})
        if envoi_perimetre and perimetre.is_valid():
            PerimetreSurveillant.objects.update_or_create(utilisateur=compte,
                                                          defaults={'classes': perimetre.cleaned_data['classes']})
            messages.success(request, _('Périmètre de « %(username)s » enregistré.') % {'username': compte.username})
            return redirect(f"{reverse('ecole:utilisateur_modifier', args=[compte.pk])}#perimetre")
    if envoi_codes and codes.is_valid():
        codes.enregistrer()
        messages.success(request, _("Codes d'accès de « %(username)s » enregistrés.") % {'username': compte.username})
        return redirect(f"{reverse('ecole:utilisateur_modifier', args=[compte.pk])}#codes")
    if compte == request.user:
        # On ne peut ni se désactiver ni se retirer le rôle Direction soi-même.
        form.fields['is_active'].disabled = True
        form.fields['role'].disabled = True
    if envoi_compte and form.is_valid():
        form.save()
        messages.success(request, _('Compte « %(username)s » mis à jour.') % {'username': compte.username})
        return redirect('ecole:utilisateurs')
    return render(request, 'ecole/utilisateur_form.html', {
        'form': form, 'codes': codes, 'perimetre': perimetre, 'titre': _('Modifier « %(username)s »') % {'username': compte.username}, 'compte': compte,
    })


@login_required
@permission_required('ecole.gerer_utilisateurs', raise_exception=True)
def parametres(request):
    form = ParametresForm(request.POST if request.method == 'POST' else None, instance=Parametres.courant())
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, _('Paramètres enregistrés.'))
        return redirect('ecole:parametres')
    return render(request, 'ecole/parametres.html', {
        'form': form,
        'etablissements': Etablissement.objects.annotate(nb=Count('eleves')),
        'decoupages': Decoupage.choices,
    })


@login_required
@permission_required('ecole.gerer_utilisateurs', raise_exception=True)
@require_POST
def etablissement_renommer(request, pk):
    etab = get_object_or_404(Etablissement, pk=pk)
    nom = ' '.join(request.POST.get('nom', '').split())
    if not nom:
        messages.error(request, _('Le nom ne peut pas être vide.'))
    elif Etablissement.objects.filter(nom__iexact=nom).exclude(pk=pk).exists():
        messages.error(request, _('Un établissement « %(nom)s » existe déjà.') % {'nom': nom})
    else:
        ancien, etab.nom = etab.nom, nom
        if request.POST.get('decoupage') in Decoupage.values:
            etab.decoupage = request.POST['decoupage']
        etab.save(update_fields=['nom', 'decoupage'])
        messages.success(request, f'« {nom} » enregistré ({etab.get_decoupage_display().lower()}).'
                         if ancien == nom else f'« {ancien} » renommé en « {nom} ».')
    return redirect('ecole:parametres')
