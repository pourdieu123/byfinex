from django import forms
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.utils.translation import gettext_lazy as _

from ecole.models import Etablissement

from .models import (
    Composition, Creneau, Enseignement, Epreuve, HeureCours, Matiere, PaiementProfesseur, Professeur,
)

HEURE = forms.TimeInput(attrs={'type': 'time'}, format='%H:%M')
DATE = forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d')


def sans_vide(champ):
    champ.choices = [c for c in champ.choices if c[0] not in ('', None)]


class MatiereForm(forms.ModelForm):
    class Meta:
        model = Matiere
        fields = ['nom', 'abreviation']
        widgets = {'nom': forms.TextInput(attrs={'placeholder': _('Ex. Mathématiques')}),
                   'abreviation': forms.TextInput(attrs={'placeholder': 'Ex. MATH'})}


class EnseignementForm(forms.ModelForm):
    class Meta:
        model = Enseignement
        fields = ['matiere', 'coefficient', 'professeur']
        widgets = {'coefficient': forms.NumberInput(attrs={'min': 1, 'max': 10})}

    def __init__(self, *args, etablissement=None, classe=None, **kwargs):
        self.etablissement, self.classe = etablissement, classe
        super().__init__(*args, **kwargs)
        self.fields['professeur'].queryset = Professeur.objects.filter(actif=True)
        self.fields['professeur'].empty_label = _('— Pas encore attribué —')
        self.fields['matiere'].empty_label = _('— Choisir la matière —')

    def clean_matiere(self):
        matiere = self.cleaned_data['matiere']
        doublon = Enseignement.objects.filter(etablissement=self.etablissement, classe=self.classe, matiere=matiere)
        if self.instance.pk:
            doublon = doublon.exclude(pk=self.instance.pk)
        if doublon.exists():
            raise forms.ValidationError(_('%(matiere)s est déjà enseignée en %(classe)s.') % {'matiere': matiere, 'classe': self.classe})
        return matiere


class ProfesseurForm(forms.ModelForm):
    class Meta:
        model = Professeur
        fields = ['nom', 'prenoms', 'telephone', 'email', 'statut', 'taux_horaire', 'matieres', 'actif']
        widgets = {
            'statut': forms.RadioSelect,
            'matieres': forms.CheckboxSelectMultiple,
            'telephone': forms.TextInput(attrs={'type': 'tel', 'inputmode': 'tel'}),
            'taux_horaire': forms.NumberInput(attrs={'inputmode': 'numeric', 'min': 0}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        sans_vide(self.fields['statut'])


class CompteProfesseurForm(forms.Form):
    username = forms.CharField(label=_('Identifiant de connexion'), max_length=150)
    mot_de_passe = forms.CharField(label=_('Mot de passe'), widget=forms.PasswordInput)

    def clean_username(self):
        username = self.cleaned_data['username'].strip()
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError(_('Cet identifiant est déjà utilisé.'))
        return username

    def clean_mot_de_passe(self):
        mdp = self.cleaned_data['mot_de_passe']
        validate_password(mdp)
        return mdp


class CreneauForm(forms.ModelForm):
    class Meta:
        model = Creneau
        fields = ['enseignement', 'jour', 'heure_debut', 'heure_fin', 'salle']
        widgets = {'heure_debut': HEURE, 'heure_fin': HEURE, 'salle': forms.TextInput(attrs={'placeholder': _('Ex. Salle 3')})}

    def __init__(self, *args, enseignements=None, **kwargs):
        super().__init__(*args, **kwargs)
        if enseignements is not None:
            self.fields['enseignement'].queryset = enseignements
        self.fields['enseignement'].label_from_instance = (
            lambda e: f'{e.matiere} — {e.professeur or _("sans professeur")}'
        )
        self.fields['enseignement'].empty_label = _('— Choisir la matière —')
        sans_vide(self.fields['jour'])


class HeureCoursForm(forms.ModelForm):
    class Meta:
        model = HeureCours
        fields = ['date', 'duree', 'enseignement', 'observation']
        widgets = {'date': DATE, 'duree': forms.NumberInput(attrs={'step': '0.25', 'min': '0.25', 'placeholder': 'Ex. 2'})}

    def __init__(self, *args, professeur=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['enseignement'].queryset = professeur.enseignements.select_related('matiere') if professeur else Enseignement.objects.none()
        self.fields['enseignement'].required = False
        self.fields['enseignement'].empty_label = _('— Non précisé —')


class PaiementProfesseurForm(forms.ModelForm):
    class Meta:
        model = PaiementProfesseur
        fields = ['montant', 'date', 'mode', 'periode', 'reference']
        widgets = {'date': DATE, 'mode': forms.RadioSelect, 'montant': forms.NumberInput(attrs={'inputmode': 'numeric', 'min': 1})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        sans_vide(self.fields['mode'])


class CompositionForm(forms.ModelForm):
    class Meta:
        model = Composition
        fields = ['etablissement', 'titre', 'periode', 'colonne', 'date_debut', 'date_fin']
        widgets = {'date_debut': DATE, 'date_fin': DATE, 'colonne': forms.RadioSelect,
                   'periode': forms.Select(choices=[(1, _('1re période')), (2, _('2e période')), (3, _('3e période'))])}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['etablissement'].queryset = Etablissement.objects.all()
        self.fields['etablissement'].empty_label = None
        sans_vide(self.fields['colonne'])


class EpreuveForm(forms.ModelForm):
    class Meta:
        model = Epreuve
        fields = ['enseignement', 'date', 'heure_debut', 'heure_fin', 'salle', 'surveillants']
        widgets = {'date': DATE, 'heure_debut': HEURE, 'heure_fin': HEURE, 'surveillants': forms.CheckboxSelectMultiple,
                   'salle': forms.TextInput(attrs={'placeholder': _('Ex. Salle 3')})}

    def __init__(self, *args, composition, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.composition = composition
        self.fields['enseignement'].queryset = (
            Enseignement.objects.filter(etablissement=composition.etablissement).select_related('matiere')
            .order_by('classe', 'matiere__nom')
        )
        self.fields['enseignement'].label_from_instance = lambda e: f'{e.classe} — {e.matiere}'
        self.fields['enseignement'].empty_label = _('— Choisir la classe et la matière —')
        self.fields['surveillants'].queryset = Professeur.objects.filter(actif=True)
        if not self.is_bound and not self.instance.pk:
            self.initial.setdefault('date', composition.date_debut)

    def clean(self):
        data = super().clean()
        surveillants = data.get('surveillants') or []
        if surveillants and data.get('date') and data.get('heure_debut') and data.get('heure_fin'):
            self.instance.date, self.instance.heure_debut, self.instance.heure_fin = data['date'], data['heure_debut'], data['heure_fin']
            occupes = self.instance.conflits_surveillants(surveillants)
            if occupes:
                self.add_error('surveillants', _('Déjà en surveillance à ce moment : ') + ', '.join(map(str, occupes)))
        return data
