from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.forms import inlineformset_factory, modelformset_factory
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from . import roles, verrou
from .modules import ADMINISTRATION, MODULES, accessible, bientot
from .models import (
    TRANCHES, ActionCorrection, ContactParent, Echeance, Eleve, Etablissement, GrilleFrais, ModePaiement, Motif,
    Paiement, Parametres, ReductionEleve, RegleRelance, TrancheGrille,
)
from .templatetags.ecole_extras import fcfa

MONTANT_ATTRS = {'inputmode': 'numeric', 'min': 0, 'placeholder': '0'}


class EleveForm(forms.ModelForm):
    etablissement_nom = forms.CharField(
        label=_('Établissement'), max_length=150,
        widget=forms.TextInput(attrs={'list': 'etablissements-existants', 'placeholder': _('Ex. CEG Akpakpa')}),
        help_text=_('Choisissez un établissement proposé ou tapez un nouveau nom : il sera créé automatiquement.'),
    )

    class Meta:
        model = Eleve
        fields = [
            'nom', 'prenom', 'sexe', 'date_naissance', 'lieu_naissance', 'classe', 'educ_master', 'residence',
            'parent', 'nature_parent', 'telephone', 'whatsapp', 'email', 'suivi_actif', 'montant_scolarite',
            'frais_inscription',
        ]
        widgets = {
            'whatsapp': forms.TextInput(attrs={'type': 'tel', 'inputmode': 'tel', 'placeholder': _('Même numéro')}),
            'classe': forms.TextInput(attrs={'list': 'classes-existantes', 'placeholder': _('Ex. CM2, 6ème A')}),
            'residence': forms.TextInput(attrs={'placeholder': _('Quartier, ville')}),
            'telephone': forms.TextInput(attrs={'type': 'tel', 'inputmode': 'tel', 'placeholder': '01 97 00 00 00'}),
            'nature_parent': forms.RadioSelect,
            'sexe': forms.RadioSelect,
            'date_naissance': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
            'montant_scolarite': forms.NumberInput(attrs=MONTANT_ATTRS),
            'frais_inscription': forms.NumberInput(attrs=MONTANT_ATTRS),
        }

    def __init__(self, *args, utilisateur=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['nature_parent'].choices = [c for c in self.fields['nature_parent'].choices if c[0]]
        self.fields['sexe'].choices = [c for c in self.fields['sexe'].choices if c[0]]
        self.fields['montant_scolarite'].required = False
        self.fields['frais_inscription'].required = False
        self.fields['frais_inscription'].label = _("Frais d'inscription (avant réduction)")
        if self.instance.pk and utilisateur is not None and not utilisateur.has_perm('ecole.change_echeance'):
            # Modifier ce qu'un élève inscrit doit payer est réservé à la direction.
            self.fields['montant_scolarite'].disabled = True
            self.fields['frais_inscription'].disabled = True
        self.fields['montant_scolarite'].help_text = _(
            'Rempli automatiquement si les frais de la classe sont définis (Scolarité › Frais par classe).')
        if self.instance.pk:
            self.initial.setdefault('etablissement_nom', self.instance.etablissement.nom)
            from .echeancier import montant_brut

            self.initial['montant_scolarite'] = montant_brut(self.instance)
        elif Etablissement.objects.count() == 1:
            self.initial.setdefault('etablissement_nom', Etablissement.objects.get().nom)

    def save(self, commit=True):
        self.instance.etablissement = Etablissement.depuis_nom(self.cleaned_data['etablissement_nom'])
        return super().save(commit=commit)

    def clean(self):
        data = super().clean()
        for name in ('nom', 'prenom', 'classe', 'parent', 'telephone', 'etablissement_nom'):
            if isinstance(data.get(name), str):
                data[name] = data[name].strip()
        self.grille = None
        if data.get('etablissement_nom') and data.get('classe'):
            from .echeancier import trouver_grille

            self.grille = trouver_grille(data['etablissement_nom'], data['classe'])
            if self.grille:
                data['classe'] = self.grille.classe  # même écriture que dans « Frais par classe »
        montant = data.get('montant_scolarite')
        if self.grille:
            data['montant_scolarite'] = self.instance.montant_scolarite if self.instance.pk else self.grille.total
            data['frais_inscription'] = self.grille.frais_inscription
            return data
        if data.get('frais_inscription') is None:
            data['frais_inscription'] = self.instance.frais_inscription if self.instance.pk else 0
        if montant is None:
            if self.instance.pk and self.instance.echeancier_personnalise:
                data['montant_scolarite'] = self.instance.montant_scolarite
                return data
            self.add_error('montant_scolarite', _('Aucune grille des frais pour cette classe : saisissez le montant.'))
            return data
        if self.instance.pk and montant < self.instance.total_verse:
            self.add_error(
                'montant_scolarite',
                _('Déjà %(total_verse)s versés sur les tranches : le montant ne peut pas être inférieur.') % {'total_verse': fcfa(self.instance.total_verse)},
            )
        return data


class InscriptionForm(EleveForm):
    """Formulaire d'inscription : fiche élève + paiements versés le jour de l'inscription."""

    PAIEMENTS_INITIAUX = [
        ('inscription_versee', Motif.INSCRIPTION),
        ('versement', Motif.SCOLARITE),
    ]

    inscription_versee = forms.IntegerField(label=_("Frais d'inscription versés aujourd'hui"), required=False, min_value=0,
                                            widget=forms.NumberInput(attrs=MONTANT_ATTRS))
    versement = forms.IntegerField(label=_('Versé sur la scolarité'), required=False, min_value=0,
                                   widget=forms.NumberInput(attrs=MONTANT_ATTRS),
                                   help_text=_('Imputé automatiquement sur les tranches, dans l’ordre.'))
    mode = forms.ChoiceField(label=_('Mode de paiement'), choices=ModePaiement.choices, initial=ModePaiement.ESPECES,
                             widget=forms.RadioSelect)

    def __init__(self, *args, avec_paiements=True, **kwargs):
        super().__init__(*args, **kwargs)
        if not avec_paiements:
            for name, _motif in self.PAIEMENTS_INITIAUX:
                del self.fields[name]
            del self.fields['mode']

    def clean(self):
        data = super().clean()
        montant = data.get('montant_scolarite')
        if montant is not None and (data.get('versement') or 0) > montant:
            raise forms.ValidationError(_('Le versement dépasse le montant de la scolarité.'))
        du = data.get('frais_inscription') or 0
        if du and (data.get('inscription_versee') or 0) > du:
            self.add_error('inscription_versee', _("Plus que les frais d'inscription dus (%(du)s).") % {'du': fcfa(du)})
        return data

    def paiements_initiaux(self):
        """Liste des (motif, montant) non nuls saisis dans le formulaire."""
        return [
            (motif, self.cleaned_data.get(name))
            for name, motif in self.PAIEMENTS_INITIAUX
            if self.cleaned_data.get(name)
        ]


class PaiementForm(forms.ModelForm):
    class Meta:
        model = Paiement
        fields = ['motif', 'montant', 'mode', 'date', 'reference', 'observation']
        widgets = {
            'motif': forms.RadioSelect,
            'mode': forms.RadioSelect,
            'montant': forms.NumberInput(attrs={**MONTANT_ATTRS, 'min': 1, 'autofocus': True}),
            'date': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
        }

    def __init__(self, *args, eleve, **kwargs):
        self.eleve = eleve
        super().__init__(*args, **kwargs)
        self.fields['motif'].choices = [(Motif.SCOLARITE, Motif.SCOLARITE.label), (Motif.INSCRIPTION, Motif.INSCRIPTION.label)]
        self.fields['mode'].choices = [c for c in self.fields['mode'].choices if c[0]]
        if not self.is_bound and eleve.inscription_reste:
            # Frais d'inscription pas encore réglés : proposés en premier.
            self.initial.setdefault('motif', Motif.INSCRIPTION)
            self.initial.setdefault('montant', eleve.inscription_reste)
        if not self.is_bound:
            self.initial.setdefault('motif', Motif.SCOLARITE if eleve.reste_a_payer else Motif.INSCRIPTION)
            prochaine = eleve.situation.prochaine
            retard = eleve.situation.montant_en_retard
            suggere = retard + (prochaine.reste if prochaine and not retard else 0) or eleve.reste_a_payer
            if suggere:
                self.initial.setdefault('montant', suggere)

    def clean_date(self):
        date = self.cleaned_data['date']
        if date > timezone.localdate():
            raise forms.ValidationError(_('La date ne peut pas être dans le futur.'))
        return date

    def clean(self):
        data = super().clean()
        if data.get('motif') in TRANCHES and data.get('montant'):
            reste = self.eleve.reste_a_payer
            if data['montant'] > reste:
                self.add_error('montant', _('Le reste à payer sur la scolarité n’est que de %(reste)s.') % {'reste': fcfa(reste)})
        if data.get('motif') == Motif.INSCRIPTION and data.get('montant') and self.eleve.frais_inscription:
            reste = self.eleve.inscription_reste
            if data['montant'] > reste:
                self.add_error('montant', _("Il ne reste que %(reste)s à payer sur les frais d'inscription.") % {
                    'reste': fcfa(reste)} if reste else _("Les frais d'inscription sont déjà entièrement payés."))
        return data


class ParametresForm(forms.ModelForm):
    class Meta:
        model = Parametres
        fields = ['annee_scolaire', 'message_relance']
        widgets = {'message_relance': forms.Textarea(attrs={'rows': 4})}


class CodesModulesForm(forms.Form):
    """Codes personnels d'un utilisateur : pour chaque module de son rôle, « nouveau code » et « retirer »."""

    def __init__(self, *args, utilisateur, **kwargs):
        super().__init__(*args, **kwargs)
        self.utilisateur = utilisateur
        self.proteges = verrou.modules_proteges(utilisateur)
        self.lignes = []
        for m in MODULES + [ADMINISTRATION]:
            if not accessible(utilisateur, m.code) or bientot(m.code):
                continue
            protege = m.code in self.proteges
            self.fields[f'code_{m.code}'] = forms.CharField(
                label=m.nom, required=False, min_length=4, max_length=20,
                widget=forms.PasswordInput(attrs={
                    'autocomplete': 'new-password',
                    'placeholder': _('Nouveau code') if protege else _('Aucun code'),
                }),
            )
            if protege:
                self.fields[f'retirer_{m.code}'] = forms.BooleanField(label=_('Retirer le code'), required=False)
            self.lignes.append((m, protege, self[f'code_{m.code}'], self[f'retirer_{m.code}'] if protege else None))

    def enregistrer(self):
        for m, _protege, _champ, _retirer in self.lignes:
            if self.cleaned_data.get(f'retirer_{m.code}'):
                verrou.retirer_code(self.utilisateur, m.code)
            elif self.cleaned_data.get(f'code_{m.code}'):
                verrou.definir_code(self.utilisateur, m.code, self.cleaned_data[f'code_{m.code}'])


ROLE_CHOICES = [(r, roles.LIBELLES[r]) for r in roles.ROLES]
ROLE_PERSONNEL_CHOICES = [(r, roles.LIBELLES[r]) for r in roles.ROLES_PERSONNEL]


class UtilisateurCreationForm(UserCreationForm):
    first_name = forms.CharField(label=_('Prénom'), max_length=150)
    last_name = forms.CharField(label=_('Nom'), max_length=150)
    role = forms.ChoiceField(label=_('Rôle'), choices=ROLE_PERSONNEL_CHOICES, widget=forms.RadioSelect)

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ['username', 'first_name', 'last_name']

    def save(self, commit=True):
        user = super().save(commit=commit)
        if commit:
            roles.definir_role(user, self.cleaned_data['role'])
        return user


class UtilisateurModifForm(forms.ModelForm):
    role = forms.ChoiceField(label=_('Rôle'), choices=ROLE_CHOICES, widget=forms.RadioSelect)
    nouveau_mot_de_passe = forms.CharField(
        label=_('Nouveau mot de passe'), required=False, widget=forms.PasswordInput(render_value=False),
        help_text=_('Laisser vide pour ne pas changer le mot de passe.'),
    )

    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'is_active']
        labels = {'first_name': _('Prénom'), 'last_name': _('Nom'), 'is_active': _('Compte actif')}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial.setdefault('role', roles.role_de(self.instance))

    def clean_nouveau_mot_de_passe(self):
        mdp = self.cleaned_data['nouveau_mot_de_passe']
        if mdp:
            validate_password(mdp, self.instance)
        return mdp

    def save(self, commit=True):
        user = super().save(commit=False)
        if self.cleaned_data['nouveau_mot_de_passe']:
            user.set_password(self.cleaned_data['nouveau_mot_de_passe'])
        if commit:
            user.save()
            roles.definir_role(user, self.cleaned_data['role'])
        return user


