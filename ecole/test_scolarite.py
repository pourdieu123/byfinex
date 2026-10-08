"""Tests de la scolarité complète : grilles, échéancier, réductions, retards, factures, corrections,
relances, import, assistant, espaces parents et surveillants."""

import io
import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import roles
from .agent import repondre
from .echeancier import appliquer_reductions, generer_echeancier, imputation_paiement, situation
from .facturation import annuler_paiement, corriger_paiement, enregistrer_paiement
from .models import (
    CompteParent, DemandeCorrection, Eleve, Etablissement, Facture, GrilleFrais, HistoriquePaiement, ImputationPaiement, Motif,
    Notification, Paiement, PerimetreSurveillant, ReductionEleve, StatutDemande, StatutFacture, TrancheGrille,
    TypeFacture,
)
from .notifications import executer_relances

AUJOURDHUI = timezone.localdate()


def utilisateur(role, username=None):
    user = User.objects.create_user(username or role.lower(), password='Mot-de-passe-123')
    roles.definir_role(user, role)
    return user


def grille(etab, classe='CM2', montants=(30000, 20000, 10000), debut=-30, pas=30):
    g = GrilleFrais.objects.create(etablissement=etab, classe=classe, frais_inscription=5000)
    for i, m in enumerate(montants, start=1):
        TrancheGrille.objects.create(grille=g, numero=i, libelle=f'Tranche {i}', montant=m,
                                     echeance=AUJOURDHUI + timedelta(days=debut + pas * (i - 1)))
    return g


def eleve(etab, telephone='01 97 00 00 00', classe='CM2', **kw):
    data = dict(nom='Dossou', prenom='Koffi', parent='Dossou Marc', classe=classe, telephone=telephone,
                montant_scolarite=0, etablissement=etab, email='parent@exemple.com')
    data.update(kw)
    e = Eleve.objects.create(**data)
    generer_echeancier(e)
    return Eleve.objects.avec_totaux().get(pk=e.pk)


def payer(e, montant, user=None, motif=Motif.SCOLARITE, **kw):
    return enregistrer_paiement(Paiement(eleve=e, motif=motif, montant=montant, **kw), user)


class Base(TestCase):
    def setUp(self):
        self.etab = Etablissement.objects.create(nom='CEG Test', ifu='3201234567890')
        self.grille = grille(self.etab)


class EcheancierTests(Base):
    def test_echeancier_copie_de_la_grille(self):
        e = eleve(self.etab)
        self.assertEqual([x.montant for x in e.echeances.all()], [30000, 20000, 10000])
        self.assertEqual(e.montant_scolarite, 60000)

    def test_retard_puis_a_jour(self):
        e = eleve(self.etab)  # 1re tranche échue il y a 30 jours
        sit = situation(e)
        self.assertTrue(sit.en_retard)
        self.assertEqual((sit.statut, sit.montant_en_retard, sit.jours_de_retard), ('retard', 30000, 30))
        payer(e, 20000)
        e = Eleve.objects.avec_totaux().get(pk=e.pk)
        self.assertEqual(situation(e).montant_en_retard, 10000)  # paiement partiel accepté
        payer(e, 10000)
        e = Eleve.objects.avec_totaux().get(pk=e.pk)
        self.assertEqual(situation(e).statut, 'a_jour')

    def test_imputation_sur_deux_tranches(self):
        e = eleve(self.etab)
        payer(e, 25000)
        p = payer(Eleve.objects.avec_totaux().get(pk=e.pk), 15000).paiement
        lignes = imputation_paiement(p)
        self.assertEqual([m for _l, m in lignes], [5000, 10000])

    def test_fratrie_automatique(self):
        self.etab.reduction_fratrie_2 = 10
        self.etab.save()
        aine = eleve(self.etab)
        cadet = eleve(self.etab, prenom='Afi')
        self.assertEqual(aine.montant_scolarite, 60000)
        self.assertEqual(cadet.montant_scolarite, 54000)

    def test_bourse_repartie_sur_les_tranches_restantes(self):
        e = eleve(self.etab)
        payer(e, 30000)  # 1re tranche réglée
        ReductionEleve.objects.create(eleve=e, type='bourse', mode='montant', valeur=6000)
        appliquer_reductions(e)
        lignes = list(e.echeances.all())
        self.assertEqual([l.reduction for l in lignes], [0, 4000, 2000])  # tranche payée intacte, prorata
        self.assertEqual(Eleve.objects.get(pk=e.pk).montant_scolarite, 54000)


