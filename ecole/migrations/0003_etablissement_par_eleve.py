import django.db.models.deletion
from django.db import migrations, models

MESSAGE_DEFAUT = (
    "Bonjour {parent}, sauf erreur de notre part, il reste {reste} à régler "
    "pour la scolarité de {eleve} ({classe}). Merci de passer à la caisse. — {ecole}"
)


def vers_parametres(apps, schema_editor):
    """Déplace l'année scolaire et le message de relance vers Paramètres, et nomme l'établissement existant."""
    Etablissement = apps.get_model('ecole', 'Etablissement')
    Parametres = apps.get_model('ecole', 'Parametres')
    ancien = Etablissement.objects.filter(pk=1).first()
    Parametres.objects.update_or_create(pk=1, defaults={
        'annee_scolaire': ancien.annee_scolaire if ancien else '2026-2027',
        'message_relance': ancien.message_relance if ancien else MESSAGE_DEFAUT,
    })
    if ancien and not ancien.nom.strip():
        ancien.nom = 'Mon établissement'
        ancien.save(update_fields=['nom'])


def rattacher_eleves(apps, schema_editor):
    """Les élèves déjà inscrits sont rattachés à l'établissement existant."""
    Eleve = apps.get_model('ecole', 'Eleve')
    Etablissement = apps.get_model('ecole', 'Etablissement')
    if Eleve.objects.filter(etablissement__isnull=True).exists():
        etab = Etablissement.objects.order_by('pk').first() or Etablissement.objects.create(nom='Mon établissement')
        Eleve.objects.filter(etablissement__isnull=True).update(etablissement=etab)


class Migration(migrations.Migration):

    dependencies = [
        ('ecole', '0002_paiements_roles'),
    ]

    operations = [
        migrations.CreateModel(
            name='Parametres',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('annee_scolaire', models.CharField(default='2026-2027', max_length=20, verbose_name='année scolaire')),
                ('message_relance', models.TextField(default=MESSAGE_DEFAUT, help_text='Variables disponibles : {parent}, {eleve}, {classe}, {reste}, {ecole}.', verbose_name='message de relance')),
            ],
            options={'verbose_name': 'paramètres', 'verbose_name_plural': 'paramètres'},
        ),
        migrations.RunPython(vers_parametres, migrations.RunPython.noop),
        migrations.RemoveField(model_name='etablissement', name='annee_scolaire'),
        migrations.RemoveField(model_name='etablissement', name='message_relance'),
        migrations.AlterField(
            model_name='etablissement',
            name='nom',
            field=models.CharField(max_length=150, unique=True, verbose_name="nom de l'établissement"),
        ),
        migrations.AlterModelOptions(
            name='etablissement',
            options={'ordering': ['nom'], 'verbose_name': 'établissement'},
        ),
        migrations.AddField(
            model_name='eleve',
            name='etablissement',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, related_name='eleves', to='ecole.etablissement', verbose_name='établissement'),
        ),
        migrations.RunPython(rattacher_eleves, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='eleve',
            name='etablissement',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='eleves', to='ecole.etablissement', verbose_name='établissement'),
        ),
        migrations.AlterModelOptions(
            name='eleve',
            options={'ordering': ['etablissement__nom', 'classe', 'nom', 'prenom'], 'permissions': [('exporter_donnees', 'Peut exporter les données'), ('gerer_utilisateurs', 'Peut gérer les utilisateurs et les paramètres')], 'verbose_name': 'élève'},
        ),
    ]
