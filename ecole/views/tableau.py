from datetime import date
from itertools import groupby

from django.contrib.auth.decorators import login_required, permission_required
from django.db.models import Count, Sum
from django.db.models.functions import TruncMonth
from django.shortcuts import render
from django.utils import dateformat, timezone
from django.utils.translation import gettext as _

from ..models import Eleve, Paiement

def _six_derniers_mois(aujourdhui):
    mois = []
    annee, m = aujourdhui.year, aujourdhui.month
    for _i in range(6):
        mois.append(date(annee, m, 1))
        m -= 1
        if m == 0:
            annee, m = annee - 1, 12
    return list(reversed(mois))


def encaissements_mensuels(aujourdhui):
    mois = _six_derniers_mois(aujourdhui)
    totaux = {
        row['mois']: row['total']
        for row in Paiement.objects.filter(date__gte=mois[0])
        .annotate(mois=TruncMonth('date'))
        .values('mois')
        .annotate(total=Sum('montant'))
    }
    valeurs = [totaux.get(m, 0) for m in mois]
    maxi = max(valeurs) or 1
    return [
        {
            'libelle': dateformat.format(m, 'M y'),
            'total': v,
            'hauteur': round(v * 100 / maxi),
            'courant': i == len(mois) - 1,
        }
        for i, (m, v) in enumerate(zip(mois, valeurs))
    ]


def recouvrement_par_classe(eleves):
    lignes = []
    plusieurs = len({e.etablissement_id for e in eleves}) > 1
    cle = lambda e: (e.etablissement.nom, e.classe)  # noqa: E731
    for (etab, classe), items in groupby(sorted(eleves, key=lambda e: (e.etablissement.nom.lower(), e.classe.lower())), key=cle):
        items = list(items)
        du = sum(e.montant_scolarite for e in items)
        verse = sum(e.total_verse for e in items)
        lignes.append({
            'classe': classe,
            'etablissement': etab if plusieurs else '',
            'effectif': len(items),
            'du': du,
            'verse': verse,
            'reste': du - verse,
            'taux': round(verse * 100 / du) if du else 100,
            'soldes': sum(1 for e in items if e.statut == 'solde'),
        })
    return lignes


@login_required
@permission_required('ecole.view_eleve', raise_exception=True)
def accueil(request):
    aujourdhui = timezone.localdate()
    eleves = list(Eleve.objects.actifs().avec_totaux().select_related('etablissement').prefetch_related('echeances'))
    retards = sorted((e for e in eleves if e.situation.en_retard), key=lambda e: -e.situation.montant_en_retard)
    du = sum(e.montant_scolarite for e in eleves)
    verse = sum(e.total_verse for e in eleves)
    jour = Paiement.objects.filter(date=aujourdhui).aggregate(total=Sum('montant'), nb=Count('id'))

    return render(request, 'ecole/accueil.html', {
        'nb_eleves': len(eleves),
        'nb_classes': len({e.classe for e in eleves}),
        'nb_soldes': sum(1 for e in eleves if e.statut == 'solde'),
        'nb_partiels': sum(1 for e in eleves if e.statut == 'partiel'),
        'nb_aucun': sum(1 for e in eleves if e.statut == 'aucun'),
        'encaisse_total': Paiement.objects.aggregate(t=Sum('montant'))['t'] or 0,
        'reste': du - verse,
        'taux': round(verse * 100 / du) if du else 0,
        'jour_total': jour['total'] or 0,
        'jour_nb': jour['nb'],
        'mensuel': encaissements_mensuels(aujourdhui),
        'par_classe': recouvrement_par_classe(eleves),
        'derniers_paiements': Paiement.objects.select_related('eleve__etablissement')[:6],
        'a_relancer': retards[:6] or sorted((e for e in eleves if e.reste_a_payer), key=lambda e: -e.reste_a_payer)[:5],
        'nb_retard': len(retards),
        'insc_du': sum(e.inscription_due for e in eleves),
        'insc_verse': sum(min(e.total_inscription, e.inscription_due) for e in eleves if e.frais_inscription),
        'insc': {s: sum(1 for e in eleves if e.statut_inscription == s) for s in ('payee', 'partielle', 'impayee', 'non_definie')},
        'montant_retard': sum(e.situation.montant_en_retard for e in retards),
        'temps_reel': True,
    })
