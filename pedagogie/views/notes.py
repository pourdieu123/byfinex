from decimal import Decimal, InvalidOperation
from itertools import groupby

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _

from ecole.models import Eleve, Etablissement

from .. import pdf
from ..acces import enseignements_visibles, verifier_enseignement, voit_tout
from ..calculs import libelle_rang, resultats_classe
from ..forms import EnseignementForm, MatiereForm
from ..models import Enseignement, FicheNote, Matiere

COLONNES = FicheNote.INTERROS + ['d1', 'd2']


def _classe_demandee(request):
    """(établissement, classe) depuis ?etab=&classe=, ou 404."""
    etab_id, classe = request.GET.get('etab', ''), request.GET.get('classe', '').strip()
    if not etab_id.isdigit() or not classe:
        raise Http404('Classe non précisée')
    return get_object_or_404(Etablissement, pk=int(etab_id)), classe


def _periode(request, etab):
    try:
        p = int(request.GET.get('periode', 1))
    except ValueError:
        p = 1
    return p if 1 <= p <= etab.nb_periodes else 1


def _classes_avec_eleves():
    """[(établissement, classe, effectif)] pour toutes les classes ayant des élèves."""
    lignes = (Eleve.objects.values('etablissement', 'classe').order_by('etablissement__nom', 'classe')
              .distinct())
    etabs = {e.pk: e for e in Etablissement.objects.all()}
    effectifs = {}
    for e in Eleve.objects.values_list('etablissement', 'classe'):
        effectifs[e] = effectifs.get(e, 0) + 1
    return [(etabs[l['etablissement']], l['classe'], effectifs[(l['etablissement'], l['classe'])]) for l in lignes]


@login_required
@permission_required('pedagogie.view_fichenote', raise_exception=True)
def tableau(request):
    mes_cours = list(enseignements_visibles(request.user).order_by('etablissement__nom', 'classe', 'matiere__nom'))
    classes = []
    if voit_tout(request.user):
        par_classe = {}
        for ens in mes_cours:
            par_classe.setdefault((ens.etablissement_id, ens.classe), []).append(ens)
        for etab, classe, effectif in _classes_avec_eleves():
            ens = par_classe.get((etab.pk, classe), [])
            saisies = FicheNote.objects.filter(enseignement__in=ens).exclude(
                i1=None, i2=None, i3=None, i4=None, d1=None, d2=None).count()
            attendu = effectif * len(ens) * etab.nb_periodes
            classes.append({
                'etablissement': etab, 'classe': classe, 'effectif': effectif, 'nb_matieres': len(ens),
                'sans_prof': sum(1 for e in ens if not e.professeur_id),
                'avancement': round(saisies * 100 / attendu) if attendu else 0,
            })
    groupes = [(etab, list(items)) for etab, items in groupby(mes_cours, key=lambda e: e.etablissement)]
    return render(request, 'pedagogie/notes/tableau.html', {
        'classes': classes, 'mes_groupes': groupes, 'nb_cours': len(mes_cours),
        'nb_matieres': Matiere.objects.count(),
    })


@login_required
@permission_required('pedagogie.view_matiere', raise_exception=True)
def matieres(request):
    form = MatiereForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST':
        if not request.user.has_perm('pedagogie.add_matiere'):
            raise Http404
        if form.is_valid():
            m = form.save()
            messages.success(request, _('Matière « %(m)s » ajoutée.') % {'m': m})
            return redirect('notes:matieres')
    return render(request, 'pedagogie/notes/matieres.html', {
        'form': form, 'matieres': Matiere.objects.all().prefetch_related('enseignements'),
    })


