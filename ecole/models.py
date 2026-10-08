import re

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models, transaction
from django.db.models import F, OuterRef, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class Parametres(models.Model):
    """Réglages généraux (une seule ligne)."""

    annee_scolaire = models.CharField(_('année scolaire'), max_length=20, default='2026-2027')
    message_relance = models.TextField(
        'message de relance',
        default=(
            "Bonjour {parent}, sauf erreur de notre part, il reste {reste} à régler "
            "pour la scolarité de {eleve} ({classe}). Merci de passer à la caisse. — {ecole}"
        ),
        help_text=_('Variables disponibles : {parent}, {eleve}, {classe}, {reste}, {ecole}.'),
    )

    class Meta:
        verbose_name = _('paramètres')
        verbose_name_plural = _('paramètres')

    def __str__(self):
        return 'Paramètres'

    @classmethod
    def courant(cls):
        obj, _cree = cls.objects.get_or_create(pk=1)
        return obj


class Decoupage(models.TextChoices):
    TRIMESTRE = 'trimestre', _('Trimestres')
    SEMESTRE = 'semestre', _('Semestres')


class ModeEmecef(models.TextChoices):
    TEST = 'test', _('Test (développeur)')
    PRODUCTION = 'production', _('Production')


class GroupeTaxe(models.TextChoices):
    EXONERE = 'A', _('A — Exonéré')
    TAXABLE = 'B', _('B — Taxable (TVA 18 %)')


def numero_international(telephone):
    """Numéro au format international sans « + » (Bénin par défaut) : '01 97 12 34 56' -> '2290197123456'."""
    telephone = telephone or ''
    chiffres = re.sub(r'\D', '', telephone)
    if not chiffres:
        return ''
    if telephone.strip().startswith('+') or chiffres.startswith('00'):
        return chiffres.removeprefix('00')
    if chiffres.startswith('229'):
        return chiffres
    return '229' + chiffres

class Etablissement(models.Model):
    """École d'un élève. Créée automatiquement quand un nouveau nom est saisi à l'inscription."""

    nom = models.CharField(_("nom de l'établissement"), max_length=150, unique=True)
    adresse = models.CharField(_('adresse'), max_length=200, blank=True)
    telephone = models.CharField(_('téléphone'), max_length=50, blank=True)
    email = models.EmailField(_('e-mail'), blank=True)
    decoupage = models.CharField(
        'découpage de l’année', max_length=10, choices=Decoupage.choices, default=Decoupage.TRIMESTRE
    )
    # Identité fiscale (factures normalisées)
    ifu = models.CharField(
        _('IFU'), max_length=13, blank=True, validators=[RegexValidator(r'^\d{13}$', _("L'IFU compte 13 chiffres."))],
        help_text=_('Identifiant Fiscal Unique de l’établissement (13 chiffres).'),
    )
    rccm = models.CharField(_('RCCM'), max_length=40, blank=True)
    # Réductions pour fratrie (en % de la scolarité), appliquées automatiquement
    reduction_fratrie_2 = models.PositiveSmallIntegerField(
        _('réduction 2ᵉ enfant (%)'), default=0, validators=[MaxValueValidator(100)])
    reduction_fratrie_3 = models.PositiveSmallIntegerField(
        _('réduction 3ᵉ enfant et suivants (%)'), default=0, validators=[MaxValueValidator(100)])
    reduction_inscription_2 = models.PositiveSmallIntegerField(
        _("réduction sur l'inscription, 2ᵉ enfant (%)"), default=0, validators=[MaxValueValidator(100)])
    reduction_inscription_3 = models.PositiveSmallIntegerField(
        _("réduction sur l'inscription, 3ᵉ enfant et suivants (%)"), default=0, validators=[MaxValueValidator(100)],
        help_text=_('100 % = inscription gratuite à partir du 3ᵉ enfant.'))
    # Facturation normalisée e-MECeF (DGI Bénin)
    emecef_actif = models.BooleanField(_('normaliser les factures (e-MECeF)'), default=False)
    emecef_mode = models.CharField(_('environnement e-MECeF'), max_length=10, choices=ModeEmecef.choices,
                                   default=ModeEmecef.TEST)
    emecef_jeton = models.TextField(_('jeton e-MECeF'), blank=True,
                                    help_text=_('Jeton d’accès (JWT) délivré par la DGI pour cet IFU.'))
    groupe_taxe = models.CharField(_('groupe de taxation'), max_length=1, choices=GroupeTaxe.choices,
                                   default=GroupeTaxe.EXONERE)

    class Meta:
        verbose_name = _('établissement')
        ordering = ['nom']

    def __str__(self):
        return self.nom

    @property
    def nb_periodes(self):
        return 2 if self.decoupage == Decoupage.SEMESTRE else 3

    @property
    def periodes(self):
        """[(1, '1er trimestre'), (2, '2e trimestre'), ...]"""
        return [(n, libelle_periode(n, self.decoupage)) for n in range(1, self.nb_periodes + 1)]

    @classmethod
    def depuis_nom(cls, nom):
        """Retrouve l'établissement portant ce nom (sans tenir compte des majuscules) ou le crée."""
        nom = ' '.join(nom.split())
        existant = cls.objects.filter(nom__iexact=nom).first()
        return existant or cls.objects.create(nom=nom)


