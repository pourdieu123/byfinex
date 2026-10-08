"""Notes et moyennes, professeurs (affectations, emploi du temps, heures, paiements) et compositions."""

from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.db.models import Q, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from ecole.models import Eleve, Etablissement, ModePaiement

NOTE_VALIDATORS = [MinValueValidator(0), MaxValueValidator(20)]


def arrondi(valeur, decimales=2):
    if valeur is None:
        return None
    return Decimal(valeur).quantize(Decimal(1).scaleb(-decimales), rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------- Matières et affectations

class Matiere(models.Model):
    nom = models.CharField(_('nom'), max_length=80, unique=True)
    abreviation = models.CharField(_('abréviation'), max_length=12, blank=True)

    class Meta:
        verbose_name = _('matière')
        ordering = ['nom']

    def __str__(self):
        return self.nom


class Statut(models.TextChoices):
    PERMANENT = 'permanent', _('Permanent')
    VACATAIRE = 'vacataire', _('Vacataire')
class Professeur(models.Model):
    nom = models.CharField(_('nom'), max_length=100)
    prenoms = models.CharField(_('prénom(s)'), max_length=150)
    telephone = models.CharField(_('téléphone'), max_length=30)
    email = models.EmailField(_('e-mail'), blank=True)
    statut = models.CharField(_('statut'), max_length=10, choices=Statut.choices, default=Statut.VACATAIRE)
    taux_horaire = models.PositiveIntegerField(_('taux horaire (FCFA / heure)'), default=0)
    matieres = models.ManyToManyField(Matiere, blank=True, related_name='professeurs', verbose_name=_('matières enseignées'))
    utilisateur = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='professeur', verbose_name=_('compte de connexion'),
    )
    actif = models.BooleanField(_('en activité'), default=True)
    cree_le = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _('professeur')
        ordering = ['nom', 'prenoms']
        permissions = [('voir_tout', 'Voit toutes les classes et tous les professeurs')]

    def __str__(self):
        return f'{self.nom.upper()} {self.prenoms}'

    @property
    def initiales(self):
        return (self.nom[:1] + self.prenoms[:1]).upper()

    @property
    def total_heures(self):
        return self.heures.aggregate(t=Sum('duree'))['t'] or Decimal(0)

    @property
    def montant_du(self):
        return int(self.total_heures * self.taux_horaire)

    @property
    def total_paye(self):
        return self.paiements.aggregate(t=Sum('montant'))['t'] or 0

    @property
    def solde(self):
        """Ce qui reste à verser au professeur (heures × taux − paiements)."""
        return self.montant_du - self.total_paye


class Enseignement(models.Model):
    """Une matière enseignée dans une classe d'un établissement, avec son coefficient et son professeur."""

    etablissement = models.ForeignKey(Etablissement, on_delete=models.CASCADE, related_name='enseignements')
    classe = models.CharField(_('classe'), max_length=50)
    matiere = models.ForeignKey(Matiere, on_delete=models.PROTECT, related_name='enseignements', verbose_name=_('matière'))
    coefficient = models.PositiveSmallIntegerField(_('coefficient'), default=1, validators=[MinValueValidator(1), MaxValueValidator(10)])
    professeur = models.ForeignKey(
        Professeur, on_delete=models.SET_NULL, null=True, blank=True, related_name='enseignements', verbose_name=_('professeur'),
    )

    class Meta:
        verbose_name = _('enseignement')
        ordering = ['etablissement__nom', 'classe', 'matiere__nom']
        constraints = [models.UniqueConstraint(fields=['etablissement', 'classe', 'matiere'], name='enseignement_unique')]

    def __str__(self):
        return f'{self.classe} — {self.matiere}'

    def eleves(self):
        return Eleve.objects.filter(etablissement=self.etablissement, classe=self.classe).order_by('nom', 'prenom')


# --------------------------------------------------------------------------- Notes (système béninois)

class Colonne(models.TextChoices):
    D1 = 'd1', _('1er devoir')
    D2 = 'd2', _('2e devoir')
