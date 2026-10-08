from django.urls import path

from .views import webhooks

app_name = 'webhooks'

urlpatterns = [
    path('whatsapp/', webhooks.whatsapp, name='whatsapp'),
    path('sms/', webhooks.sms, name='sms'),
]
