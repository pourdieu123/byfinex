from django.urls import path

from .views import notes

app_name = 'notes'

urlpatterns = [
    path('', notes.tableau, name='tableau'),
    path('matieres/', notes.matieres, name='matieres'),
    path('classe/', notes.classe_config, name='classe_config'),
    path('classe/copier/', notes.copier_config, name='copier_config'),
    path('enseignements/<int:pk>/retirer/', notes.enseignement_supprimer, name='enseignement_supprimer'),
    path('saisie/<int:pk>/', notes.saisie, name='saisie'),
    path('resultats/', notes.resultats, name='resultats'),
    path('bulletins.pdf', notes.bulletins_pdf, name='bulletins_pdf'),
]