def libelle_periode(numero, decoupage):
    from django.utils.translation import get_language

    if (get_language() or '').startswith('en'):
        mot = 'semester' if decoupage == Decoupage.SEMESTRE else 'term'
        return f"{numero}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(numero, 'th') } {mot}"
    mot = 'semestre' if decoupage == Decoupage.SEMESTRE else 'trimestre'
    return f"{'1er' if numero == 1 else f'{numero}e'} {mot}"


class Sexe(models.TextChoices):
    MASCULIN = 'M', _('Masculin')
    FEMININ = 'F', _('Féminin')


class NatureParent(models.TextChoices):
    PERE = 'pere', _('Père')
    MERE = 'mere', _('Mère')
    TUTEUR = 'tuteur', _('Tuteur')
    TUTRICE = 'tutrice', _('Tutrice')
    AUTRE = 'autre', _('Autre')
class Motif(models.TextChoices):
    SCOLARITE = 'scolarite', _('Scolarité')
    INSCRIPTION = 'inscription', _("Frais d'inscription")
    # Anciens motifs (paiements enregistrés avant l'imputation automatique)
    TRANCHE1 = 'tranche1', _('1ʳᵉ tranche')
    TRANCHE2 = 'tranche2', _('2ᵉ tranche')
    TRANCHE3 = 'tranche3', _('3ᵉ tranche')
# Les paiements de scolarité sont imputés dans l'ordre sur les tranches de l'échéancier.
# Les frais d'inscription sont suivis à part.
TRANCHES = [Motif.SCOLARITE, Motif.TRANCHE1, Motif.TRANCHE2, Motif.TRANCHE3]


def _somme_paiements(motifs):
    sous_requete = (
        Paiement.objects.filter(eleve=OuterRef('pk'), motif__in=motifs)
        .order_by()
        .values('eleve')
        .annotate(s=Sum('montant'))
        .values('s')
    )
    return Coalesce(Subquery(sous_requete), Value(0))


class EleveQuerySet(models.QuerySet):
    def avec_totaux(self):
        derniere_relance = (
            Relance.objects.filter(eleve=OuterRef('pk')).order_by('-cree_le').values('cree_le')[:1]
        )
        return self.annotate(
            verse_tranches=_somme_paiements(TRANCHES),
            verse_inscription=_somme_paiements([Motif.INSCRIPTION]),
            derniere_relance=Subquery(derniere_relance),
        )

    def actifs(self):
        """Élèves non archivés (ceux qui apparaissent dans les listes, statistiques et relances)."""
        return self.filter(archive=False)

    def archives(self):
        return self.filter(archive=True)

    def soldes(self):
        return self.avec_totaux().filter(verse_tranches__gte=F('montant_scolarite'))

    def impayes(self):
        return self.avec_totaux().filter(verse_tranches__lt=F('montant_scolarite'))


