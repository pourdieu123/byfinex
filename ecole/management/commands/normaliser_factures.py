"""Relance la normalisation e-MECeF des factures restées en attente ou en erreur."""

from django.core.management.base import BaseCommand

from ecole.facturation import normaliser_facture
from ecole.models import Facture, StatutFacture


class Command(BaseCommand):
    help = "Normalise (e-MECeF) les factures pas encore normalisées des établissements où c'est activé."

    def handle(self, *args, **options):
        ok = ko = 0
        for f in (Facture.objects.exclude(statut=StatutFacture.NORMALISEE)
                  .filter(eleve__etablissement__emecef_actif=True).order_by('date')):
            normaliser_facture(f)
            if f.statut == StatutFacture.NORMALISEE:
                ok += 1
            else:
                ko += 1
                self.stderr.write(f'{f.numero} : {f.erreur}')
        self.stdout.write(self.style.SUCCESS(f'{ok} facture(s) normalisée(s), {ko} en échec.'))