class FacturationTests(Base):
    def setUp(self):
        super().setUp()
        self.direction = utilisateur(roles.DIRECTION)
        self.e = eleve(self.etab)

    def test_facture_et_envoi_au_parent(self):
        f = payer(self.e, 30000, self.direction)
        self.assertEqual((f.type, f.montant, f.ifu, f.statut), (TypeFacture.VENTE, 30000, '3201234567890', StatutFacture.EN_ATTENTE))
        self.assertTrue(f.numero.startswith('F'))
        canaux = set(Notification.objects.filter(facture=f).values_list('canal', flat=True))
        self.assertEqual(canaux, {'whatsapp', 'sms', 'email'})

    def test_correction_avoir_et_trace(self):
        f = payer(self.e, 30000, self.direction)
        p = f.paiement
        nouvelle = corriger_paiement(p, {'montant': 25000}, 'Erreur de frappe', self.direction)
        f.refresh_from_db()
        self.assertTrue(f.annulee)
        avoir = Facture.objects.get(type=TypeFacture.AVOIR)
        self.assertEqual((avoir.facture_origine, avoir.montant), (f, 30000))
        self.assertEqual(nouvelle.montant, 25000)
        h = HistoriquePaiement.objects.get()
        self.assertEqual((h.avant['montant'], h.apres['montant'], h.motif), (30000, 25000, 'Erreur de frappe'))

    def test_annulation_garde_les_factures(self):
        f = payer(self.e, 30000, self.direction)
        annuler_paiement(f.paiement, 'Chèque sans provision', self.direction)
        self.assertFalse(Paiement.objects.exists())
        self.assertEqual(Facture.objects.count(), 2)
        self.assertEqual(HistoriquePaiement.objects.get().action, 'annulation')

    def test_emecef(self):
        self.etab.emecef_actif, self.etab.emecef_jeton = True, 'jeton'
        self.etab.save()
        reponses = [{'uid': 'abc'}, {'codeMECeFDGI': 'TEST-1234-5678-9ABC-DEF0-1234', 'qrCode': 'F;TEST;123',
                                     'counters': '1/1 FV', 'nim': 'TS01000001', 'dateTime': '07/10/2026 10:00:00'}]

        class Reponse(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch('urllib.request.urlopen', side_effect=[Reponse(json.dumps(r).encode()) for r in reponses]) as appel:
            f = payer(self.e, 30000, self.direction)
        self.assertEqual(f.statut, StatutFacture.NORMALISEE)
        self.assertEqual(f.code_mecef, 'TEST-1234-5678-9ABC-DEF0-1234')
        corps = json.loads(appel.call_args_list[0].args[0].data)
        self.assertEqual((corps['ifu'], corps['type'], corps['items'][0]['taxGroup']), ('3201234567890', 'FV', 'A'))
        from .pdf import pdf_facture
        self.assertTrue(pdf_facture(f).startswith(b'%PDF'))

    def test_emecef_injoignable(self):
        self.etab.emecef_actif, self.etab.emecef_jeton = True, 'jeton'
        self.etab.save()
        with mock.patch('urllib.request.urlopen', side_effect=OSError('hors ligne')):
            f = payer(self.e, 30000, self.direction)
        self.assertEqual(f.statut, StatutFacture.ERREUR)
        self.assertTrue(Paiement.objects.exists())  # la caisse n'est pas bloquée


@override_settings(VERROU_MODULES=False)
class VuesTests(Base):
    def setUp(self):
        super().setUp()
        self.direction = utilisateur(roles.DIRECTION)
        self.caisse = utilisateur(roles.CAISSIER, 'caisse')
        self.e = eleve(self.etab)

    def test_inscription_avec_grille_et_versement(self):
        self.client.force_login(self.direction)
        r = self.client.post(reverse('ecole:eleve_nouveau'), {
            'etablissement_nom': 'CEG Test', 'nom': 'Agbo', 'prenom': 'Afi', 'classe': 'CM2', 'parent': 'Agbo Rose',
            'telephone': '96 11 22 33', 'versement': '35000', 'inscription_versee': '5000', 'mode': 'especes',
            'suivi_actif': 'on',
        })
        nouvel = Eleve.objects.get(nom='Agbo')
        self.assertRedirects(r, reverse('ecole:eleve_detail', args=[nouvel.pk]))
        self.assertEqual(nouvel.montant_scolarite, 60000)
        self.assertEqual(nouvel.factures.count(), 2)
        self.assertContains(self.client.get(reverse('ecole:eleve_detail', args=[nouvel.pk])), 'Échéancier')

    def test_encaisser_affiche_la_facture(self):
        self.client.force_login(self.caisse)
        r = self.client.post(reverse('ecole:encaisser', args=[self.e.pk]), {
            'motif': 'scolarite', 'montant': '30000', 'mode': 'mobile_money', 'date': AUJOURDHUI.isoformat(),
            'reference': 'MM123', 'observation': 'Versé par la tante'})
        p = Paiement.objects.get()
        self.assertEqual(p.observation, 'Versé par la tante')
        self.assertContains(self.client.get(r.url), Facture.objects.get().numero)

    def test_demande_de_correction_validee_par_la_direction(self):
        p = payer(self.e, 30000, self.caisse).paiement
        self.client.force_login(self.caisse)
        url = reverse('ecole:paiement_corriger', args=[p.pk])
        self.client.post(url, {'action': 'modification', 'montant': '20000', 'mode': 'especes',
                               'date': p.date.isoformat(), 'motif': 'Mauvais montant'})
        p.refresh_from_db()
        self.assertEqual(p.montant, 30000)  # rien n'est appliqué sans validation
        demande = DemandeCorrection.objects.get()
        self.assertEqual(self.client.post(reverse('ecole:correction_traiter', args=[demande.pk]),
                                          {'decision': 'valider'}).status_code, 403)
        self.client.force_login(self.direction)
        self.client.post(reverse('ecole:correction_traiter', args=[demande.pk]), {'decision': 'valider'})
        p.refresh_from_db()
        demande.refresh_from_db()
        self.assertEqual((p.montant, demande.statut), (20000, StatutDemande.VALIDEE))
        self.assertEqual(HistoriquePaiement.objects.get().demande_par, self.caisse)

    def test_retards_dans_le_suivi_et_le_tableau_de_bord(self):
        self.client.force_login(self.direction)
        r = self.client.get(reverse('ecole:suivi') + '?statut=retard')
        self.assertEqual(r.context['eleves'], [self.e])
        self.assertEqual(self.client.get(reverse('ecole:accueil')).context['nb_retard'], 1)

    def test_grille_modifiee_met_a_jour_les_eleves(self):
        self.client.force_login(self.direction)
        t = list(self.grille.tranches.all())
        data = {'etablissement': self.etab.pk, 'classe': 'CM2', 'frais_inscription': '5000',
                'tranches-TOTAL_FORMS': '3', 'tranches-INITIAL_FORMS': '3', 'tranches-MIN_NUM_FORMS': '1',
                'tranches-MAX_NUM_FORMS': '1000'}
        for i, x in enumerate(t):
            data.update({f'tranches-{i}-id': x.pk, f'tranches-{i}-libelle': x.libelle,
                         f'tranches-{i}-montant': '40000', f'tranches-{i}-echeance': x.echeance.isoformat()})
        r = self.client.post(reverse('ecole:grille_modifier', args=[self.grille.pk]), data)
        self.assertRedirects(r, reverse('ecole:grilles'))
        self.assertEqual(Eleve.objects.get(pk=self.e.pk).montant_scolarite, 120000)

    def test_import_csv(self):
        self.client.force_login(self.direction)
        mauvais = 'Nom;Prénom;Classe;Parent;Téléphone\nA;B;CM2;C;\n'.encode()
        self.client.post(reverse('ecole:eleves_import'), {'fichier': SimpleUploadedFile('e.csv', mauvais)})
        self.assertEqual(Eleve.objects.count(), 1)  # rien de créé en cas d'erreur
        bon = 'Nom;Prénom;Classe;Parent;Téléphone;Email\nAgbo;Afi;CM2;Agbo Rose;96112233;a@b.bj\nZinsou;Paul;CM2;Zinsou;97000001;\n'.encode()
        r = self.client.post(reverse('ecole:eleves_import'), {'fichier': SimpleUploadedFile('e.csv', bon)})
        self.assertEqual(Eleve.objects.count(), 1)  # étape 1 : aperçu seulement
        self.assertEqual(len(r.context['apercu']), 2)
        r = self.client.post(reverse('ecole:eleves_import'), {'confirmer': '1', 'jeton': r.context['jeton']})
        self.assertRedirects(r, reverse('ecole:eleves'))
        self.assertEqual(Eleve.objects.get(nom='Agbo').echeances.count(), 3)

    def test_modele_import(self):
        self.client.force_login(self.direction)
        r = self.client.get(reverse('ecole:eleves_import_modele'))
        from .importation import analyser, lire_lignes
        fichier = SimpleUploadedFile('m.xlsx', r.content)
        self.assertEqual(len(analyser(lire_lignes(fichier)).a_creer), 2)

    def test_version_temps_reel(self):
        self.client.force_login(self.direction)
        v1 = self.client.get(reverse('ecole:version')).json()['v']
        payer(self.e, 1000)
        self.assertNotEqual(v1, self.client.get(reverse('ecole:version')).json()['v'])


class RelancesTests(Base):
    def test_rappel_avant_echeance_sans_doublon(self):
        e = eleve(self.etab)
        jour = e.echeances.get(numero=2).echeance - timedelta(days=7)
        envois = executer_relances(jour)
        self.assertGreater(envois, 0)
        self.assertEqual(executer_relances(jour), 0)
        self.assertTrue(Notification.objects.filter(type='rappel', eleve=e).exists())

    def test_relance_de_retard_et_suivi_desactive(self):
        e = eleve(self.etab)
        jour = e.echeances.get(numero=1).echeance + timedelta(days=3)
        executer_relances(jour)
        self.assertTrue(Notification.objects.filter(type='retard', message__contains='retard').exists())
        Notification.objects.all().delete()
        Eleve.objects.filter(pk=e.pk).update(suivi_actif=False)
        self.assertEqual(executer_relances(jour), 0)


class AgentTests(Base):
    def test_situation_par_numero(self):
        e = eleve(self.etab)
        payer(e, 10000)
        texte, _f, eleves = repondre('+229 01 97 00 00 00', 'SOLDE')
        self.assertEqual(eleves[0].pk, e.pk)
        self.assertIn('reste 50 000', texte)
        self.assertIn('retard', texte.lower())

    def test_numero_inconnu_et_facture(self):
        self.assertIn('aucun élève', repondre('+22990000000', 'bonjour')[0])
        e = eleve(self.etab)
        f = payer(e, 10000)
        _t, factures, _e = repondre('0197000000', '3')
        self.assertEqual(factures, [f])

    def test_webhooks_refuses_sans_configuration_ni_signature(self):
        eleve(self.etab)
        self.assertEqual(self.client.post('/webhooks/sms/', {'From': '+2290197000000', 'Body': 'solde'}).status_code, 403)
        self.assertEqual(self.client.get('/webhooks/whatsapp/', {'hub.mode': 'subscribe'}).status_code, 403)
        with override_settings(NOTIFICATIONS={'sms': {'backend': 'twilio', 'jeton': 'cle'}}):
            r = self.client.post('/webhooks/sms/', {'From': '+2290197000000', 'Body': 'solde'})
            self.assertEqual(r.status_code, 403)  # signature absente : la situation n'est pas révélée
        with override_settings(NOTIFICATIONS={'whatsapp': {'backend': 'meta', 'verification': 'v', 'secret_app': 's'}}):
            r = self.client.post('/webhooks/whatsapp/', '{}', content_type='application/json',
                                 HTTP_X_HUB_SIGNATURE_256='sha256=faux')
            self.assertEqual(r.status_code, 403)

    def test_webhooks_signes(self):
        import base64
        import hashlib
        import hmac
        eleve(self.etab)
        conf = {'whatsapp': {'backend': 'meta', 'verification': 'secret', 'secret_app': 'app', 'jeton': 'x', 'numero_id': '1'},
                'sms': {'backend': 'twilio', 'jeton': 'cle', 'compte': 'AC', 'expediteur': '+1'}}
        with override_settings(NOTIFICATIONS=conf), mock.patch('ecole.agent.transmettre' if False else 'ecole.notifications.transmettre',
                                                               return_value='envoye'):
            r = self.client.get('/webhooks/whatsapp/', {'hub.mode': 'subscribe', 'hub.verify_token': 'secret',
                                                        'hub.challenge': '42'})
            self.assertEqual(r.content, b'42')
            corps = json.dumps({'entry': [{'changes': [{'value': {'messages': [
                {'from': '2290197000000', 'text': {'body': 'echeances'}}]}}]}]}).encode()
            signature = 'sha256=' + hmac.new(b'app', corps, hashlib.sha256).hexdigest()
            self.client.post('/webhooks/whatsapp/', corps, content_type='application/json', HTTP_X_HUB_SIGNATURE_256=signature)
            self.assertEqual(Notification.objects.filter(type='reponse', canal='whatsapp').count(), 1)
            donnees = {'From': '+2290197000000', 'Body': 'solde'}
            url = 'http://testserver/webhooks/sms/'
            sig = base64.b64encode(hmac.new(b'cle', (url + ''.join(f'{k}{v}' for k, v in sorted(donnees.items()))).encode(),
                                            hashlib.sha1).digest()).decode()
            r = self.client.post('/webhooks/sms/', donnees, HTTP_X_TWILIO_SIGNATURE=sig)
            self.assertContains(r, '<Response><Message>')


@override_settings(VERROU_MODULES=False)
class EspacesTests(Base):
    def test_espace_parent(self):
        e = eleve(self.etab)
        autre = eleve(self.etab, telephone='96000000', nom='Autre')
        f_autre = payer(autre, 1000)
        direction = utilisateur(roles.DIRECTION)
        self.client.force_login(direction)
        self.client.post(reverse('ecole:acces_parent', args=[e.pk]),
                         {'identifiant': 'papa', 'mot_de_passe': 'Un-Long-Secret-2026'})
        compte = CompteParent.objects.get()
        self.assertEqual(list(compte.eleves.all()), [Eleve.objects.get(pk=e.pk)])
        self.client.force_login(compte.utilisateur)
        self.assertRedirects(self.client.get(reverse('ecole:portail')), reverse('parents:accueil'))
        self.assertContains(self.client.get(reverse('parents:accueil')), 'Koffi')
        self.assertEqual(self.client.get(reverse('parents:facture_pdf', args=[f_autre.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('ecole:eleves')).status_code, 403)

    def test_surveillant_perimetre_sans_montants(self):
        eleve(self.etab)
        eleve(self.etab, classe='6e', nom='Hors')
        surveillant = utilisateur(roles.SURVEILLANT, 'surv')
        PerimetreSurveillant.objects.create(utilisateur=surveillant, classes=[f'{self.etab.pk}|CM2'])
        self.client.force_login(surveillant)
        r = self.client.get(reverse('ecole:controle'))
        self.assertEqual([x.nom for x in r.context['eleves']], ['Dossou'])
        self.assertContains(r, 'En retard')
        self.assertNotContains(r, 'FCFA')
        self.assertEqual(self.client.get(reverse('ecole:eleves')).status_code, 403)


@override_settings(VERROU_MODULES=False)
class PagesTests(Base):
    def test_toutes_les_pages_repondent(self):
        direction = utilisateur(roles.DIRECTION)
        e = eleve(self.etab)
        ReductionEleve.objects.create(eleve=e, type='bourse', mode='pourcentage', valeur=10)
        f = payer(e, 10000, direction)
        p = f.paiement
        DemandeCorrection.objects.create(eleve=e, paiement=p, numero_recu=p.numero_recu, motif='x', demande_par=direction,
                                         nouvelles_valeurs={'montant': 5000, 'mode': 'especes', 'date': AUJOURDHUI.isoformat()})
        self.client.force_login(direction)
        pages = [
            reverse('ecole:accueil'), reverse('ecole:eleves'), reverse('ecole:eleve_detail', args=[e.pk]),
            reverse('ecole:eleve_echeancier', args=[e.pk]), reverse('ecole:eleve_nouveau'),
            reverse('ecole:eleve_modifier', args=[e.pk]), reverse('ecole:eleves_import'),
            reverse('ecole:encaisser', args=[e.pk]), reverse('ecole:recu', args=[p.pk]),
            reverse('ecole:recu_pdf', args=[p.pk]), reverse('ecole:facture_pdf', args=[f.pk]),
            reverse('ecole:paiement_corriger', args=[p.pk]), reverse('ecole:paiement_supprimer', args=[p.pk]),
            reverse('ecole:corrections'), reverse('ecole:factures'), reverse('ecole:messages'),
            reverse('ecole:suivi') + '?statut=retard', reverse('ecole:controle'), reverse('ecole:journal'),
            reverse('ecole:grilles'), reverse('ecole:grille_nouvelle'), reverse('ecole:grille_modifier', args=[self.grille.pk]),
            reverse('ecole:regles_relance'), reverse('ecole:regle_relance_nouvelle'),
            reverse('ecole:etablissement_modifier', args=[self.etab.pk]), reverse('ecole:parametres'),
            reverse('ecole:utilisateurs'), reverse('ecole:utilisateur_modifier', args=[utilisateur(roles.SURVEILLANT, 's').pk]),
            reverse('ecole:portail'),
        ]
        for url in pages:
            self.assertEqual(self.client.get(url).status_code, 200, url)


@override_settings(VERROU_MODULES=False)
class FraisParClasseTests(Base):
    def test_frais_dans_le_module_scolarite_et_le_formulaire(self):
        from .modules import module_courant  # noqa: F401
        secretaire = utilisateur(roles.SECRETARIAT, 'sec')
        self.client.force_login(secretaire)
        r = self.client.get(reverse('ecole:grilles'))
        self.assertEqual(r.status_code, 200)
        self.assertIn('mod-scolarite', r.content.decode())
        self.assertEqual(self.client.get(reverse('ecole:grille_nouvelle')).status_code, 200)
        r = self.client.get(reverse('ecole:eleve_nouveau'))
        frais = r.context['frais_classes']['cegtest|cm2']
        self.assertEqual((frais['scolarite'], frais['inscription'], len(frais['tranches'])), (60000, 5000, 3))
        self.assertContains(r, 'id="frais-classes"')
        self.assertIn('CM2', r.context['classes'])


class AffectationTranchesTests(TestCase):
    """Exemple du cahier des charges : 300 000 FCFA en 3 tranches de 100 000."""

    def setUp(self):
        self.etab = Etablissement.objects.create(nom='Lycée Exemple')
        g = GrilleFrais.objects.create(etablissement=self.etab, classe='Tle')
        for i, jours in enumerate((10, 100, 190), start=1):
            TrancheGrille.objects.create(grille=g, numero=i, libelle=f'Tranche {i}', montant=100000,
                                         echeance=AUJOURDHUI + timedelta(days=jours))
        self.e = eleve(self.etab, classe='Tle')

    def recharger(self):
        return Eleve.objects.avec_totaux().get(pk=self.e.pk)

    def lignes(self):
        return [(l.paye, l.reste, l.statut) for l in situation(self.recharger()).lignes]

    def test_exemple_50000_puis_70000(self):
        self.assertEqual(self.lignes(), [(0, 100000, 'en_cours'), (0, 100000, 'a_venir'), (0, 100000, 'a_venir')])
        p1 = payer(self.e, 50000).paiement
        self.assertEqual(self.lignes()[0], (50000, 50000, 'partielle'))  # surtout pas soldée
        self.assertEqual(situation(self.recharger()).lignes[0].statut_libelle, 'Partiellement payée')
        p2 = payer(self.recharger(), 70000).paiement
        self.assertEqual(self.lignes(), [(100000, 0, 'payee'), (20000, 80000, 'partielle'), (0, 100000, 'a_venir')])
        # Historique enregistré : tranches concernées, montant affecté, soldes après le paiement
        a1 = [(i.libelle, i.montant, i.reste_tranche, i.reste_total) for i in p1.imputations.all()]
        a2 = [(i.libelle, i.montant, i.reste_tranche, i.reste_total) for i in p2.imputations.all()]
        self.assertEqual(a1, [('Tranche 1', 50000, 50000, 250000)])
        self.assertEqual(a2, [('Tranche 1', 50000, 0, 180000), ('Tranche 2', 20000, 80000, 180000)])
        self.assertEqual([l['montant'] for l in Facture.objects.get(paiement=p2).lignes], [50000, 20000])
        self.assertEqual(Paiement.objects.get(pk=p2.pk).montant, 70000)  # montant versé intact

    def test_paiement_couvrant_plusieurs_tranches(self):
        p = payer(self.e, 250000).paiement
        self.assertEqual([i.montant for i in p.imputations.all()], [100000, 100000, 50000])
        self.assertEqual(self.lignes()[2], (50000, 50000, 'partielle'))

    def test_paiement_antidate_reaffecte_dans_l_ordre_des_dates(self):
        payer(self.e, 30000)
        ancien = payer(self.recharger(), 100000, date=AUJOURDHUI - timedelta(days=20)).paiement
        self.assertEqual([(i.libelle, i.montant) for i in ancien.imputations.all()], [('Tranche 1', 100000)])
        recent = Paiement.objects.exclude(pk=ancien.pk).get()
        self.assertEqual([(i.libelle, i.montant) for i in recent.imputations.all()], [('Tranche 2', 30000)])

    def test_retard_partiellement_paye(self):
        payer(self.e, 40000)
        sit = situation(self.recharger(), aujourdhui=AUJOURDHUI + timedelta(days=11))
        self.assertEqual((sit.lignes[0].statut, sit.lignes[0].reste), ('retard', 60000))
        self.assertEqual(sit.lignes[0].statut_libelle, 'En retard — partiellement payée')

    def test_correction_et_annulation_reaffectent(self):
        f = payer(self.e, 120000)
        corriger_paiement(f.paiement, {'montant': 90000}, 'Erreur', utilisateur(roles.DIRECTION))
        self.assertEqual(self.lignes()[:2], [(90000, 10000, 'partielle'), (0, 100000, 'a_venir')])
        annuler_paiement(Paiement.objects.get(), 'Erreur', User.objects.get())
        self.assertFalse(ImputationPaiement.objects.exists())
        self.assertEqual(self.lignes()[0], (0, 100000, 'en_cours'))

    def test_anciens_paiements_marques_tranche_2_affectes_a_la_tranche_1(self):
        Paiement.objects.create(eleve=self.e, motif=Motif.TRANCHE2, montant=115000)
        appliquer_reductions(self.e)
        self.assertEqual(self.lignes()[:2], [(100000, 0, 'payee'), (15000, 85000, 'partielle')])

    def test_classe_sans_frais_tranche_unique(self):
        e = Eleve.objects.create(nom='X', prenom='Y', parent='Z', classe='5e', telephone='97', montant_scolarite=80000,
                                 etablissement=self.etab)
        generer_echeancier(e)
        self.assertEqual([(t.libelle, t.montant) for t in e.echeances.all()], [('Tranche unique', 80000)])
        f = payer(Eleve.objects.avec_totaux().get(pk=e.pk), 30000)
        self.assertEqual([(i.montant, i.reste_tranche) for i in f.paiement.imputations.all()], [(30000, 50000)])

    def test_commande_de_recalcul(self):
        e = Eleve.objects.create(nom='X', prenom='Y', parent='Z', classe='5e', telephone='97', montant_scolarite=80000,
                                 etablissement=self.etab)
        Paiement.objects.create(eleve=e, motif=Motif.TRANCHE1, montant=20000)
        from django.core.management import call_command
        call_command('recalculer_tranches', stdout=io.StringIO())
        self.assertEqual(e.echeances.count(), 1)
        self.assertEqual(ImputationPaiement.objects.filter(eleve=e).get().reste_tranche, 60000)


@override_settings(VERROU_MODULES=False)
class InscriptionTests(Base):
    def setUp(self):
        super().setUp()
        self.direction = utilisateur(roles.DIRECTION)
        self.client.force_login(self.direction)

    def test_statut_de_l_inscription(self):
        e = eleve(self.etab)  # grille : 5 000 de frais d'inscription
        self.assertEqual((e.frais_inscription, e.statut_inscription), (5000, 'impayee'))
        payer(e, 2000, motif=Motif.INSCRIPTION)
        e = Eleve.objects.avec_totaux().get(pk=e.pk)
        self.assertEqual((e.statut_inscription, e.inscription_reste), ('partielle', 3000))
        r = self.client.get(reverse('ecole:suivi') + '?statut=inscription')
        self.assertEqual(r.context['eleves'], [e])
        self.assertContains(self.client.get(reverse('ecole:eleve_detail', args=[e.pk])), 'Inscription partielle')
        payer(e, 3000, motif=Motif.INSCRIPTION)
        e = Eleve.objects.avec_totaux().get(pk=e.pk)
        self.assertEqual(e.statut_inscription, 'payee')
        self.assertEqual(self.client.get(reverse('ecole:suivi') + '?statut=inscription').context['eleves'], [])

    def test_encaissement_propose_l_inscription_et_refuse_le_trop_percu(self):
        e = eleve(self.etab)
        r = self.client.get(reverse('ecole:encaisser', args=[e.pk]))
        self.assertEqual((r.context['form'].initial['motif'], r.context['form'].initial['montant']), ('inscription', 5000))
        r = self.client.post(reverse('ecole:encaisser', args=[e.pk]), {
            'motif': 'inscription', 'montant': '6000', 'mode': 'especes', 'date': AUJOURDHUI.isoformat()})
        self.assertIn('montant', r.context['form'].errors)

    def test_classe_sans_frais_montant_saisi(self):
        r = self.client.post(reverse('ecole:eleve_nouveau'), {
            'etablissement_nom': 'CEG Test', 'nom': 'Zinsou', 'prenom': 'Paul', 'classe': '3e', 'parent': 'Z',
            'telephone': '97000009', 'montant_scolarite': '90000', 'frais_inscription': '10000',
            'inscription_versee': '4000', 'mode': 'especes'})
        e = Eleve.objects.avec_totaux().get(nom='Zinsou')
        self.assertRedirects(r, reverse('ecole:eleve_detail', args=[e.pk]))
        self.assertEqual((e.frais_inscription, e.total_inscription, e.statut_inscription), (10000, 4000, 'partielle'))


@override_settings(VERROU_MODULES=False)
class FratrieInscriptionTests(Base):
    """Un parent inscrit trois enfants : 2ᵉ enfant −50 %, 3ᵉ enfant gratuit sur les frais d'inscription."""

    def setUp(self):
        super().setUp()
        self.etab.reduction_inscription_2, self.etab.reduction_inscription_3 = 50, 100
        self.etab.save()
        self.direction = utilisateur(roles.DIRECTION)
        self.client.force_login(self.direction)

    def test_trois_enfants(self):
        a = eleve(self.etab, prenom='Aîné')
        b = eleve(self.etab, prenom='Cadet')
        c = eleve(self.etab, prenom='Benjamin')
        self.assertEqual([(e.frais_inscription, e.inscription_due) for e in (a, b, c)],
                         [(5000, 5000), (5000, 2500), (5000, 0)])
        self.assertEqual((c.statut_inscription, str(c.statut_inscription_libelle)), ('payee', 'Inscription offerte'))
        self.assertEqual(b.statut_inscription, 'impayee')
        payer(b, 2500, motif=Motif.INSCRIPTION)
        self.assertEqual(Eleve.objects.avec_totaux().get(pk=b.pk).statut_inscription, 'payee')

    def test_remise_manuelle_sur_l_inscription(self):
        e = eleve(self.etab)
        self.client.post(reverse('ecole:reduction_ajouter', args=[e.pk]),
                         {'porte_sur': 'inscription', 'type': 'remise', 'mode': 'montant', 'valeur': '2000',
                          'motif': 'Enfant du personnel'})
        e = Eleve.objects.avec_totaux().get(pk=e.pk)
        self.assertEqual((e.inscription_due, e.montant_scolarite), (3000, 60000))  # la scolarité n'est pas touchée

    def test_inscrire_un_frere_depuis_la_fiche(self):
        aine = eleve(self.etab, prenom='Aîné')
        r = self.client.get(reverse('ecole:eleve_nouveau') + f'?frere={aine.pk}')
        self.assertEqual(r.context['form'].initial['telephone'], aine.telephone)
        self.assertEqual(r.context['fratrie_inscription'], {'rang': 2, 'taux': 50})
        r = self.client.post(reverse('ecole:eleve_nouveau') + f'?frere={aine.pk}', {
            'etablissement_nom': 'CEG Test', 'nom': 'Dossou', 'prenom': 'Cadet', 'classe': 'CM2', 'parent': aine.parent,
            'telephone': aine.telephone, 'inscription_versee': '5000', 'mode': 'especes'})
        cadet = Eleve.objects.avec_totaux().get(prenom='Cadet')
        self.assertEqual((cadet.inscription_due, cadet.total_inscription), (2500, 2500))  # jamais plus que le dû
        self.assertContains(self.client.get(reverse('ecole:eleve_detail', args=[cadet.pk])), 'Fratrie (2e enfant)')


@override_settings(VERROU_MODULES=False)
class ClasseTolerancteTests(Base):
    def test_classe_et_etablissement_ecrits_autrement(self):
        self.client.force_login(utilisateur(roles.DIRECTION))
        self.client.post(reverse('ecole:eleve_nouveau'), {
            'etablissement_nom': 'ceg test', 'nom': 'Agbo', 'prenom': 'Afi', 'classe': 'cm 2', 'parent': 'Agbo Rose',
            'telephone': '96112233', 'mode': 'especes'})
        e = Eleve.objects.get(nom='Agbo')
        self.assertEqual((e.classe, e.montant_scolarite, e.frais_inscription, e.echeances.count()), ('CM2', 60000, 5000, 3))

    def test_cle_texte(self):
        from .echeancier import cle_texte
        self.assertEqual({cle_texte(x) for x in ('6ème A', '6eme a', '6EMEA', ' 6ᵉme  A ')}, {'6emea'})



@override_settings(VERROU_MODULES=False)
class ImportCompletTests(Base):
    """Élèves pas encore dans la base, avec toutes les informations possibles dans le fichier Excel."""

    ENTETES = ['Nom', 'Prénom(s)', 'Sexe', 'Date de naissance', 'Lieu de naissance', 'Classe', 'Établissement',
               'N° Educ Master', 'Résidence', 'Parent', 'Lien', 'Téléphone', 'WhatsApp', 'Email',
               'Parent 2', 'Lien parent 2', 'Téléphone parent 2', 'WhatsApp parent 2', 'Email parent 2',
               'Montant scolarité', "Frais d'inscription", 'Réduction scolarité', 'Réduction inscription',
               'Type de réduction', 'Motif réduction', 'Inscription déjà versée', 'Scolarité déjà versée',
               'Date du versement', 'Mode de paiement', 'Référence du paiement', 'Rappels automatiques']

    def fichier(self, *lignes):
        from openpyxl import Workbook
        classeur = Workbook()
        feuille = classeur.active
        feuille.append(self.ENTETES)
        for ligne in lignes:
            feuille.append(ligne)
        sortie = io.BytesIO()
        classeur.save(sortie)
        return SimpleUploadedFile('eleves.xlsx', sortie.getvalue())

    def setUp(self):
        super().setUp()
        self.client.force_login(utilisateur(roles.DIRECTION))

    def test_import_complet(self):
        from datetime import date
        ligne = ['HOUNSOU', 'Rachel', 'F', date(2015, 4, 9), 'Porto-Novo', 'cm 2', 'ceg test', 'EM-001', 'Akpakpa',
                 'HOUNSOU Paul', 'Père', '97 11 22 33', '', 'paul@exemple.bj',
                 'HOUNSOU Rose', 'Mère', '96 44 55 66', '', 'rose@exemple.bj',
                 '', '', '10 %', '', 'Bourse', 'Excellence',
                 5000, 35000, '15/09/2026', 'Mobile Money', 'MM-778899', 'Oui']
        r = self.client.post(reverse('ecole:eleves_import'), {'fichier': self.fichier(ligne)})
        self.assertFalse(r.context['rapport'].erreurs, r.context['rapport'].erreurs)
        self.client.post(reverse('ecole:eleves_import'), {'confirmer': '1', 'jeton': r.context['jeton']})
        e = Eleve.objects.avec_totaux().get(nom='HOUNSOU')
        self.assertEqual((e.classe, e.sexe, e.date_naissance, e.lieu_naissance, e.educ_master), ('CM2', 'F', date(2015, 4, 9),
                                                                                                'Porto-Novo', 'EM-001'))
        self.assertEqual(e.contacts.get().nom, 'HOUNSOU Rose')
        self.assertEqual(e.montant_scolarite, 54000)  # grille 60 000 − bourse 10 %
        self.assertEqual((e.total_inscription, e.total_verse), (5000, 35000))
        p = e.paiements.get(motif='scolarite')
        self.assertEqual((p.mode, p.reference, p.date.isoformat()), ('mobile_money', 'MM-778899', '2026-09-15'))
        self.assertTrue(p.imputations.exists() and p.factures.exists())
        self.assertFalse(Notification.objects.exists())  # factures non envoyées par défaut

    def test_valeurs_illisibles_signalees_et_eleve_quand_meme_cree(self):
        ligne = ['A', 'B', 'X', '31/02/2015', '', 'CM2', 'CEG Test', '', '', 'C', 'Oncle', '97000000', '', 'pas-un-mail',
                 '', '', '', '', '', '', '', '150 %', '', '', '', '', '', '', 'Bitcoin', '', 'Peut-être']
        r = self.client.post(reverse('ecole:eleves_import'), {'fichier': self.fichier(ligne)})
        self.assertFalse(r.context['rapport'].erreurs)
        messages = ' '.join(m for _n, m in r.context['rapport'].avertissements)
        for attendu in ('Sexe', 'Date de naissance', 'Lien', 'E-mail', 'Réduction', 'Mode de paiement', 'Rappels'):
            self.assertIn(attendu, messages)
        self.client.post(reverse('ecole:eleves_import'), {'confirmer': '1', 'jeton': r.context['jeton']})
        e = Eleve.objects.get(nom='A')
        self.assertEqual((e.sexe, e.date_naissance, e.email, e.nature_parent), ('', None, '', ''))

    def test_informations_absentes_neant(self):
        ligne = ['SOSSOU', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '',
                 '', '', '', '', '', '']
        r = self.client.post(reverse('ecole:eleves_import'), {'fichier': self.fichier(ligne)})
        self.client.post(reverse('ecole:eleves_import'), {'confirmer': '1', 'jeton': r.context['jeton']})
        e = Eleve.objects.get(nom='SOSSOU')
        self.assertEqual((e.prenom, e.classe, e.parent, e.telephone, e.etablissement.nom),
                         ('Néant', 'Néant', 'Néant', 'Néant', 'CEG Test'))  # un seul établissement : utilisé

    def test_confirmation_sans_apercu_refusee(self):
        r = self.client.post(reverse('ecole:eleves_import'), {'confirmer': '1', 'jeton': 'faux'})
        self.assertRedirects(r, reverse('ecole:eleves_import'))
        self.assertEqual(Eleve.objects.count(), 0)


@override_settings(VERROU_MODULES=False)
class ImportFichierReelTests(Base):
    """Fichier comme celui de l'école : titre, « Nom et Prénom », classe + année, détail des paiements, sans téléphone."""

    def fichier(self):
        from openpyxl import Workbook
        classeur = Workbook()
        f = classeur.active
        f.append(['Suivi des Frais Scolaires par élève 2026-2027'])
        f.append(['N°', 'Nom et Prénom', 'Classe', 'ANNEES', 'Frais Scolarité (CFA)', 'Frais Inscription (CFA)',
                  'Total Attendu (CFA)', 'Détail des Paiements', 'Total Payé (CFA)', 'Solde Restant (CFA)', 'Statut',
                  'TELEPHONE ', 'NOM ET PRENOM DU PARENT'])
        f.append([1, 'Abdulahi Aboubakar Sadick', None, None, 96000, 2000, 98000,
                  '1. 7/9/2026 - Paiement 1er trimestre : 32 000 CFA', 32000, 66000, 'Ancien', None, None])
        f.append([2, 'Adeoye Jamaima', 'Primaire', 'CE1', 105000, 2000, 107000,
                  '1. 7/9/2026 - Paiement 1er trimestre : 35 000 CFA\n2. 7/9/2026 - Paiement inscription : 2 000 CFA',
                  37000, 70000, 'Ancien', None, None])
        f.append([3, 'Abdoul Nasser Mahamat ', 'Maternelle', 2, 96000, 5000, 101000,
                  '1. 7/9/2026 - Paiement inscription : 5 000 CFA\n2. 7/9/2026 - Paiement 1er trimestre : 32 000 CFA',
                  37000, 64000, 'Nouveau', None, None])
        f.append([4, 'Yoann Hazoume', 'Maternelle', 2, None, None, 0, None, 0, 0, 'Nouveau', None, None])
        f.append([None] * 13)
        f.append([None, None, None, None, None, None, 306000, None, 74000, None, None, None, None])  # ligne de total
        sortie = io.BytesIO()
        classeur.save(sortie)
        return SimpleUploadedFile('SUIVIE PAIEMENT SCOLARITE.xlsx', sortie.getvalue())

    def test_fichier_de_l_ecole(self):
        self.client.force_login(utilisateur(roles.DIRECTION))
        r = self.client.post(reverse('ecole:eleves_import'), {'etablissement': 'CEG Test', 'fichier': self.fichier()})
        statuts = {x['eleve']: x['statut'] for x in r.context['rapport'].resultats}
        self.assertEqual(statuts['Abdulahi Aboubakar Sadick'], 'ok')       # pas de classe : « Néant »
        self.assertEqual(statuts['Adeoye Jamaima'], 'ok')
        self.assertEqual(statuts['Yoann Hazoume'], 'ok')                   # montant repris de la classe
        self.assertNotContains(r, '<tr class="erreur">')
        self.assertContains(r, '<tr class="ok">', count=4)
        self.client.post(reverse('ecole:eleves_import'), {'confirmer': '1', 'jeton': r.context['jeton']})
        nasser = Eleve.objects.avec_totaux().get(nom='Abdoul')
        self.assertEqual((nasser.prenom, nasser.classe, nasser.montant_scolarite, nasser.total_inscription,
                          nasser.total_verse), ('Nasser Mahamat', 'Maternelle 2', 96000, 5000, 32000))
        self.assertEqual(Eleve.objects.get(nom='Yoann').montant_scolarite, 96000)
        self.assertEqual(Eleve.objects.get(nom='Abdulahi').classe, 'Néant')

    def test_meme_fichier_jamais_importe_deux_fois(self):
        self.client.force_login(utilisateur(roles.DIRECTION))
        r = self.client.post(reverse('ecole:eleves_import'), {'etablissement': 'CEG Test', 'fichier': self.fichier()})
        jeton = r.context['jeton']
        self.client.post(reverse('ecole:eleves_import'), {'confirmer': '1', 'jeton': jeton})
        nb_eleves, nb_paiements = Eleve.objects.count(), Paiement.objects.count()
        self.assertEqual(nb_eleves, 4)
        # Double clic / retour arrière sur la confirmation : refusé avec un message
        r = self.client.post(reverse('ecole:eleves_import'), {'confirmer': '1', 'jeton': jeton}, follow=True)
        self.assertContains(r, 'déjà été enregistré')
        # Le même fichier rechargé : prévenu, chaque élève signalé « déjà dans la base », rien à importer
        r = self.client.post(reverse('ecole:eleves_import'), {'etablissement': 'CEG Test', 'fichier': self.fichier()})
        self.assertContains(r, 'Ce fichier a déjà été importé le')
        self.assertContains(r, 'Rien à importer')
        self.assertEqual({x['statut'] for x in r.context['rapport'].resultats if x['eleve'] != '—'}, {'deja'})
        self.assertContains(r, '<tr class="deja">', count=4)
        self.assertFalse(r.context['jeton'])
        # Même sans préciser l'établissement (lignes « Néant ») et après archivage : toujours reconnus
        Eleve.objects.filter(nom='Yoann').update(archive=True)
        r = self.client.post(reverse('ecole:eleves_import'), {'fichier': self.fichier()})
        self.assertEqual(r.context['nb_deja'], 4)
        self.assertEqual((Eleve.objects.count(), Paiement.objects.count()), (nb_eleves, nb_paiements))

    def test_eleve_deja_present_signale_meme_ordre_et_casse_differents(self):
        from .models import ImportFichier
        self.client.force_login(utilisateur(roles.DIRECTION))
        Eleve.objects.create(etablissement=Etablissement.objects.get(nom='CEG Test'), nom='JAMAIMA', prenom='adéoye',
                             classe='CE1', parent='X', telephone='97000000', montant_scolarite=105000)
        r = self.client.post(reverse('ecole:eleves_import'), {'etablissement': 'CEG Test', 'fichier': self.fichier()})
        statuts = {x['eleve']: x['statut'] for x in r.context['rapport'].resultats}
        self.assertEqual(statuts['Adeoye Jamaima'], 'deja')
        self.assertEqual(statuts['Yoann Hazoume'], 'ok')
        self.client.post(reverse('ecole:eleves_import'), {'confirmer': '1', 'jeton': r.context['jeton']})
        self.assertEqual(Eleve.objects.count(), 4)
        trace = ImportFichier.objects.get()
        self.assertEqual((trace.nb_eleves, trace.nom), (3, 'SUIVIE PAIEMENT SCOLARITE.xlsx'))
        self.assertEqual(trace.eleves.count(), 3)


@override_settings(VERROU_MODULES=False)
class RelancesSmsTests(Base):
    """Relances par SMS via un téléphone passerelle (puce MTN / Celtiis)."""

    PASSERELLE = {'sms': {'backend': 'android', 'passerelle_url': 'http://192.168.1.20:8080',
                          'passerelle_utilisateur': 'sms', 'passerelle_mot_de_passe': 'secret'}}

    def test_texte_sms_reste_dans_l_alphabet_gsm(self):
        from .notifications import nombre_sms, texte_sms
        t = texte_sms('Tranche 2 — 35 000 FCFA « ça » être’')
        self.assertEqual(t, 'Tranche 2 - 35 000 FCFA " ca " etre\'')
        self.assertEqual(nombre_sms('é' * 160), 1)   # é fait partie de l'alphabet GSM
        self.assertEqual(nombre_sms('a' * 161), 2)

    def test_envoi_par_le_telephone_passerelle(self):
        from .notifications import transmettre

        class Reponse(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with override_settings(NOTIFICATIONS=self.PASSERELLE), \
                mock.patch('urllib.request.urlopen', return_value=Reponse(b'{"id": "x"}')) as appel:
            self.assertEqual(transmettre('sms', '22997123456', 'Rappel — 35 000 FCFA'), 'envoye')
        requete = appel.call_args[0][0]
        self.assertEqual(requete.full_url, 'http://192.168.1.20:8080/message')
        self.assertEqual(json.loads(requete.data), {'message': 'Rappel - 35 000 FCFA', 'phoneNumbers': ['+22997123456']})
        self.assertTrue(requete.headers['Authorization'].startswith('Basic '))

    def test_sms_essai(self):
        self.client.force_login(utilisateur(roles.DIRECTION))
        with override_settings(NOTIFICATIONS={'sms': {'backend': 'console'}}):
            r = self.client.post(reverse('ecole:sms_essai'), {'numero': '01 97 12 34 56', 'message': 'Essai'}, follow=True)
        self.assertContains(r, 'SMS simulé')
        self.assertEqual(Notification.objects.get().destinataire, '2290197123456')
        with override_settings(NOTIFICATIONS=self.PASSERELLE), \
                mock.patch('urllib.request.urlopen', side_effect=OSError('téléphone injoignable')):
            r = self.client.post(reverse('ecole:sms_essai'), {'numero': '0197123456', 'message': 'Essai'}, follow=True)
        self.assertContains(r, 'Échec de l’envoi')
        self.client.force_login(utilisateur(roles.CAISSIER))
        self.assertEqual(self.client.post(reverse('ecole:sms_essai'), {'numero': '0197123456', 'message': 'x'}).status_code, 403)

    def test_regles_par_defaut_en_sms_uniquement(self):
        from .models import RegleRelance
        regles = RegleRelance.objects.all()
        self.assertEqual(regles.count(), 3)
        self.assertEqual({(r.whatsapp, r.sms, r.email) for r in regles}, {(False, True, False)})
        self.client.force_login(utilisateur(roles.DIRECTION))
        self.assertContains(self.client.get(reverse('ecole:regles_relance')), '≈ 1 SMS par envoi', count=3)


@override_settings(VERROU_MODULES=False)
class FicheEleveTests(Base):
    def test_telecharger_la_fiche_pdf(self):
        e = eleve(self.etab)
        payer(e, 20000)
        self.client.force_login(utilisateur(roles.DIRECTION))
        self.assertContains(self.client.get(reverse('ecole:eleve_detail', args=[e.pk])), 'Télécharger la fiche')
        r = self.client.get(reverse('ecole:eleve_fiche_pdf', args=[e.pk]))
        self.assertEqual(r['Content-Type'], 'application/pdf')
        self.assertTrue(r.content.startswith(b'%PDF'))
        self.assertIn('attachment', r['Content-Disposition'])
        self.client.force_login(utilisateur(roles.SURVEILLANT))
        self.assertEqual(self.client.get(reverse('ecole:eleve_fiche_pdf', args=[e.pk])).status_code, 403)

    def test_fiche_soldee_archivee_avec_fratrie(self):
        from .pdf import pdf_fiche_eleve
        e = eleve(self.etab)
        eleve(self.etab, classe='CE1')  # même téléphone : frère ou sœur
        payer(e, 60000)
        e = Eleve.objects.avec_totaux().get(pk=e.pk)
        e.archive, e.motif_archive = True, 'Transfert'
        contenu = pdf_fiche_eleve(e, e.situation, list(e.paiements.all()), [],
                                  fratrie=list(Eleve.objects.filter(etablissement=self.etab)))
        self.assertTrue(contenu.startswith(b'%PDF'))