class Eleve(models.Model):
    nom = models.CharField(_('nom'), max_length=100)
    prenom = models.CharField(_('prénom(s)'), max_length=150)
    sexe = models.CharField(_('sexe'), max_length=1, choices=Sexe.choices, blank=True)
    date_naissance = models.DateField(_('date de naissance'), null=True, blank=True)
    lieu_naissance = models.CharField(_('lieu de naissance'), max_length=100, blank=True)
    parent = models.CharField(_('parent ou tuteur'), max_length=150)
    nature_parent = models.CharField(
        'nature du parent', max_length=10, choices=NatureParent.choices, blank=True
    )
    classe = models.CharField(_('classe'), max_length=50, db_index=True)
    telephone = models.CharField(_('téléphone'), max_length=30)
    whatsapp = models.CharField(_('WhatsApp du parent'), max_length=30, blank=True,
                                help_text=_('Laisser vide si c’est le même numéro que le téléphone.'))
    email = models.EmailField(_('e-mail du parent'), blank=True)
    suivi_actif = models.BooleanField(_('rappels automatiques'), default=True,
                                      help_text=_('Le parent reçoit les rappels avant chaque échéance et en cas de retard.'))
    echeancier_personnalise = models.BooleanField(_('échéancier personnalisé'), default=False)
    residence = models.CharField(_('résidence'), max_length=200, blank=True)
    educ_master = models.CharField(_('numéro Educ Master'), max_length=50, blank=True)
    montant_scolarite = models.PositiveIntegerField(_('montant total de la scolarité'))
    frais_inscription = models.PositiveIntegerField(
        _("frais d'inscription (avant réduction)"), default=0,
        help_text=_("Rempli automatiquement si les frais de la classe sont définis."))
    etablissement = models.ForeignKey(
        Etablissement, on_delete=models.PROTECT, related_name='eleves', verbose_name=_('établissement')
    )

    cree_le = models.DateTimeField(_('enregistré le'), auto_now_add=True)
    modifie_le = models.DateTimeField(_('modifié le'), auto_now=True)
    # Un élève n'est jamais supprimé : il est archivé (départ, transfert…) et peut être réactivé.
    archive = models.BooleanField(_('archivé'), default=False, db_index=True)
    archive_le = models.DateTimeField(_('archivé le'), null=True, blank=True)
    archive_par = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='+', verbose_name=_('archivé par'))
    motif_archive = models.CharField(_("motif de l'archivage"), max_length=200, blank=True)
    import_fichier = models.ForeignKey('ImportFichier', on_delete=models.SET_NULL, null=True, blank=True,
                                       related_name='eleves', verbose_name=_("fichier d'import"))

    objects = EleveQuerySet.as_manager()

    class Meta:
        verbose_name = _('élève')
        ordering = ['etablissement__nom', 'classe', 'nom', 'prenom']
        permissions = [
            ('exporter_donnees', 'Peut exporter les données'),
            ('gerer_utilisateurs', 'Peut gérer les utilisateurs et les paramètres'),
            ('voir_statuts', 'Peut consulter le statut de paiement (surveillants)'),
            ('archiver_eleve', 'Peut archiver et réactiver des élèves'),
        ]

    def __str__(self):
        return f'{self.nom.upper()} {self.prenom}'

    def _somme(self, motifs):
        return self.paiements.filter(motif__in=motifs).aggregate(s=Sum('montant'))['s'] or 0

    @property
    def total_verse(self):
        """Total versé sur les tranches (hors inscription)."""
        v = getattr(self, 'verse_tranches', None)
        return self._somme(TRANCHES) if v is None else v

    @property
    def total_inscription(self):
        v = getattr(self, 'verse_inscription', None)
        return self._somme([Motif.INSCRIPTION]) if v is None else v

    @property
    def reductions_inscription(self):
        """[(libellé, montant)] : fratrie automatique et remises accordées sur les frais d'inscription."""
        if not hasattr(self, '_reductions_inscription'):
            from .echeancier import detail_reductions_inscription

            self._reductions_inscription = detail_reductions_inscription(self)
        return self._reductions_inscription

    @property
    def reduction_inscription(self):
        return sum(m for _l, m in self.reductions_inscription)

    @property
    def inscription_due(self):
        """Frais d'inscription à payer après réductions (fratrie, remise, exonération)."""
        return max(0, self.frais_inscription - self.reduction_inscription)

    @property
    def inscription_reste(self):
        return max(0, self.inscription_due - self.total_inscription)

    @property
    def statut_inscription(self):
        """'payee', 'partielle', 'impayee' ou 'non_definie' (aucun montant dû ni versé)."""
        verse = self.total_inscription
        if self.frais_inscription and not self.inscription_due:
            return 'payee'  # exonérée
        if self.frais_inscription:
            if verse >= self.inscription_due:
                return 'payee'
            return 'partielle' if verse else 'impayee'
        return 'payee' if verse else 'non_definie'

    @property
    def statut_inscription_libelle(self):
        if self.statut_inscription == 'payee' and self.frais_inscription and not self.inscription_due:
            return _('Inscription offerte')
        return {'payee': _('Inscription payée'), 'partielle': _('Inscription partielle'),
                'impayee': _('Inscription non payée'), 'non_definie': _('Inscription : montant non défini')}[
            self.statut_inscription]

    @property
    def reste_a_payer(self):
        return max(0, self.montant_scolarite - self.total_verse)

    @property
    def taux_paye(self):
        if not self.montant_scolarite:
            return 100
        return min(100, round(self.total_verse * 100 / self.montant_scolarite))

    @property
    def statut(self):
        """'solde', 'partiel' ou 'aucun'."""
        if self.reste_a_payer == 0:
            return 'solde'
        return 'partiel' if self.total_verse > 0 else 'aucun'

    @property
    def statut_libelle(self):
        return {'solde': _('Soldé'), 'partiel': _('Partiel'), 'aucun': _('Impayé')}[self.statut]

    @property
    def initiales(self):
        return (self.nom[:1] + self.prenom[:1]).upper()

    @property
    def telephone_international(self):
        """Numéro au format international sans « + » (Bénin par défaut), pour SMS et WhatsApp."""
        return numero_international(self.telephone)

    @property
    def whatsapp_international(self):
        return numero_international(self.whatsapp) or self.telephone_international

    @property
    def situation(self):
        """Échéancier imputé : tranches payées, en retard, à venir (voir ecole/echeancier.py)."""
        if not hasattr(self, '_situation'):
            from .echeancier import situation

            self._situation = situation(self)
        return self._situation


