from django.contrib import admin
from django.db.models import Count

from .models import Eleve, Etablissement, Paiement, Parametres, Relance


class PaiementInline(admin.TabularInline):
    model = Paiement
    extra = 0
    fields = ['numero_recu', 'date', 'motif', 'montant', 'mode', 'reference', 'encaisse_par']
    readonly_fields = ['numero_recu']


@admin.register(Eleve)
class EleveAdmin(admin.ModelAdmin):
    list_display = ['nom', 'prenom', 'etablissement', 'classe', 'parent', 'telephone', 'montant_scolarite', 'reste_a_payer']
    list_filter = ['etablissement', 'classe', 'nature_parent']
    search_fields = ['nom', 'prenom', 'parent', 'educ_master', 'telephone']
    inlines = [PaiementInline]
    list_filter = ['archive', 'etablissement', 'classe', 'nature_parent']

    def has_delete_permission(self, request, obj=None):
        return False  # un élève s'archive (fiche › Archiver), il ne se supprime pas


@admin.register(Paiement)
class PaiementAdmin(admin.ModelAdmin):
    list_display = ['numero_recu', 'date', 'eleve', 'motif', 'montant', 'mode', 'encaisse_par']
    list_filter = ['motif', 'mode', 'date']
    search_fields = ['numero_recu', 'reference', 'eleve__nom', 'eleve__prenom']
    readonly_fields = ['numero_recu']


@admin.register(Relance)
class RelanceAdmin(admin.ModelAdmin):
    list_display = ['cree_le', 'eleve', 'canal', 'reste_a_payer', 'envoye_par']
    list_filter = ['canal']


@admin.register(Etablissement)
class EtablissementAdmin(admin.ModelAdmin):
    list_display = ['nom', 'adresse', 'telephone', 'nb_eleves']
    exclude = ['emecef_jeton']
    search_fields = ['nom']

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(nb=Count('eleves'))

    @admin.display(description='élèves', ordering='nb')
    def nb_eleves(self, obj):
        return obj.nb


admin.site.register(Parametres)


from .models import (  # noqa: E402
    CompteParent, ContactParent, DemandeCorrection, Echeance, Facture, GrilleFrais, HistoriquePaiement, Notification,
    PerimetreSurveillant, ReductionEleve, RegleRelance, TrancheGrille,
)


class TrancheInline(admin.TabularInline):
    model = TrancheGrille
    extra = 0


@admin.register(GrilleFrais)
class GrilleFraisAdmin(admin.ModelAdmin):
    list_display = ['classe', 'etablissement', 'frais_inscription', 'total']
    list_filter = ['etablissement']
    inlines = [TrancheInline]


@admin.register(Facture)
class FactureAdmin(admin.ModelAdmin):
    list_display = ['numero', 'type', 'date', 'eleve', 'montant', 'statut', 'code_mecef', 'annulee']
    list_filter = ['type', 'statut', 'annulee']
    search_fields = ['numero', 'code_mecef', 'eleve__nom', 'eleve__prenom']
    readonly_fields = [f.name for f in Facture._meta.fields]

    def has_delete_permission(self, request, obj=None):
        return False  # une facture ne se supprime pas : elle s'annule par un avoir


@admin.register(HistoriquePaiement)
class HistoriquePaiementAdmin(admin.ModelAdmin):
    list_display = ['cree_le', 'numero_recu', 'eleve', 'action', 'valide_par', 'motif']
    list_filter = ['action']
    readonly_fields = [f.name for f in HistoriquePaiement._meta.fields]


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ['cree_le', 'canal', 'type', 'destinataire', 'eleve', 'statut']
    list_filter = ['canal', 'type', 'statut']
    search_fields = ['destinataire', 'eleve__nom', 'message']


admin.site.register([Echeance, ReductionEleve, ContactParent, DemandeCorrection, RegleRelance, CompteParent,
                     PerimetreSurveillant])
