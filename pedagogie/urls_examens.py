from django.urls import path

from .views import examens

app_name = 'examens'

urlpatterns = [
    path('', examens.liste, name='liste'),
    path('nouvelle/', examens.nouvelle, name='nouvelle'),
    path('<int:pk>/', examens.detail, name='detail'),
    path('<int:pk>/modifier/', examens.modifier, name='modifier'),
    path('<int:pk>/supprimer/', examens.supprimer, name='supprimer'),
    path('epreuves/<int:pk>/supprimer/', examens.epreuve_supprimer, name='epreuve_supprimer'),
]