class ModePaiement(models.TextChoices):
    ESPECES = 'especes', _('Espèces')
    MOBILE_MONEY = 'mobile_money', _('Mobile Money')
    VIREMENT = 'virement', _('Virement bancaire')
    CHEQUE = 'cheque', _('Chèque')
class Paiement(models.Model):
    eleve = models.ForeignKey(Eleve, on_delete=models.CASCADE, related_name='paiements', verbose_name=_('élève'))
    motif = models.CharField(_('motif'), max_length=12, choices=Motif.choices)
    montant = models.PositiveIntegerField(_('montant'), validators=[MinValueValidator(1)])
    mode = models.CharField(_('mode de paiement'), max_length=12, choices=ModePaiement.choices, default=ModePaiement.ESPECES)
    date = models.DateField(_('date du paiement'), default=timezone.localdate)
    reference = models.CharField(
        'référence', max_length=80, blank=True, help_text=_('N° de transaction Mobile Money, de chèque…')
    )
    observation = models.CharField(_('observation'), max_length=200, blank=True)
    numero_recu = models.CharField(_('n° de reçu'), max_length=20, unique=True, editable=False, blank=True, null=True)
    encaisse_par = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='paiements_encaisses', verbose_name=_('encaissé par'),
    )
    cree_le = models.DateTimeField(_('enregistré le'), auto_now_add=True)

    class Meta:
        verbose_name = _('paiement')
        ordering = ['-date', '-cree_le']

    def __str__(self):
        return f'{self.numero_recu} — {self.eleve} — {self.montant}'

    def save(self, *args, **kwargs):
        with transaction.atomic():
            super().save(*args, **kwargs)
            if not self.numero_recu:
                self.numero_recu = f'R{self.date:%Y}-{self.pk:05d}'
                super().save(update_fields=['numero_recu'])


class CanalRelance(models.TextChoices):
    SMS = 'sms', _('SMS')
    WHATSAPP = 'whatsapp', _('WhatsApp')
class Relance(models.Model):
    eleve = models.ForeignKey(Eleve, on_delete=models.CASCADE, related_name='relances', verbose_name=_('élève'))
    canal = models.CharField(_('canal'), max_length=10, choices=CanalRelance.choices)
    reste_a_payer = models.PositiveIntegerField(_('reste à payer au moment de la relance'))
    envoye_par = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, verbose_name=_('envoyée par')
    )
    cree_le = models.DateTimeField(_('date'), auto_now_add=True)

    class Meta:
        verbose_name = _('relance')
        ordering = ['-cree_le']



class CodeModule(models.Model):
    """Code personnel d'un utilisateur pour entrer dans un module (stocké chiffré, comme un mot de passe)."""

    utilisateur = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='codes_modules', verbose_name=_('utilisateur'),
    )
    module = models.CharField(_('module'), max_length=20)
    code = models.CharField(_('code chiffré'), max_length=128)
    modifie_le = models.DateTimeField(_('modifié le'), auto_now=True)

    class Meta:
        verbose_name = _("code d'accès d'un module")
        verbose_name_plural = _("codes d'accès des modules")
        constraints = [models.UniqueConstraint(fields=['utilisateur', 'module'], name='code_module_unique')]

    def __str__(self):
        return f'{self.utilisateur} — {self.module}'


# --------------------------------------------------------------------------- Grille des frais et échéancier

class GrilleFrais(models.Model):
    """Frais d'une classe : tranches (montant et date d'échéance) et frais d'inscription."""

    etablissement = models.ForeignKey(Etablissement, on_delete=models.CASCADE, related_name='grilles',
                                      verbose_name=_('établissement'))
    classe = models.CharField(_('classe'), max_length=50)
    frais_inscription = models.PositiveIntegerField(_("frais d'inscription"), default=0)
    modifie_le = models.DateTimeField(_('modifié le'), auto_now=True)

    class Meta:
        verbose_name = _('grille des frais')
        verbose_name_plural = _('grilles des frais')
        ordering = ['etablissement__nom', 'classe']
        constraints = [models.UniqueConstraint(fields=['etablissement', 'classe'], name='grille_unique')]

    def __str__(self):
        return f'{self.classe} — {self.etablissement}'

    @property
    def total(self):
        return sum(t.montant for t in self.tranches.all())


