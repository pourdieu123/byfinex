"""Relances par SMS uniquement, avec des messages courts (1 SMS = 160 caractères).

Seules les règles dont le message n'a pas été modifié depuis leur création (migration 0007) sont changées.
"""
import importlib

from django.db import migrations

ANCIENS = {moment: message for moment, _j, _r, message in
           importlib.import_module('ecole.migrations.0007_regles_relance_par_defaut').REGLES}
COURTS = {
    'avant': "{ecole} : rappel, la {tranche} de {eleve} ({classe}), soit {montant}, est attendue le {echeance}. Merci.",
    'jour': "{ecole} : la {tranche} de {eleve} ({classe}), soit {montant}, est attendue aujourd'hui. Merci.",
    'apres': "{ecole} : {tranche} de {eleve} ({classe}) en retard de {retard} j. Reste {montant}, "
             "total {reste}. Merci de régulariser.",
}


def passer_en_sms(apps, schema_editor):
    RegleRelance = apps.get_model('ecole', 'RegleRelance')
    for regle in RegleRelance.objects.all():
        if regle.message == ANCIENS.get(regle.moment):
            regle.message = COURTS[regle.moment]
            regle.whatsapp, regle.sms, regle.email = False, True, False
            regle.save(update_fields=['message', 'whatsapp', 'sms', 'email'])


def revenir(apps, schema_editor):
    RegleRelance = apps.get_model('ecole', 'RegleRelance')
    for regle in RegleRelance.objects.all():
        if regle.message == COURTS.get(regle.moment):
            regle.message = ANCIENS[regle.moment]
            regle.whatsapp, regle.sms, regle.email = True, regle.moment == 'apres', True
            regle.save(update_fields=['message', 'whatsapp', 'sms', 'email'])


class Migration(migrations.Migration):
    dependencies = [('ecole', '0013_trace_imports')]
    operations = [migrations.RunPython(passer_en_sms, revenir)]
