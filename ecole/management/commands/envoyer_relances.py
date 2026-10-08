r"""Envoie les rappels d'échéance et les relances de retard prévus aujourd'hui.

À lancer une fois par jour (Planificateur de tâches Windows) :
    .venv\Scripts\python manage.py envoyer_relances
"""

from datetime import date

from django.core.management.base import BaseCommand

from ecole.notifications import executer_relances


class Command(BaseCommand):
    help = "Envoie les rappels et relances du jour selon les règles de relance."

    def add_arguments(self, parser):
        parser.add_argument('--date', help='Date à simuler (AAAA-MM-JJ), pour tester.')

    def handle(self, *args, **options):
        jour = date.fromisoformat(options['date']) if options.get('date') else None
        n = executer_relances(jour)
        self.stdout.write(self.style.SUCCESS(f'{n} message(s) envoyé(s).'))
