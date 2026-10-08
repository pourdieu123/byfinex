from math import ceil

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Sum
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _

from pedagogie.acces import enseignements_visibles, professeur_de
from pedagogie.models import Composition, Creneau, FicheNote, Professeur

from .. import verrou
from ..modules import accessible, bientot, info_module, modules_de
from ..models import Eleve, Paiement


def _chiffres(user, code):
    """Deux ou trois chiffres clés affichés sur la tuile de chaque module."""
    if code == 'scolarite':
        eleves = list(Eleve.objects.actifs().avec_totaux())
        du = sum(e.montant_scolarite for e in eleves)
        verse = sum(e.total_verse for e in eleves)
        return [(str(len(eleves)), _('élèves inscrits')),
                (f'{round(verse * 100 / du) if du else 0} %', _('de la scolarité recouvrée')),
                (str(sum(1 for e in eleves if e.reste_a_payer)), _('élèves à relancer'))]
    if code == 'notes':
        cours = enseignements_visibles(user)
        fiches = FicheNote.objects.filter(enseignement__in=cours)
        return [(str(cours.count()), _('cours (classe × matière)')),
                (str(fiches.exclude(d1=None).count()), _('notes de devoir saisies')),
                (str(cours.values('etablissement', 'classe').distinct().count()), _('classes'))]
    if code == 'profs':
        moi = professeur_de(user)
        if moi and not user.has_perm('pedagogie.voir_tout'):
            return [(str(moi.enseignements.count()), _('cours attribués')),
                    (f'{sum(c.duree_heures for c in Creneau.objects.filter(enseignement__professeur=moi)):g} h', _('par semaine')),
                    (f'{moi.total_heures:g} h', _('heures pointées'))]
        profs = list(Professeur.objects.filter(actif=True))
        return [(str(len(profs)), _('professeurs actifs')),
                (f'{sum(max(p.solde, 0) for p in profs):,}'.replace(',', ' ') + ' F', _('restant à verser')),
                (str(Creneau.objects.count()), _('créneaux au planning'))]
    if code == 'controle':
        eleves = Eleve.objects.actifs().avec_totaux().prefetch_related('echeances')
        perimetre = getattr(user, 'perimetre', None)
        if perimetre is not None:
            eleves = perimetre.filtrer(eleves)
        statuts = [e.situation.statut for e in eleves]
        return [(str(statuts.count('a_jour') + statuts.count('solde')), _('à jour ou soldés')),
                (str(statuts.count('retard')), _('en retard'))]
    if code == 'examens':
        prochaine = Composition.objects.filter(date_fin__gte=timezone.localdate()).order_by('date_debut').first()
        nb = Composition.objects.count()
        return [(str(nb), _('compositions') if nb > 1 else _('composition')),
                (f'{prochaine.date_debut:%d/%m}' if prochaine else '—', prochaine.titre if prochaine else _('aucune à venir'))]
    return []


@login_required
def portail(request):
    if getattr(request.user, 'compte_parent', None) is not None:
        return redirect('parents:accueil')
    modules = [(m, [] if bientot(m.code) else _chiffres(request.user, m.code), bientot(m.code))
               for m in modules_de(request.user)]
    return render(request, 'ecole/portail.html', {
        'modules': modules,
        'encaisse_jour': Paiement.objects.filter(date=timezone.localdate()).aggregate(t=Sum('montant'))['t'] or 0
        if request.user.has_perm('ecole.view_paiement') else None,
    })


@login_required
def deverrouiller(request, module):
    """Saisie du code personnel d'un module, puis retour à la page demandée."""
    info = info_module(module)
    if info is None or not accessible(request.user, module):
        raise PermissionDenied
    if bientot(module):
        return render(request, 'ecole/bientot.html', {'m': info})
    suivant = request.POST.get('next') or request.GET.get('next') or ''
    if not url_has_allowed_host_and_scheme(suivant, allowed_hosts={request.get_host()},
                                           require_https=request.is_secure()):
        suivant = reverse(info.url)
    if verrou.est_ouvert(request, module):
        verrou.ouvrir(request, module)
        return redirect(suivant)
    contexte = {'m': info, 'suivant': suivant}
    if not verrou.est_protege(request.user, module):
        if not verrou.peut_creer_son_code(request.user):
            return render(request, 'ecole/deverrouiller.html', {**contexte, 'sans_code': True})
        erreur = None
        if request.method == 'POST':
            code, confirmation = request.POST.get('code', ''), request.POST.get('confirmation', '')
            if len(code) < 4:
                erreur = _('Le code doit contenir au moins 4 caractères.')
            elif code != confirmation:
                erreur = _('Les deux codes ne sont pas identiques.')
            else:
                verrou.definir_code(request.user, module, code)
                verrou.ouvrir(request, module)
                messages.success(request, _('Votre code pour ce module est enregistré.'))
                return redirect(suivant)
        return render(request, 'ecole/deverrouiller.html', {**contexte, 'creation': True, 'erreur': erreur})
    erreur = None
    attente = verrou.attente_restante(request)
    if request.method == 'POST' and not attente:
        if verrou.verifier(request, request.user, module, request.POST.get('code', '')):
            return redirect(suivant)
        attente = verrou.attente_restante(request)
        erreur = None if attente else _('Code incorrect.')
    return render(request, 'ecole/deverrouiller.html', {
        **contexte, 'erreur': erreur, 'attente_minutes': ceil(attente / 60),
    })
