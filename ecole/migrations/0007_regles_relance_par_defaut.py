from django.db import migrations

REGLES = [
    ('avant', 7, 0, "Bonjour {parent}, la {tranche} de la scolarité de {eleve} ({classe}), soit {montant}, "
                    "est attendue le {echeance}. Merci. — {ecole}"),
    ('jour', 0, 0, "Bonjour {parent}, c'est aujourd'hui l'échéance de la {tranche} de {eleve} ({classe}) : "
                   "{montant}. Merci de passer à la caisse. — {ecole}"),
    ('apres', 3, 7, "Bonjour {parent}, sauf erreur de notre part, la {tranche} de {eleve} ({classe}) est en retard "
                    "de {retard} jour(s) : {montant} restent à régler (total restant : {reste}). — {ecole}"),
]


def creer(apps, schema_editor):
    RegleRelance = apps.get_model('ecole', 'RegleRelance')
    if RegleRelance.objects.exists():
        return
    for moment, jours, repeter, message in REGLES:
        RegleRelance.objects.create(moment=moment, jours=jours, repeter_tous_les=repeter, message=message,
                                    whatsapp=True, sms=moment == 'apres', email=True, actif=True)


class Migration(migrations.Migration):
    dependencies = [('ecole', '0006_scolarite_complete')]
    operations = [migrations.RunPython(creer, migrations.RunPython.noop)]