class FicheNote(models.Model):
    """Notes d'un élève dans une matière pour une période : interrogations, 1er et 2e devoir.

    Moyenne des interrogations (MI) = moyenne des interrogations notées.
    Moyenne de la matière = (MI + D1 + D2) / 3, calculée sur les notes disponibles.
    """

    INTERROS = ['i1', 'i2', 'i3', 'i4']

    eleve = models.ForeignKey(Eleve, on_delete=models.CASCADE, related_name='fiches_notes')
    enseignement = models.ForeignKey(Enseignement, on_delete=models.CASCADE, related_name='fiches')
    periode = models.PositiveSmallIntegerField(_('période'), validators=[MinValueValidator(1), MaxValueValidator(3)])
    i1 = models.DecimalField(_('interro 1'), max_digits=4, decimal_places=2, null=True, blank=True, validators=NOTE_VALIDATORS)
    i2 = models.DecimalField(_('interro 2'), max_digits=4, decimal_places=2, null=True, blank=True, validators=NOTE_VALIDATORS)
    i3 = models.DecimalField(_('interro 3'), max_digits=4, decimal_places=2, null=True, blank=True, validators=NOTE_VALIDATORS)
    i4 = models.DecimalField(_('interro 4'), max_digits=4, decimal_places=2, null=True, blank=True, validators=NOTE_VALIDATORS)
    d1 = models.DecimalField(_('devoir 1'), max_digits=4, decimal_places=2, null=True, blank=True, validators=NOTE_VALIDATORS)
    d2 = models.DecimalField(_('devoir 2'), max_digits=4, decimal_places=2, null=True, blank=True, validators=NOTE_VALIDATORS)
    modifie_le = models.DateTimeField(auto_now=True)
    modifie_par = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        verbose_name = _('fiche de notes')
        verbose_name_plural = _('fiches de notes')
        constraints = [models.UniqueConstraint(fields=['eleve', 'enseignement', 'periode'], name='fiche_unique')]

    @property
    def interros(self):
        return [getattr(self, c) for c in self.INTERROS if getattr(self, c) is not None]

    @property
    def mi(self):
        notes = self.interros
        return arrondi(sum(notes) / len(notes)) if notes else None

    @property
    def moyenne(self):
        parts = [v for v in (self.mi, self.d1, self.d2) if v is not None]
        return arrondi(sum(parts) / len(parts)) if parts else None

    @property
    def vide(self):
        return all(getattr(self, c) is None for c in self.INTERROS + ['d1', 'd2'])


# --------------------------------------------------------------------------- Emploi du temps

class Jour(models.IntegerChoices):
    LUNDI = 1, _('Lundi')
    MARDI = 2, _('Mardi')
    MERCREDI = 3, _('Mercredi')
    JEUDI = 4, _('Jeudi')
    VENDREDI = 5, _('Vendredi')
    SAMEDI = 6, _('Samedi')
def _chevauche(qs, debut, fin):
    return qs.filter(heure_debut__lt=fin, heure_fin__gt=debut)


