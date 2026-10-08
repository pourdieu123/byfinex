from django.apps import AppConfig
from django.db.models.signals import post_migrate


class PedagogieConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'pedagogie'
    verbose_name = 'Pédagogie'

    def ready(self):
        from ecole.roles import synchroniser_groupes

        # Les permissions de cette application n'existent qu'après sa propre migration.
        post_migrate.connect(synchroniser_groupes, sender=self)
