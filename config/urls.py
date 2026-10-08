from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from ecole.views.connexion import ConnexionView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('langue/', include('django.conf.urls.i18n')),
    path('connexion/', ConnexionView.as_view(), name='login'),
    path('deconnexion/', auth_views.LogoutView.as_view(), name='logout'),
    path('parents/', include('ecole.urls_parents')),
    path('webhooks/', include('ecole.urls_webhooks')),
    path('notes/', include('pedagogie.urls_notes')),
    path('professeurs/', include('pedagogie.urls_profs')),
    path('examens/', include('pedagogie.urls_examens')),
    path('', include('ecole.urls')),
]