@login_required
@permission_required('pedagogie.change_enseignement', raise_exception=True)
def classe_config(request):
    etab, classe = _classe_demandee(request)
    enseignements = Enseignement.objects.filter(etablissement=etab, classe=classe).select_related('matiere', 'professeur')
    edition = None
    if request.GET.get('modifier', '').isdigit():
        edition = get_object_or_404(enseignements, pk=int(request.GET['modifier']))
    form = EnseignementForm(request.POST if request.method == 'POST' else None, instance=edition,
                            etablissement=etab, classe=classe)
    if request.method == 'POST' and form.is_valid():
        ens = form.save(commit=False)
        ens.etablissement, ens.classe = etab, classe
        ens.save()
        messages.success(request, _('%(matiere)s (coef. %(coefficient)s) enregistrée pour la %(classe)s.') % {'matiere': ens.matiere, 'coefficient': ens.coefficient, 'classe': classe})
        return redirect(f"{reverse('notes:classe_config')}?etab={etab.pk}&classe={classe}")
    return render(request, 'pedagogie/notes/classe_config.html', {
        'etab': etab, 'classe': classe, 'enseignements': enseignements, 'form': form, 'edition': edition,
        'effectif': Eleve.objects.filter(etablissement=etab, classe=classe).count(),
        'total_coef': sum(e.coefficient for e in enseignements),
        'autres_classes': [c for c in _classes_avec_eleves() if not (c[0] == etab and c[1] == classe)],
    })


@login_required
@permission_required('pedagogie.delete_enseignement', raise_exception=True)
def enseignement_supprimer(request, pk):
    ens = get_object_or_404(Enseignement, pk=pk)
    retour = f"{reverse('notes:classe_config')}?etab={ens.etablissement_id}&classe={ens.classe}"
    if request.method == 'POST':
        messages.success(request, _('%(matiere)s retirée de la %(classe)s.') % {'matiere': ens.matiere, 'classe': ens.classe})
        ens.delete()
        return redirect(retour)
    return render(request, 'ecole/confirmer_suppression.html', {
        'titre': _('Retirer une matière'),
        'message': _('Retirer %(matiere)s de la %(classe)s ? Les %(fiches)s fiche(s) de notes de cette matière seront supprimées.') % {'matiere': ens.matiere, 'classe': ens.classe, 'fiches': ens.fiches.count()},
        'bouton': _('Oui, retirer'), 'annuler_url': retour,
    })


@login_required
@permission_required('pedagogie.change_enseignement', raise_exception=True)
@transaction.atomic
def copier_config(request):
    """Copie les matières et coefficients d'une autre classe (sans les professeurs)."""
    etab, classe = _classe_demandee(request)
    source = Etablissement.objects.filter(pk=request.POST.get('source_etab') or 0).first()
    source_classe = request.POST.get('source_classe', '')
    if request.method == 'POST' and source:
        n = 0
        for ens in Enseignement.objects.filter(etablissement=source, classe=source_classe):
            _ens, cree = Enseignement.objects.get_or_create(etablissement=etab, classe=classe, matiere=ens.matiere,
                                                         defaults={'coefficient': ens.coefficient})
            n += cree
        messages.success(request, _('%(n)s matière(s) copiée(s) depuis la %(source_classe)s.') % {'n': n, 'source_classe': source_classe})
    return redirect(f"{reverse('notes:classe_config')}?etab={etab.pk}&classe={classe}")


def _lire_note(brut):
    brut = (brut or '').strip().replace(',', '.')
    if brut == '':
        return None, None
    try:
        v = Decimal(brut)
    except InvalidOperation:
        return None, 'nombre attendu'
    if not (0 <= v <= 20):
        return None, 'entre 0 et 20'
    return v.quantize(Decimal('0.01')), None


