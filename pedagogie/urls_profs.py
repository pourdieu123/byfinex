from django.urls import path

from .views import professeurs

app_name = 'profs'

urlpatterns = [
    path('', professeurs.liste, name='liste'),
    path('nouveau/', professeurs.nouveau, name='nouveau'),
    path('<int:pk>/', professeurs.detail, name='detail'),
    path('<int:pk>/modifier/', professeurs.modifier, name='modifier'),
    path('<int:pk>/heures/', professeurs.heure_ajouter, name='heure_ajouter'),
    path('heures/<int:pk>/supprimer/', professeurs.heure_supprimer, name='heure_supprimer'),
    path('<int:pk>/paiements/', professeurs.paiement_ajouter, name='paiement_ajouter'),
    path('paiements/<int:pk>/bon.pdf', professeurs.bon_pdf, name='bon_pdf'),
    path('<int:pk>/compte/', professeurs.compte_creer, name='compte_creer'),
    path('emploi-du-temps/', professeurs.emploi_du_temps, name='emploi_du_temps'),
    path('creneaux/<int:pk>/supprimer/', professeurs.creneau_supprimer, name='creneau_supprimer'),
]
