from django.contrib import admin

from .models import Composition, Creneau, Enseignement, Epreuve, FicheNote, HeureCours, Matiere, PaiementProfesseur, Professeur


@admin.register(Matiere)
class MatiereAdmin(admin.ModelAdmin):
    list_display = ['nom', 'abreviation']
    search_fields = ['nom']


@admin.register(Professeur)
class ProfesseurAdmin(admin.ModelAdmin):
    list_display = ['nom', 'prenoms', 'telephone', 'statut', 'taux_horaire', 'actif', 'utilisateur']
    list_filter = ['statut', 'actif']
    search_fields = ['nom', 'prenoms', 'telephone']
    filter_horizontal = ['matieres']


@admin.register(Enseignement)
class EnseignementAdmin(admin.ModelAdmin):
    list_display = ['classe', 'matiere', 'coefficient', 'professeur', 'etablissement']
    list_filter = ['etablissement', 'classe']


@admin.register(FicheNote)
class FicheNoteAdmin(admin.ModelAdmin):
    list_display = ['eleve', 'enseignement', 'periode', 'i1', 'i2', 'i3', 'i4', 'd1', 'd2']
    list_filter = ['periode', 'enseignement__classe']


class EpreuveInline(admin.TabularInline):
    model = Epreuve
    extra = 0


@admin.register(Composition)
class CompositionAdmin(admin.ModelAdmin):
    list_display = ['titre', 'etablissement', 'periode', 'colonne', 'date_debut', 'date_fin']
    inlines = [EpreuveInline]


admin.site.register([Creneau, HeureCours, PaiementProfesseur])