class TrancheGrille(models.Model):
    grille = models.ForeignKey(GrilleFrais, on_delete=models.CASCADE, related_name='tranches')
    numero = models.PositiveSmallIntegerField(_('n°'))
    libelle = models.CharField(_('libellé'), max_length=60, blank=True)
    montant = models.PositiveIntegerField(_('montant'), validators=[MinValueValidator(1)])
    echeance = models.DateField(_("date d'échéance"))

    class Meta:
        verbose_name = _('tranche')
        ordering = ['numero']
        constraints = [models.UniqueConstraint(fields=['grille', 'numero'], name='tranche_grille_unique')]

    def __str__(self):
        return self.libelle or libelle_tranche(self.numero)


def libelle_tranche(numero):
    from django.utils.translation import gettext

    return gettext('Tranche %(n)s') % {'n': numero}


class Echeance(models.Model):
    """Une tranche de l'échéancier d'un élève (issue de la grille de sa classe, ou personnalisée)."""

    eleve = models.ForeignKey(Eleve, on_delete=models.CASCADE, related_name='echeances', verbose_name=_('élève'))
    numero = models.PositiveSmallIntegerField(_('n°'))
    libelle = models.CharField(_('libellé'), max_length=60, blank=True)
    montant_brut = models.PositiveIntegerField(_('montant avant réduction'))
    reduction = models.PositiveIntegerField(_('réduction'), default=0)
    echeance = models.DateField(_("date d'échéance"))

    class Meta:
        verbose_name = _('échéance')
        ordering = ['numero']
        constraints = [models.UniqueConstraint(fields=['eleve', 'numero'], name='echeance_unique')]

    def __str__(self):
        return self.libelle or libelle_tranche(self.numero)

    @property
    def montant(self):
        return max(0, self.montant_brut - self.reduction)


class TypeReduction(models.TextChoices):
    BOURSE = 'bourse', _('Bourse')
    REMISE = 'remise', _('Remise personnalisée')


class ModeReduction(models.TextChoices):
    POURCENTAGE = 'pourcentage', _('Pourcentage')
    MONTANT = 'montant', _('Montant fixe')


class PorteeReduction(models.TextChoices):
    SCOLARITE = 'scolarite', _('Scolarité')
    INSCRIPTION = 'inscription', _("Frais d'inscription")


class ReductionEleve(models.Model):
    """Bourse ou remise accordée à un élève (la réduction fratrie est calculée automatiquement)."""

    eleve = models.ForeignKey(Eleve, on_delete=models.CASCADE, related_name='reductions', verbose_name=_('élève'))
    porte_sur = models.CharField(_('porte sur'), max_length=12, choices=PorteeReduction.choices,
                                 default=PorteeReduction.SCOLARITE)
    type = models.CharField(_('type'), max_length=12, choices=TypeReduction.choices, default=TypeReduction.REMISE)
    mode = models.CharField(_('calcul'), max_length=12, choices=ModeReduction.choices, default=ModeReduction.POURCENTAGE)
    valeur = models.PositiveIntegerField(_('valeur'), validators=[MinValueValidator(1)])
    motif = models.CharField(_('motif'), max_length=150, blank=True)
    cree_par = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    cree_le = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _('réduction')
        ordering = ['cree_le']

    def montant_sur(self, brut):
        if self.mode == ModeReduction.POURCENTAGE:
            return round(brut * min(self.valeur, 100) / 100)
        return min(self.valeur, brut)


class ContactParent(models.Model):
    """Autre parent ou tuteur à prévenir (en plus du contact principal de la fiche)."""

    eleve = models.ForeignKey(Eleve, on_delete=models.CASCADE, related_name='contacts', verbose_name=_('élève'))
    nom = models.CharField(_('nom'), max_length=150)
    nature = models.CharField(_('lien'), max_length=10, choices=NatureParent.choices, blank=True)
    telephone = models.CharField(_('téléphone'), max_length=30, blank=True)
    whatsapp = models.CharField(_('WhatsApp'), max_length=30, blank=True)
    email = models.EmailField(_('e-mail'), blank=True)
    notifier = models.BooleanField(_('reçoit les factures et rappels'), default=True)

    class Meta:
        verbose_name = _('contact parent')
        ordering = ['nom']

    def __str__(self):
        return self.nom


# --------------------------------------------------------------------------- Factures

class TypeFacture(models.TextChoices):
    VENTE = 'FV', _('Facture')
    AVOIR = 'FA', _("Facture d'avoir")


class StatutFacture(models.TextChoices):
    NORMALISEE = 'normalisee', _('Normalisée')
    EN_ATTENTE = 'en_attente', _('Non normalisée')
    ERREUR = 'erreur', _('Erreur de normalisation')