class Creneau(models.Model):
    enseignement = models.ForeignKey(Enseignement, on_delete=models.CASCADE, related_name='creneaux', verbose_name=_('cours'))
    jour = models.PositiveSmallIntegerField(_('jour'), choices=Jour.choices)
    heure_debut = models.TimeField(_('début'))
    heure_fin = models.TimeField(_('fin'))
    salle = models.CharField(_('salle'), max_length=40, blank=True)

    class Meta:
        verbose_name = _('créneau')
        ordering = ['jour', 'heure_debut']

    def clean(self):
        if self.heure_debut and self.heure_fin and self.heure_fin <= self.heure_debut:
            raise ValidationError(_("L'heure de fin doit être après l'heure de début."))
        if not (self.heure_debut and self.heure_fin and self.jour and self.enseignement_id):
            return
        autres = _chevauche(Creneau.objects.filter(jour=self.jour).exclude(pk=self.pk), self.heure_debut, self.heure_fin)
        ens = self.enseignement
        conflit = autres.filter(enseignement__etablissement=ens.etablissement, enseignement__classe=ens.classe).first()
        if conflit:
            raise ValidationError(_('La classe %(classe)s a déjà %(matiere)s de %(heure_debut)s à %(heure_fin)s.') % {'classe': ens.classe, 'matiere': conflit.enseignement.matiere, 'heure_debut': format(conflit.heure_debut, '%H:%M'), 'heure_fin': format(conflit.heure_fin, '%H:%M')})
        if ens.professeur_id:
            conflit = autres.filter(enseignement__professeur_id=ens.professeur_id).first()
            if conflit:
                raise ValidationError(_('%(professeur)s a déjà cours en %(classe)s de %(heure_debut)s à %(heure_fin)s.') % {'professeur': ens.professeur, 'classe': conflit.enseignement.classe, 'heure_debut': format(conflit.heure_debut, '%H:%M'), 'heure_fin': format(conflit.heure_fin, '%H:%M')})
        if self.salle:
            conflit = autres.filter(salle__iexact=self.salle, enseignement__etablissement=ens.etablissement).first()
            if conflit:
                raise ValidationError(_('La salle %(salle)s est déjà occupée par la %(classe)s de %(heure_debut)s à %(heure_fin)s.') % {'salle': self.salle, 'classe': conflit.enseignement.classe, 'heure_debut': format(conflit.heure_debut, '%H:%M'), 'heure_fin': format(conflit.heure_fin, '%H:%M')})

    @property
    def duree_heures(self):
        d = (self.heure_fin.hour * 60 + self.heure_fin.minute) - (self.heure_debut.hour * 60 + self.heure_debut.minute)
        return Decimal(d) / 60


# --------------------------------------------------------------------------- Heures et paiements des professeurs

class HeureCours(models.Model):
    professeur = models.ForeignKey(Professeur, on_delete=models.CASCADE, related_name='heures')
    enseignement = models.ForeignKey(Enseignement, on_delete=models.SET_NULL, null=True, blank=True, verbose_name=_('cours'))
    date = models.DateField(_('date'), default=timezone.localdate)
    duree = models.DecimalField(_('durée (heures)'), max_digits=4, decimal_places=2,
                                validators=[MinValueValidator(Decimal('0.25')), MaxValueValidator(12)])
    observation = models.CharField(_('observation'), max_length=200, blank=True)
    saisi_par = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    cree_le = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _('heure de cours')
        verbose_name_plural = _('heures de cours')
        ordering = ['-date', '-cree_le']


class PaiementProfesseur(models.Model):
    professeur = models.ForeignKey(Professeur, on_delete=models.CASCADE, related_name='paiements')
    date = models.DateField(_('date'), default=timezone.localdate)
    montant = models.PositiveIntegerField(_('montant'), validators=[MinValueValidator(1)])
    mode = models.CharField(_('mode de paiement'), max_length=12, choices=ModePaiement.choices, default=ModePaiement.ESPECES)
    reference = models.CharField(_('référence'), max_length=80, blank=True)
    periode = models.CharField(_('période concernée'), max_length=40, blank=True, help_text=_('Ex. Septembre 2026'))
    numero = models.CharField(_('n° de bon'), max_length=20, unique=True, editable=False, null=True, blank=True)
    verse_par = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    cree_le = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _('paiement de professeur')
        verbose_name_plural = _('paiements de professeurs')
        ordering = ['-date', '-cree_le']

    def save(self, *args, **kwargs):
        with transaction.atomic():
            super().save(*args, **kwargs)
            if not self.numero:
                self.numero = f'V{self.date:%Y}-{self.pk:05d}'
                super().save(update_fields=['numero'])


# --------------------------------------------------------------------------- Compositions (examens internes)

