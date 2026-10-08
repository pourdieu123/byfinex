from django.urls import path

from .views import parents

app_name = 'parents'

urlpatterns = [
    path('', parents.accueil, name='accueil'),
    path('enfants/<int:pk>/', parents.enfant, name='enfant'),
    path('factures/<int:pk>.pdf', parents.facture_pdf, name='facture_pdf'),
]
