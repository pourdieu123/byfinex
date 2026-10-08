"""Codes d'accès personnels aux modules, en ligne de commande (utile si la direction a oublié le sien).

    python manage.py codes_modules                              # qui a un code pour quel module
    python manage.py codes_modules definir awa scolarite 4821   # définit (ou change) le code d'awa
    python manage.py codes_modules retirer awa admin            # retire un code (« tous » pour tous ses codes)
"""

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from ecole import verrou
from ecole.models import CodeModule
from ecole.modules import ADMINISTRATION, MODULES

CODES = [m.code for m in MODULES + [ADMINISTRATION]]


class Command(BaseCommand):
    help = "Liste, définit ou retire les codes d'accès personnels aux modules."

    def add_arguments(self, parser):
        parser.add_argument('action', nargs='?', choices=['liste', 'definir', 'retirer'], default='liste')
        parser.add_argument('utilisateur', nargs='?')
        parser.add_argument('module', nargs='?')
        parser.add_argument('code', nargs='?')

    def handle(self, action, utilisateur, module, code, **options):
        if action == 'liste':
            for user in User.objects.order_by('username'):
                modules = sorted(CodeModule.objects.filter(utilisateur=user).values_list('module', flat=True))
                self.stdout.write(f"{user.username:<20} {', '.join(modules) or 'aucun code'}")
            return
        user = User.objects.filter(username=utilisateur).first()
        if user is None:
            raise CommandError(f"Utilisateur inconnu : {utilisateur}")
        choix = CODES + (['tous'] if action == 'retirer' else [])
        if module not in choix:
            raise CommandError(f"Module inconnu. Choix : {', '.join(choix)}")
        if action == 'retirer':
            for c in (CODES if module == 'tous' else [module]):
                verrou.retirer_code(user, c)
            self.stdout.write(self.style.SUCCESS(f'Code retiré : {user.username} / {module}.'))
            return
        if not code or len(code) < 4:
            raise CommandError('Le code doit contenir au moins 4 caractères.')
        verrou.definir_code(user, module, code)
        self.stdout.write(self.style.SUCCESS(f'Code de {user.username} pour {module} enregistré.'))