@login_required
@permission_required('pedagogie.view_fichenote', raise_exception=True)
def saisie(request, pk):
    ens = get_object_or_404(Enseignement.objects.select_related('etablissement', 'matiere', 'professeur'), pk=pk)
    verifier_enseignement(request.user, ens)
    periode = _periode(request, ens.etablissement)
    peut_modifier = request.user.has_perm('pedagogie.change_fichenote')
    eleves = list(ens.eleves())
    fiches = {f.eleve_id: f for f in FicheNote.objects.filter(enseignement=ens, periode=periode)}
    erreurs, valeurs = {}, {}

    if request.method == 'POST':
        verifier_enseignement(request.user, ens, saisie=True)
        a_enregistrer = []
        for e in eleves:
            fiche = fiches.get(e.pk) or FicheNote(eleve=e, enseignement=ens, periode=periode)
            for col in COLONNES:
                brut = request.POST.get(f'{e.pk}-{col}', '')
                valeurs[(e.pk, col)] = brut
                v, err = _lire_note(brut)
                if err:
                    erreurs[(e.pk, col)] = err
                else:
                    setattr(fiche, col, v)
            a_enregistrer.append(fiche)
        if not erreurs:
            with transaction.atomic():
                for fiche in a_enregistrer:
                    if fiche.pk or not fiche.vide:
                        fiche.modifie_par = request.user
                        fiche.save()
            messages.success(request, _('Notes de %(matiere)s (%(classe)s) enregistrées.') % {'matiere': ens.matiere, 'classe': ens.classe})
            return redirect(f"{reverse('notes:saisie', args=[ens.pk])}?periode={periode}")
        messages.error(request, _('Certaines notes sont invalides : corrigez les cases en rouge.'))

    lignes = []
    for e in eleves:
        fiche = fiches.get(e.pk)
        cellules = []
        for col in COLONNES:
            if (e.pk, col) in valeurs:
                val = valeurs[(e.pk, col)]
            else:
                v = getattr(fiche, col) if fiche else None
                val = '' if v is None else f'{v:g}'.replace('.', ',')
            cellules.append({'nom': f'{e.pk}-{col}', 'valeur': val, 'erreur': erreurs.get((e.pk, col))})
        lignes.append({'eleve': e, 'cellules': cellules, 'fiche': fiche})
    return render(request, 'pedagogie/notes/saisie.html', {
        'ens': ens, 'periode': periode, 'periodes': ens.etablissement.periodes, 'lignes': lignes,
        'peut_modifier': peut_modifier, 'colonne_focus': request.GET.get('colonne', ''),
        'entetes': [('i1', 'I1'), ('i2', 'I2'), ('i3', 'I3'), ('i4', 'I4'), ('d1', 'D1'), ('d2', 'D2')],
    })


@login_required
@permission_required('pedagogie.view_fichenote', raise_exception=True)
def resultats(request):
    etab, classe = _classe_demandee(request)
    if not voit_tout(request.user):
        # Un professeur voit les résultats des classes où il enseigne.
        if not enseignements_visibles(request.user).filter(etablissement=etab, classe=classe).exists():
            raise Http404
    periode = _periode(request, etab)
    res = resultats_classe(etab, classe, periode)
    return render(request, 'pedagogie/notes/resultats.html', {
        'res': res, 'etab': etab, 'classe': classe, 'periode': periode, 'periodes': etab.periodes,
        'libelle_periode': dict(etab.periodes)[periode], 'libelle_rang': libelle_rang,
        'lignes': sorted(res.eleves, key=lambda r: (r.rang is None, r.rang or (0, False), r.eleve.nom)),
    })


@login_required
@permission_required('pedagogie.view_fichenote', raise_exception=True)
def bulletins_pdf(request):
    etab, classe = _classe_demandee(request)
    if not voit_tout(request.user):
        raise Http404
    periode = _periode(request, etab)
    res = resultats_classe(etab, classe, periode)
    eleve_id = request.GET.get('eleve', '')
    eleves = [res.pour(int(eleve_id))] if eleve_id.isdigit() and res.pour(int(eleve_id)) else res.eleves
    contenu = pdf.bulletins(res, eleves)
    nom = f'bulletin-{eleves[0].eleve.nom}' if len(eleves) == 1 else f'bulletins-{classe}'
    reponse = HttpResponse(contenu, content_type='application/pdf')
    reponse['Content-Disposition'] = f'inline; filename="{nom}-p{periode}.pdf"'
    return reponse
