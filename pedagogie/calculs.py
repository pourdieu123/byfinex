"""Calcul des résultats d'une classe : moyennes par matière, moyenne générale, rangs, appréciations."""

from dataclasses import dataclass, field
from decimal import Decimal

from django.utils.translation import get_language, gettext_lazy as _

from ecole.models import Eleve

from .models import Enseignement, FicheNote, arrondi

APPRECIATIONS = [
    (Decimal(18), _('Excellent')), (Decimal(16), _('Très bien')), (Decimal(14), _('Bien')), (Decimal(12), _('Assez bien')),
    (Decimal(10), _('Passable')), (Decimal(8), _('Insuffisant')), (Decimal(5), _('Faible')), (Decimal(0), _('Très faible')),
]


def appreciation(moyenne):
    if moyenne is None:
        return ''
    return next(libelle for seuil, libelle in APPRECIATIONS if moyenne >= seuil)


def rangs(valeurs):
    """{clé: valeur} -> {clé: (rang, ex_aequo)} ; les valeurs None n'ont pas de rang."""
    notees = sorted(((v, k) for k, v in valeurs.items() if v is not None), key=lambda x: -x[0])
    resultat, compte = {}, {}
    for v, _cle in notees:
        compte[v] = compte.get(v, 0) + 1
    rang_courant, precedent = 0, None
    for position, (v, k) in enumerate(notees, start=1):
        if v != precedent:
            rang_courant, precedent = position, v
        resultat[k] = (rang_courant, compte[v] > 1)
    return resultat


def libelle_rang(rang):
    if not rang:
        return '—'
    n, ex = rang
    if (get_language() or '').startswith('en'):
        suffixe = 'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
        return f"{n}{suffixe}{' (tie)' if ex else ''}"
    return f"{'1er' if n == 1 else f'{n}e'}{' ex' if ex else ''}"


@dataclass
class LigneMatiere:
    enseignement: Enseignement
    fiche: FicheNote | None
    rang: tuple | None = None

    @property
    def moyenne(self):
        return self.fiche.moyenne if self.fiche else None

    @property
    def points(self):
        m = self.moyenne
        return None if m is None else m * self.enseignement.coefficient


@dataclass
class ResultatEleve:
    eleve: Eleve
    matieres: list = field(default_factory=list)
    rang: tuple | None = None
    moyenne_annuelle: Decimal | None = None
    rang_annuel: tuple | None = None

    @property
    def total_coef(self):
        return sum(l.enseignement.coefficient for l in self.matieres if l.moyenne is not None)

    @property
    def total_points(self):
        return sum((l.points for l in self.matieres if l.points is not None), Decimal(0))

    @property
    def moyenne(self):
        return arrondi(self.total_points / self.total_coef) if self.total_coef else None

    @property
    def appreciation(self):
        return appreciation(self.moyenne)


@dataclass
class ResultatsClasse:
    etablissement: object
    classe: str
    periode: int
    enseignements: list
    eleves: list  # [ResultatEleve]

    @property
    def moyennes(self):
        return [r.moyenne for r in self.eleves if r.moyenne is not None]

    @property
    def plus_forte(self):
        return max(self.moyennes, default=None)

    @property
    def plus_faible(self):
        return min(self.moyennes, default=None)

    @property
    def moyenne_classe(self):
        m = self.moyennes
        return arrondi(sum(m) / len(m)) if m else None

    @property
    def nb_moyenne(self):
        return sum(1 for m in self.moyennes if m >= 10)

    @property
    def taux_reussite(self):
        return round(self.nb_moyenne * 100 / len(self.moyennes)) if self.moyennes else None

    @property
    def est_derniere_periode(self):
        return self.periode == self.etablissement.nb_periodes

    def pour(self, eleve_id):
        return next((r for r in self.eleves if r.eleve.pk == eleve_id), None)


def resultats_classe(etablissement, classe, periode, avec_annuel=True):
    enseignements = list(
        Enseignement.objects.filter(etablissement=etablissement, classe=classe)
        .select_related('matiere', 'professeur').order_by('matiere__nom')
    )
    eleves = list(Eleve.objects.filter(etablissement=etablissement, classe=classe).order_by('nom', 'prenom'))
    fiches = {
        (f.eleve_id, f.enseignement_id): f
        for f in FicheNote.objects.filter(enseignement__in=enseignements, periode=periode, eleve__in=eleves)
    }
    resultats = [
        ResultatEleve(e, [LigneMatiere(ens, fiches.get((e.pk, ens.pk))) for ens in enseignements])
        for e in eleves
    ]
    # Rang par matière
    for i, ens in enumerate(enseignements):
        r = rangs({res.eleve.pk: res.matieres[i].moyenne for res in resultats})
        for res in resultats:
            res.matieres[i].rang = r.get(res.eleve.pk)
    # Rang général
    r = rangs({res.eleve.pk: res.moyenne for res in resultats})
    for res in resultats:
        res.rang = r.get(res.eleve.pk)
    # Moyenne annuelle (dernière période) = moyenne des moyennes générales de chaque période
    if avec_annuel and periode == etablissement.nb_periodes and periode > 1:
        precedentes = [resultats_classe(etablissement, classe, p, avec_annuel=False) for p in range(1, periode)]
        for res in resultats:
            valeurs = [res.moyenne] + [pr.pour(res.eleve.pk).moyenne for pr in precedentes]
            valeurs = [v for v in valeurs if v is not None]
            res.moyenne_annuelle = arrondi(sum(valeurs) / len(valeurs)) if valeurs else None
        r = rangs({res.eleve.pk: res.moyenne_annuelle for res in resultats})
        for res in resultats:
            res.rang_annuel = r.get(res.eleve.pk)
    return ResultatsClasse(etablissement, classe, periode, enseignements, resultats)