class Facture(models.Model):
    """Facture émise à chaque encaissement ; une correction donne une facture d'avoir puis une nouvelle facture."""

    eleve = models.ForeignKey(Eleve, on_delete=models.CASCADE, related_name='factures', verbose_name=_('élève'))
    paiement = models.ForeignKey(Paiement, on_delete=models.SET_NULL, null=True, blank=True, related_name='factures')
    type = models.CharField(_('type'), max_length=2, choices=TypeFacture.choices, default=TypeFacture.VENTE)
    numero = models.CharField(_('n° de facture'), max_length=20, unique=True, editable=False, null=True, blank=True)
    date = models.DateTimeField(_('date'), default=timezone.now)
    montant = models.PositiveIntegerField(_('montant'))
    mode = models.CharField(_('mode de paiement'), max_length=12, choices=ModePaiement.choices)
    lignes = models.JSONField(_('lignes'), default=list)  # [{'libelle': ..., 'montant': ...}]
    client_nom = models.CharField(_('client'), max_length=150)
    client_contact = models.CharField(_('contact du client'), max_length=60, blank=True)
    ifu = models.CharField(_('IFU'), max_length=13, blank=True)
    groupe_taxe = models.CharField(_('groupe de taxation'), max_length=1, choices=GroupeTaxe.choices,
                                   default=GroupeTaxe.EXONERE)
    facture_origine = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True,
                                        related_name='avoirs', verbose_name=_('facture corrigée'))
    annulee = models.BooleanField(_('annulée par un avoir'), default=False)
    statut = models.CharField(_('statut'), max_length=12, choices=StatutFacture.choices,
                              default=StatutFacture.EN_ATTENTE)
    # Réponse e-MECeF
    emecef_uid = models.CharField(max_length=60, blank=True)
    code_mecef = models.CharField(_('code MECeF/DGI'), max_length=40, blank=True)
    qr_code = models.TextField(blank=True)
    compteurs = models.CharField(max_length=60, blank=True)
    nim = models.CharField(_('NIM'), max_length=30, blank=True)
    date_mecef = models.CharField(max_length=30, blank=True)
    erreur = models.TextField(blank=True)
    emise_par = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        verbose_name = _('facture')
        ordering = ['-date']

    def __str__(self):
        return self.numero or '—'

    def save(self, *args, **kwargs):
        with transaction.atomic():
            super().save(*args, **kwargs)
            if not self.numero:
                prefixe = 'A' if self.type == TypeFacture.AVOIR else 'F'
                self.numero = f'{prefixe}{timezone.localtime(self.date):%Y}-{self.pk:05d}'
                super().save(update_fields=['numero'])

    @property
    def est_avoir(self):
        return self.type == TypeFacture.AVOIR


# --------------------------------------------------------------------------- Traçabilité des corrections

class ActionCorrection(models.TextChoices):
    MODIFICATION = 'modification', _('Modification')
    ANNULATION = 'annulation', _('Annulation')


class StatutDemande(models.TextChoices):
    EN_ATTENTE = 'en_attente', _('En attente')
    VALIDEE = 'validee', _('Validée')
    REFUSEE = 'refusee', _('Refusée')


class DemandeCorrection(models.Model):
    """Correction d'un paiement demandée par la caisse ou le secrétariat, à valider par la direction."""

    eleve = models.ForeignKey(Eleve, on_delete=models.CASCADE, related_name='demandes_correction')
    paiement = models.ForeignKey(Paiement, on_delete=models.SET_NULL, null=True, blank=True, related_name='demandes')
    numero_recu = models.CharField(_('n° de reçu'), max_length=20)
    action =models.CharField(_('action'), max_length=12, choices=ActionCorrection.choices,
                              default=ActionCorrection.MODIFICATION)
    nouvelles_valeurs = models.JSONField(_('nouvelles valeurs'), default=dict, blank=True)
    motif = models.TextField(_('motif de la correction'))
    statut = models.CharField(_('statut'), max_length=12, choices=StatutDemande.choices, default=StatutDemande.EN_ATTENTE)
    demande_par = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                    related_name='corrections_demandees')
    cree_le = models.DateTimeField(auto_now_add=True)
    traite_par = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='corrections_traitees')
    traite_le = models.DateTimeField(null=True, blank=True)
    commentaire = models.CharField(_('commentaire'), max_length=200, blank=True)

    class Meta:
        verbose_name = _('demande de correction')
        verbose_name_plural = _('demandes de correction')
        ordering = ['-cree_le']
        permissions = [('valider_corrections', 'Peut corriger les paiements et valider les demandes')]