class Composition(models.Model):
    """Session de compositions d'un établissement ; les notes sont reportées dans la colonne D1 ou D2."""

    etablissement = models.ForeignKey(Etablissement, on_delete=models.CASCADE, related_name='compositions')
    titre = models.CharField(_('intitulé'), max_length=120, help_text=_('Ex. Compositions du 1er trimestre'))
    periode = models.PositiveSmallIntegerField(_('période'), validators=[MinValueValidator(1), MaxValueValidator(3)])
    colonne = models.CharField(_('notes reportées dans'), max_length=2, choices=Colonne.choices, default=Colonne.D1)
    date_debut = models.DateField(_('du'))
    date_fin = models.DateField(_('au'))
    cree_le = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _('composition')
        ordering = ['-date_debut']

    def __str__(self):
        return self.titre

    def clean(self):
        if self.date_debut and self.date_fin and self.date_fin < self.date_debut:
            raise ValidationError(_('La date de fin doit être après la date de début.'))
        if self.etablissement_id and self.periode and self.periode > self.etablissement.nb_periodes:
            raise ValidationError(_('%(etablissement)s est organisé en %(decoupage)s.') % {'etablissement': self.etablissement, 'decoupage': self.etablissement.get_decoupage_display().lower()})

    @property
    def statut(self):
        today = timezone.localdate()
        if today < self.date_debut:
            return 'a_venir'
        return 'terminee' if today > self.date_fin else 'en_cours'


class Epreuve(models.Model):
    composition = models.ForeignKey(Composition, on_delete=models.CASCADE, related_name='epreuves')
    enseignement = models.ForeignKey(Enseignement, on_delete=models.CASCADE, related_name='epreuves', verbose_name=_('classe et matière'))
    date = models.DateField(_('date'))
    heure_debut = models.TimeField(_('début'))
    heure_fin = models.TimeField(_('fin'))
    salle = models.CharField(_('salle'), max_length=40, blank=True)
    surveillants = models.ManyToManyField(Professeur, blank=True, related_name='surveillances', verbose_name=_('surveillants'))

    class Meta:
        verbose_name = _('épreuve')
        ordering = ['date', 'heure_debut']

    def clean(self):
        if self.heure_debut and self.heure_fin and self.heure_fin <= self.heure_debut:
            raise ValidationError(_("L'heure de fin doit être après l'heure de début."))
        comp = self.composition if self.composition_id else None
        if comp and self.date and not (comp.date_debut <= self.date <= comp.date_fin):
            raise ValidationError(_('La date doit être comprise entre le %(date_debut)s et le %(date_fin)s.') % {'date_debut': format(comp.date_debut, '%d/%m'), 'date_fin': format(comp.date_fin, '%d/%m/%Y')})
        if comp and self.enseignement_id and self.enseignement.etablissement_id != comp.etablissement_id:
            raise ValidationError(_('Cette classe n’appartient pas à l’établissement de la composition.'))
        if not (self.date and self.heure_debut and self.heure_fin and self.enseignement_id):
            return
        autres = _chevauche(Epreuve.objects.filter(date=self.date).exclude(pk=self.pk), self.heure_debut, self.heure_fin)
        ens = self.enseignement
        conflit = autres.filter(enseignement__etablissement=ens.etablissement, enseignement__classe=ens.classe).first()
        if conflit:
            raise ValidationError(_('La %(classe)s compose déjà en %(matiere)s à ce moment-là.') % {'classe': ens.classe, 'matiere': conflit.enseignement.matiere})
        if self.salle:
            conflit = autres.filter(Q(salle__iexact=self.salle), enseignement__etablissement=ens.etablissement).first()
            if conflit:
                raise ValidationError(_('La salle %(salle)s est déjà prise par la %(classe)s.') % {'salle': self.salle, 'classe': conflit.enseignement.classe})

    def conflits_surveillants(self, surveillants):
        """Surveillants déjà occupés ailleurs sur ce créneau."""
        autres = _chevauche(Epreuve.objects.filter(date=self.date).exclude(pk=self.pk), self.heure_debut, self.heure_fin)
        return [p for p in surveillants if autres.filter(surveillants=p).exists()]

    def avancement_saisie(self):
        """(notes saisies, effectif) pour la colonne de la composition."""
        colonne = self.composition.colonne
        effectif = self.enseignement.eleves().count()
        saisies = FicheNote.objects.filter(
            enseignement=self.enseignement, periode=self.composition.periode, **{f'{colonne}__isnull': False}
        ).count()
        return saisies, effectif