# --------------------------------------------------------------------------- Paramétrage (Administration)

class EtablissementForm(forms.ModelForm):
    class Meta:
        model = Etablissement
        fields = ['nom', 'adresse', 'telephone', 'email', 'decoupage', 'ifu', 'rccm',
                  'reduction_fratrie_2', 'reduction_fratrie_3', 'reduction_inscription_2', 'reduction_inscription_3',
                  'emecef_actif', 'emecef_mode', 'emecef_jeton',
                  'groupe_taxe']
        widgets = {
            'decoupage': forms.RadioSelect, 'emecef_mode': forms.RadioSelect, 'groupe_taxe': forms.RadioSelect,
            'ifu': forms.TextInput(attrs={'inputmode': 'numeric', 'maxlength': 13}),
            'emecef_jeton': forms.PasswordInput(render_value=False, attrs={'autocomplete': 'off'}),
            'reduction_fratrie_2': forms.NumberInput(attrs={'min': 0, 'max': 100}),
            'reduction_fratrie_3': forms.NumberInput(attrs={'min': 0, 'max': 100}),
            'reduction_inscription_2': forms.NumberInput(attrs={'min': 0, 'max': 100}),
            'reduction_inscription_3': forms.NumberInput(attrs={'min': 0, 'max': 100}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['emecef_jeton'].required = False
        if self.instance.emecef_jeton:
            self.fields['emecef_jeton'].help_text = _('Un jeton est enregistré. Laissez vide pour le conserver.')

    def clean_nom(self):
        nom = ' '.join(self.cleaned_data['nom'].split())
        if Etablissement.objects.filter(nom__iexact=nom).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError(_('Un établissement « %(nom)s » existe déjà.') % {'nom': nom})
        return nom

    def clean_emecef_jeton(self):
        return self.cleaned_data.get('emecef_jeton') or self.instance.emecef_jeton

    def clean(self):
        data = super().clean()
        if data.get('emecef_actif') and not (data.get('ifu') and data.get('emecef_jeton')):
            raise forms.ValidationError(_('Pour normaliser les factures, renseignez l’IFU et le jeton e-MECeF.'))
        return data


class GrilleForm(forms.ModelForm):
    class Meta:
        model = GrilleFrais
        fields = ['etablissement', 'classe', 'frais_inscription']
        widgets = {
            'classe': forms.TextInput(attrs={'list': 'classes-existantes', 'placeholder': _('Ex. CM2, 6ème A')}),
            'frais_inscription': forms.NumberInput(attrs=MONTANT_ATTRS),
        }

    def clean_classe(self):
        return ' '.join(self.cleaned_data['classe'].split())


class TrancheForm(forms.ModelForm):
    class Meta:
        model = TrancheGrille
        fields = ['libelle', 'montant', 'echeance']
        widgets = {
            'libelle': forms.TextInput(attrs={'placeholder': _('Ex. 1ʳᵉ tranche')}),
            'montant': forms.NumberInput(attrs={**MONTANT_ATTRS, 'min': 1}),
            'echeance': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
        }


class _TranchesBase(forms.BaseInlineFormSet):
    def clean(self):
        super().clean()
        restantes = [f for f in self.forms if f.cleaned_data and not f.cleaned_data.get('DELETE')]
        if not restantes:
            raise forms.ValidationError(_('Ajoutez au moins une tranche.'))
        dates = [f.cleaned_data['echeance'] for f in restantes if f.cleaned_data.get('echeance')]
        if dates != sorted(dates):
            raise forms.ValidationError(_('Les échéances doivent être dans l’ordre chronologique.'))


TranchesFormSet = inlineformset_factory(GrilleFrais, TrancheGrille, form=TrancheForm, formset=_TranchesBase,
                                        extra=0, can_delete=True, min_num=1, validate_min=False)


class EcheanceForm(forms.ModelForm):
    class Meta:
        model = Echeance
        fields = ['libelle', 'montant_brut', 'echeance']
        widgets = {
            'montant_brut': forms.NumberInput(attrs={**MONTANT_ATTRS, 'min': 1}),
            'echeance': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
        }
        labels = {'montant_brut': _('Montant')}


class _EcheancesBase(forms.BaseModelFormSet):
    def clean(self):
        super().clean()
        restantes = [f for f in self.forms if f.cleaned_data and not f.cleaned_data.get('DELETE')]
        if not restantes:
            raise forms.ValidationError(_('Gardez au moins une échéance.'))


EcheancesFormSet = modelformset_factory(Echeance, form=EcheanceForm, formset=_EcheancesBase, extra=0, can_delete=True)


class ReductionForm(forms.ModelForm):
    class Meta:
        model = ReductionEleve
        fields = ['porte_sur', 'type', 'mode', 'valeur', 'motif']
        widgets = {'porte_sur': forms.RadioSelect, 'type': forms.RadioSelect, 'mode': forms.RadioSelect,
                   'valeur': forms.NumberInput(attrs={'min': 1, 'inputmode': 'numeric'})}

    def clean(self):
        data = super().clean()
        if data.get('mode') == 'pourcentage' and (data.get('valeur') or 0) > 100:
            self.add_error('valeur', _('Un pourcentage ne peut pas dépasser 100.'))
        return data


class ContactParentForm(forms.ModelForm):
    class Meta:
        model = ContactParent
        fields = ['nom', 'nature', 'telephone', 'whatsapp', 'email', 'notifier']
        widgets = {'telephone': forms.TextInput(attrs={'type': 'tel'}), 'whatsapp': forms.TextInput(attrs={'type': 'tel'})}

    def clean(self):
        data = super().clean()
        if not (data.get('telephone') or data.get('whatsapp') or data.get('email')):
            raise forms.ValidationError(_('Indiquez au moins un téléphone ou un e-mail.'))
        return data


class RegleRelanceForm(forms.ModelForm):
    class Meta:
        model = RegleRelance
        fields = ['etablissement', 'moment', 'jours', 'repeter_tous_les', 'whatsapp', 'sms', 'email', 'message', 'actif']
        widgets = {'moment': forms.RadioSelect, 'message': forms.Textarea(attrs={'rows': 4})}

    def clean(self):
        data = super().clean()
        if not any(data.get(c) for c in ('whatsapp', 'sms', 'email')):
            raise forms.ValidationError(_('Choisissez au moins un canal.'))
        return data


# --------------------------------------------------------------------------- Corrections de paiement

class CorrectionForm(forms.Form):
    action = forms.ChoiceField(label=_('Correction demandée'), choices=ActionCorrection.choices,
                               initial=ActionCorrection.MODIFICATION, widget=forms.RadioSelect)
    montant = forms.IntegerField(label=_('Montant'), min_value=1, required=False,
                                 widget=forms.NumberInput(attrs={**MONTANT_ATTRS, 'min': 1}))
    mode = forms.ChoiceField(label=_('Mode de paiement'), choices=ModePaiement.choices, widget=forms.RadioSelect,
                             required=False)
    date = forms.DateField(label=_('Date du paiement'), required=False,
                           widget=forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'))
    reference = forms.CharField(label=_('Référence'), max_length=80, required=False)
    observation = forms.CharField(label=_('Observation'), max_length=200, required=False)
    motif = forms.CharField(label=_('Motif de la correction'), widget=forms.Textarea(attrs={'rows': 3}),
                            help_text=_('Obligatoire : il est conservé dans l’historique du paiement.'))

    def __init__(self, *args, paiement, avec_action=True, **kwargs):
        self.paiement = paiement
        super().__init__(*args, **kwargs)
        if not avec_action:
            del self.fields['action']
        if not self.is_bound:
            for champ in ('montant', 'mode', 'date', 'reference', 'observation'):
                self.initial.setdefault(champ, getattr(paiement, champ))

    def clean_date(self):
        date = self.cleaned_data.get('date')
        if date and date > timezone.localdate():
            raise forms.ValidationError(_('La date ne peut pas être dans le futur.'))
        return date

    def clean(self):
        data = super().clean()
        if data.get('action', ActionCorrection.MODIFICATION) == ActionCorrection.ANNULATION:
            return data
        for champ in ('montant', 'mode', 'date'):
            if not data.get(champ):
                self.add_error(champ, _('Ce champ est obligatoire.'))
        p = self.paiement
        if data.get('montant') and p.motif in TRANCHES:
            plafond = p.eleve.reste_a_payer + p.montant
            if data['montant'] > plafond:
                self.add_error('montant', _('Au plus %(plafond)s pour cet élève.') % {'plafond': fcfa(plafond)})
        if not self.errors and all(data.get(c) == getattr(p, c) for c in ('montant', 'mode', 'date', 'reference', 'observation')):
            raise forms.ValidationError(_('Aucune valeur n’a changé.'))
        return data

    def valeurs(self):
        return {c: self.cleaned_data[c] for c in ('montant', 'mode', 'date', 'reference', 'observation')}

    def valeurs_json(self):
        v = self.valeurs()
        v['date'] = v['date'].isoformat()
        return v


class MotifForm(forms.Form):
    motif = forms.CharField(label=_('Motif'), widget=forms.Textarea(attrs={'rows': 3}))


# --------------------------------------------------------------------------- Import, accès parent, surveillant

class ImportForm(forms.Form):
    etablissement = forms.CharField(
        label=_('Établissement des élèves'), max_length=150, required=False,
        widget=forms.TextInput(attrs={'list': 'etablissements-existants', 'placeholder': _('Ex. CS Romaric')}),
        help_text=_('Utilisé pour les lignes sans colonne « Établissement ». Un nouveau nom crée l’établissement.'))
    fichier = forms.FileField(label=_('Fichier Excel (.xlsx) ou CSV'),
                              widget=forms.ClearableFileInput(attrs={'accept': '.xlsx,.csv'}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if Etablissement.objects.count() == 1:
            self.initial.setdefault('etablissement', Etablissement.objects.get().nom)

    def clean_fichier(self):
        f = self.cleaned_data['fichier']
        if not f.name.lower().endswith(('.xlsx', '.csv')):
            raise forms.ValidationError(_('Format accepté : .xlsx ou .csv.'))
        if f.size > 5 * 1024 * 1024:
            raise forms.ValidationError(_('Fichier trop volumineux (5 Mo au maximum).'))
        return f


class AccesParentForm(forms.Form):
    identifiant = forms.CharField(label=_('Identifiant'), max_length=150)
    mot_de_passe = forms.CharField(label=_('Mot de passe'), widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}),
                                   help_text=_('À communiquer au parent ; il pourra se connecter sur la même adresse.'))

    def clean_identifiant(self):
        identifiant = self.cleaned_data['identifiant'].strip()
        if User.objects.filter(username__iexact=identifiant).exists():
            raise forms.ValidationError(_('Cet identifiant est déjà utilisé.'))
        return identifiant

    def clean_mot_de_passe(self):
        mdp = self.cleaned_data['mot_de_passe']
        validate_password(mdp)
        return mdp


class PerimetreForm(forms.Form):
    classes = forms.MultipleChoiceField(label=_('Classes surveillées'), required=False,
                                        widget=forms.CheckboxSelectMultiple,
                                        help_text=_('Aucune case cochée : toutes les classes.'))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        couples = (Eleve.objects.actifs().values_list('etablissement_id', 'etablissement__nom', 'classe')
                   .order_by('etablissement__nom', 'classe').distinct())
        plusieurs = len({c[0] for c in couples}) > 1
        self.fields['classes'].choices = [
            (f'{e}|{c}', f'{c} — {nom}' if plusieurs else c) for e, nom, c in couples
        ]
