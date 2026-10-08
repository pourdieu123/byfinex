from django.apps import AppConfig
from django.db.models.signals import post_migrate


class EcoleConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'ecole'
    verbose_name = 'École'

    def ready(self):
        from .roles import synchroniser_groupes

        post_migrate.connect(synchroniser_groupes, sender=self)
