r"""Donne des tranches à tous les élèves et réaffecte tous les paiements de scolarité à ces tranches.

Sans danger : aucun paiement n'est modifié ni supprimé (montant, date, référence et motif d'origine
sont conservés) ; seuls l'échéancier (élèves sans tranches) et le détail des affectations sont recalculés.
    .venv\Scripts\python manage.py recalculer_tranches
"""

from django.core.management.base import BaseCommand
from django.db.models import Sum

from ecole.echeancier import appliquer_reductions, generer_echeancier
from ecole.models import Eleve, ImputationPaiement, Paiement


class Command(BaseCommand):
    help = "Crée les tranches manquantes et réaffecte les paiements de scolarité aux tranches."

    def handle(self, *args, **options):
        avant = Paiement.objects.aggregate(n=Sum('montant'))['n'] or 0
        crees = 0
        for eleve in Eleve.objects.all():
            if not eleve.echeances.exists():
                generer_echeancier(eleve)
                crees += eleve.echeances.exists()
            else:
                appliquer_reductions(eleve)
        apres = Paiement.objects.aggregate(n=Sum('montant'))['n'] or 0
        assert avant == apres, 'Les paiements ne doivent pas changer'
        self.stdout.write(self.style.SUCCESS(
            f'{crees} échéancier(s) créé(s) ; {ImputationPaiement.objects.count()} affectation(s) enregistrée(s) ; '
            f'total des paiements inchangé ({apres} FCFA).'))