class HistoriquePaiement(models.Model):
    """Trace de chaque correction ou annulation d'un paiement : qui, quand, pourquoi, avant / après."""

    eleve = models.ForeignKey(Eleve, on_delete=models.CASCADE, related_name='historique_paiements')
    paiement = models.ForeignKey(Paiement, on_delete=models.SET_NULL, null=True, blank=True, related_name='historique')
    numero_recu = models.CharField(_('n° de reçu'), max_length=20)
    action = models.CharField(_('action'), max_length=12, choices=ActionCorrection.choices)
    avant = models.JSONField(_('avant'), default=dict)
    apres = models.JSONField(_('après'), default=dict, blank=True)
    motif = models.TextField(_('motif'))
    demande_par = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='+')
    valide_par = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    cree_le = models.DateTimeField(_('date'), auto_now_add=True)

    class Meta:
        verbose_name = _('correction de paiement')
        verbose_name_plural = _('corrections de paiements')
        ordering = ['-cree_le']


# --------------------------------------------------------------------------- Rappels et messages

class MomentRelance(models.TextChoices):
    AVANT = 'avant', _("Avant l'échéance")
    JOUR = 'jour', _("Le jour de l'échéance")
    APRES = 'apres', _('En cas de retard')


class RegleRelance(models.Model):
    """Quand et comment prévenir les parents (ex. 7 jours avant, le jour même, puis en cas de retard)."""

    etablissement = models.ForeignKey(Etablissement, on_delete=models.CASCADE, null=True, blank=True,
                                      related_name='regles_relance', verbose_name=_('établissement'),
                                      help_text=_('Laisser vide pour tous les établissements.'))
    moment = models.CharField(_('moment'), max_length=6, choices=MomentRelance.choices)
    jours = models.PositiveSmallIntegerField(_('nombre de jours'), default=0,
                                             help_text=_("Jours avant l'échéance, ou jours de retard."))
    repeter_tous_les = models.PositiveSmallIntegerField(
        _('répéter tous les (jours)'), default=0, help_text=_('Retards uniquement : 0 = une seule fois.'))
    whatsapp = models.BooleanField(_('WhatsApp'), default=True)
    sms = models.BooleanField(_('SMS'), default=False)
    email = models.BooleanField(_('e-mail'), default=True)
    message = models.TextField(
        _('message'),
        help_text=_('Variables : {parent}, {eleve}, {classe}, {tranche}, {montant}, {echeance}, {reste}, {retard}, {ecole}.'),
    )
    actif = models.BooleanField(_('active'), default=True)

    class Meta:
        verbose_name = _('règle de relance')
        verbose_name_plural = _('règles de relance')
        ordering = ['moment', 'jours']

    def __str__(self):
        if self.moment == MomentRelance.AVANT:
            return str(_('%(n)s jour(s) avant l’échéance')) % {'n': self.jours}
        if self.moment == MomentRelance.JOUR:
            return str(_('Le jour de l’échéance'))
        texte = str(_('%(n)s jour(s) de retard')) % {'n': self.jours}
        if self.repeter_tous_les:
            texte += str(_(', puis tous les %(n)s jours')) % {'n': self.repeter_tous_les}
        return texte

    @property
    def canaux(self):
        return [c for c in ('whatsapp', 'sms', 'email') if getattr(self, c)]


class CanalMessage(models.TextChoices):
    WHATSAPP = 'whatsapp', _('WhatsApp')
    SMS = 'sms', _('SMS')
    EMAIL = 'email', _('E-mail')


class TypeMessage(models.TextChoices):
    FACTURE = 'facture', _('Facture')
    RAPPEL = 'rappel', _('Rappel d’échéance')
    RETARD = 'retard', _('Relance de retard')
    QUESTION = 'question', _('Message reçu')
    REPONSE = 'reponse', _('Réponse de l’assistant')


class StatutMessage(models.TextChoices):
    ENVOYE = 'envoye', _('Envoyé')
    SIMULE = 'simule', _('Simulé (canal non configuré)')
    ECHEC = 'echec', _('Échec')
    RECU = 'recu', _('Reçu')


class Notification(models.Model):
    """Journal de tous les messages échangés avec les parents (envoyés ou reçus)."""

    eleve = models.ForeignKey(Eleve, on_delete=models.SET_NULL, null=True, blank=True, related_name='notifications')
    facture = models.ForeignKey(Facture, on_delete=models.SET_NULL, null=True, blank=True, related_name='notifications')
    canal = models.CharField(_('canal'), max_length=10, choices=CanalMessage.choices)
    type = models.CharField(_('type'), max_length=10, choices=TypeMessage.choices)
    destinataire = models.CharField(_('destinataire'), max_length=150)
    objet = models.CharField(_('objet'), max_length=200, blank=True)
    message = models.TextField(_('message'))
    statut = models.CharField(_('statut'), max_length=8, choices=StatutMessage.choices)
    erreur = models.TextField(blank=True)
    cle = models.CharField(max_length=120, blank=True, db_index=True)  # évite d'envoyer deux fois le même rappel
    cree_le = models.DateTimeField(_('date'), auto_now_add=True)

    class Meta:
        verbose_name = _('message')
        verbose_name_plural = _('messages')
        ordering = ['-cree_le']


# --------------------------------------------------------------------------- Espaces parents et surveillants

class CompteParent(models.Model):
    """Accès d'un parent à l'espace parents : ses enfants, leurs paiements et leurs factures."""

    utilisateur = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='compte_parent')
    telephone = models.CharField(_('téléphone'), max_length=20)
    eleves = models.ManyToManyField(Eleve, related_name='comptes_parents', verbose_name=_('enfants'))
    cree_le = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _('accès parent')
        verbose_name_plural = _('accès parents')

    def __str__(self):
        return self.utilisateur.get_full_name() or self.utilisateur.username


class PerimetreSurveillant(models.Model):
    """Classes dont un surveillant peut consulter le statut de paiement (vide = toutes)."""

    utilisateur = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='perimetre')
    classes = models.JSONField(_('classes'), default=list, blank=True)  # ["<etab_id>|<classe>", ...]

    class Meta:
        verbose_name = _('périmètre de surveillant')

    def filtrer(self, eleves):
        if not self.classes:
            return eleves
        from django.db.models import Q

        condition = Q(pk__in=[])
        for cle in self.classes:
            etab_id, _sep, classe = cle.partition('|')
            if etab_id.isdigit():
                condition |= Q(etablissement_id=int(etab_id), classe=classe)
        return eleves.filter(condition)


class ImputationPaiement(models.Model):
    """Part d'un paiement de scolarité affectée à une tranche, avec les soldes juste après ce paiement.

    Recalculée à chaque changement (paiement, correction, annulation, échéancier, réduction) :
    les paiements sont pris dans l'ordre de leur date puis de leur saisie, et chacun remplit d'abord
    la plus ancienne tranche non soldée, le surplus passant à la tranche suivante.
    """

    eleve = models.ForeignKey(Eleve, on_delete=models.CASCADE, related_name='imputations')
    paiement = models.ForeignKey(Paiement, on_delete=models.CASCADE, related_name='imputations')
    echeance = models.ForeignKey(Echeance, on_delete=models.SET_NULL, null=True, blank=True, related_name='imputations')
    ordre = models.PositiveIntegerField()
    libelle = models.CharField(_('tranche'), max_length=80)
    date_echeance = models.DateField(_("date d'échéance"), null=True, blank=True)
    montant_tranche = models.PositiveIntegerField(_('montant de la tranche'), default=0)
    montant = models.PositiveIntegerField(_('montant affecté'))
    paye_tranche = models.PositiveIntegerField(_('payé sur la tranche après ce paiement'), default=0)
    reste_tranche = models.PositiveIntegerField(_('reste sur la tranche après ce paiement'), default=0)
    reste_total = models.PositiveIntegerField(_('reste de la scolarité après ce paiement'), default=0)

    class Meta:
        verbose_name = _('affectation de paiement')
        verbose_name_plural = _('affectations de paiements')
        ordering = ['ordre']

    @property
    def soldee(self):
        return self.echeance_id is not None and self.reste_tranche == 0


class ImportFichier(models.Model):
    """Trace d'un fichier importé : son empreinte permet de prévenir quand le même fichier est chargé de nouveau."""

    empreinte = models.CharField(_('empreinte du contenu'), max_length=64, db_index=True)
    nom = models.CharField(_('nom du fichier'), max_length=200, blank=True)
    importe_le = models.DateTimeField(_('importé le'), auto_now_add=True)
    importe_par = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='+', verbose_name=_('importé par'))
    nb_eleves = models.PositiveIntegerField(_('élèves créés'), default=0)
    nb_paiements = models.PositiveIntegerField(_('paiements repris'), default=0)

    class Meta:
        verbose_name = _("import d'élèves")
        verbose_name_plural = _("imports d'élèves")
        ordering = ['-importe_le']

    def __str__(self):
        return f'{self.nom or self.empreinte[:10]} ({self.importe_le:%d/%m/%Y})'


def remplacer_variables(modele, **valeurs):
    """Remplace {nom} par sa valeur pour les seules variables connues (pas de format() : aucun accès
    aux attributs ni format démesuré possible depuis un message saisi par l'utilisateur)."""
    return re.sub(r'\{(\w+)\}', lambda m: str(valeurs[m.group(1)]) if m.group(1) in valeurs else m.group(0), modele)
