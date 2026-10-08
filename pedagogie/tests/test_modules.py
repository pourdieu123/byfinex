from datetime import date, time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from ecole import roles
from ecole.models import Decoupage, Eleve, Etablissement

from ..calculs import libelle_rang, rangs, resultats_classe
from ..models import Composition, Creneau, Enseignement, Epreuve, FicheNote, Matiere, PaiementProfesseur, Professeur


def utilisateur(role, username=None):
    user = User.objects.create_user(username or role.lower(), password='Mot-de-passe-123')
    roles.definir_role(user, role)
    return user


@override_settings(VERROU_MODULES=False, MODULES_BIENTOT=[])
class Donnees(TestCase):
    """Un établissement, une classe de 3 élèves, 2 matières et 2 professeurs."""

    def setUp(self):
        self.etab = Etablissement.objects.create(nom='CEG Test')
        self.eleves = [
            Eleve.objects.create(nom=n, prenom='X', parent='P', classe='6ème', telephone='1',
                                 montant_scolarite=1000, etablissement=self.etab)
            for n in ('Agbo', 'Bio', 'Dossou')
        ]
        self.maths = Matiere.objects.create(nom='Mathématiques', abreviation='MATH')
        self.fr = Matiere.objects.create(nom='Français', abreviation='FR')
        self.prof_maths = Professeur.objects.create(nom='Koffi', prenoms='Jean', telephone='97', taux_horaire=2000)
        self.prof_fr = Professeur.objects.create(nom='Sossa', prenoms='Awa', telephone='96')
        self.ens_maths = Enseignement.objects.create(etablissement=self.etab, classe='6ème', matiere=self.maths,
                                                     coefficient=3, professeur=self.prof_maths)
        self.ens_fr = Enseignement.objects.create(etablissement=self.etab, classe='6ème', matiere=self.fr,
                                                  coefficient=2, professeur=self.prof_fr)

    def notes(self, eleve, ens, periode=1, **valeurs):
        return FicheNote.objects.create(eleve=eleve, enseignement=ens, periode=periode,
                                        **{k: Decimal(str(v)) for k, v in valeurs.items()})


class CalculsTests(Donnees):
    def test_moyenne_beninoise(self):
        f = self.notes(self.eleves[0], self.ens_maths, i1=12, i2=14, d1=10, d2=16)
        self.assertEqual(f.mi, Decimal('13.00'))
        self.assertEqual(f.moyenne, Decimal('13.00'))  # (13 + 10 + 16) / 3

    def test_moyenne_sur_notes_disponibles(self):
        f = self.notes(self.eleves[0], self.ens_maths, i1=9, d1=15)
        self.assertEqual(f.moyenne, Decimal('12.00'))  # (9 + 15) / 2, pas encore de D2
        self.assertIsNone(FicheNote(eleve=self.eleves[0], enseignement=self.ens_maths, periode=2).moyenne)

    def test_moyenne_generale_coefficients_et_rangs(self):
        a, b, c = self.eleves
        self.notes(a, self.ens_maths, i1=16, d1=16, d2=16)   # 16 × 3
        self.notes(a, self.ens_fr, i1=10, d1=10, d2=10)      # 10 × 2 -> 68 / 5 = 13,60
        self.notes(b, self.ens_maths, i1=10, d1=10, d2=10)
        self.notes(b, self.ens_fr, i1=19, d1=19, d2=19)      # (30 + 38) / 5 = 13,60 -> ex aequo
        self.notes(c, self.ens_maths, i1=8, d1=8, d2=8)      # seulement les maths -> 8,00
        res = resultats_classe(self.etab, '6ème', 1)
        ra, rb, rc = (res.pour(e.pk) for e in (a, b, c))
        self.assertEqual((ra.moyenne, rb.moyenne, rc.moyenne), (Decimal('13.60'), Decimal('13.60'), Decimal('8.00')))
        self.assertEqual((libelle_rang(ra.rang), libelle_rang(rb.rang), libelle_rang(rc.rang)), ('1er ex', '1er ex', '3e'))
        self.assertEqual(ra.appreciation, 'Assez bien')  # 12 ≤ 13,60 < 14
        self.assertEqual(res.taux_reussite, 67)
        self.assertEqual(res.moyenne_classe, Decimal('11.73'))

    def test_moyenne_annuelle_semestres(self):
        self.etab.decoupage = Decoupage.SEMESTRE
        self.etab.save()
        a = self.eleves[0]
        self.notes(a, self.ens_maths, periode=1, d1=10)
        self.notes(a, self.ens_maths, periode=2, d1=14)
        res = resultats_classe(self.etab, '6ème', 2)
        self.assertEqual(res.pour(a.pk).moyenne_annuelle, Decimal('12.00'))
        self.assertEqual(self.etab.periodes, [(1, '1er semestre'), (2, '2e semestre')])

    def test_rangs_ex_aequo(self):
        self.assertEqual(rangs({'a': 12, 'b': 15, 'c': 12, 'd': None}), {'b': (1, False), 'a': (2, True), 'c': (2, True)})


class SaisieNotesTests(Donnees):
    def test_saisie_par_la_direction(self):
        self.client.force_login(utilisateur(roles.DIRECTION))
        a, b, _ = self.eleves
        r = self.client.post(f"{reverse('notes:saisie', args=[self.ens_maths.pk])}?periode=1", {
            f'{a.pk}-i1': '12,5', f'{a.pk}-d1': '14', f'{b.pk}-i1': '', f'{b.pk}-d2': '8.75',
        })
        self.assertEqual(r.status_code, 302)
        fa = FicheNote.objects.get(eleve=a, enseignement=self.ens_maths, periode=1)
        self.assertEqual((fa.i1, fa.d1, fa.d2), (Decimal('12.50'), Decimal('14.00'), None))
        self.assertEqual(FicheNote.objects.get(eleve=b).d2, Decimal('8.75'))
        self.assertEqual(FicheNote.objects.count(), 2)  # pas de fiche vide pour le 3e élève

    def test_notes_invalides_refusees(self):
        self.client.force_login(utilisateur(roles.DIRECTION))
        a = self.eleves[0]
        r = self.client.post(reverse('notes:saisie', args=[self.ens_maths.pk]), {f'{a.pk}-i1': '21', f'{a.pk}-d1': 'abc'})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'note err', count=2)
        self.assertFalse(FicheNote.objects.exists())

    def test_professeur_limite_a_ses_cours(self):
        user = utilisateur(roles.PROFESSEUR, 'koffi')
        self.prof_maths.utilisateur = user
        self.prof_maths.save()
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse('notes:saisie', args=[self.ens_maths.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse('notes:saisie', args=[self.ens_fr.pk])).status_code, 403)
        r = self.client.get(reverse('notes:tableau'))
        self.assertContains(r, '6ème · Mathématiques')
        self.assertNotContains(r, '6ème · Français')
        # Pas d'accès à la scolarité ni aux autres professeurs
        self.assertEqual(self.client.get(reverse('ecole:eleves')).status_code, 403)
        self.assertEqual(self.client.get(reverse('profs:detail', args=[self.prof_fr.pk])).status_code, 403)
        self.assertRedirects(self.client.get(reverse('profs:liste')), reverse('profs:detail', args=[self.prof_maths.pk]))

    def test_resultats_et_bulletins_pdf(self):
        self.client.force_login(utilisateur(roles.SECRETARIAT))
        self.notes(self.eleves[0], self.ens_maths, i1=15, d1=12)
        url = f"{reverse('notes:resultats')}?etab={self.etab.pk}&classe=6ème&periode=1"
        r = self.client.get(url)
        self.assertContains(r, '13,50')
        r = self.client.get(f"{reverse('notes:bulletins_pdf')}?etab={self.etab.pk}&classe=6ème&periode=1")
        self.assertTrue(r.content.startswith(b'%PDF'))
        r = self.client.get(f"{reverse('notes:bulletins_pdf')}?etab={self.etab.pk}&classe=6ème&periode=1&eleve={self.eleves[0].pk}")
        self.assertTrue(r.content.startswith(b'%PDF'))

    def test_configuration_classe(self):
        self.client.force_login(utilisateur(roles.DIRECTION))
        svt = Matiere.objects.create(nom='SVT')
        url = f"{reverse('notes:classe_config')}?etab={self.etab.pk}&classe=6ème"
        self.client.post(url, {'matiere': svt.pk, 'coefficient': 2, 'professeur': ''})
        self.assertTrue(Enseignement.objects.filter(classe='6ème', matiere=svt, coefficient=2).exists())
        r = self.client.post(url, {'matiere': self.maths.pk, 'coefficient': 1})
        self.assertContains(r, 'déjà enseignée')
        # Copie vers une autre classe
        self.client.post(f"{reverse('notes:copier_config')}?etab={self.etab.pk}&classe=5ème",
                         {'source_etab': self.etab.pk, 'source_classe': '6ème'})
        self.assertEqual(Enseignement.objects.filter(classe='5ème').count(), 3)


class ProfesseursTests(Donnees):
    def test_solde_heures_et_paiements(self):
        self.client.force_login(utilisateur(roles.DIRECTION))
        self.client.post(reverse('profs:heure_ajouter', args=[self.prof_maths.pk]),
                         {'date': date.today().isoformat(), 'duree': '3', 'enseignement': self.ens_maths.pk})
        self.assertEqual(self.prof_maths.solde, 6000)
        r = self.client.post(reverse('profs:paiement_ajouter', args=[self.prof_maths.pk]),
                             {'montant': 4000, 'date': date.today().isoformat(), 'mode': 'especes', 'periode': 'Octobre'})
        p = PaiementProfesseur.objects.get()
        self.assertRedirects(r, reverse('profs:bon_pdf', args=[p.pk]), fetch_redirect_response=False)
        self.assertRegex(p.numero, r'^V\d{4}-\d{5}$')
        self.assertEqual(self.prof_maths.solde, 2000)
        self.assertTrue(self.client.get(reverse('profs:bon_pdf', args=[p.pk])).content.startswith(b'%PDF'))

    def test_caissier_paie_mais_ne_modifie_pas(self):
        self.client.force_login(utilisateur(roles.CAISSIER))
        self.assertEqual(self.client.get(reverse('profs:detail', args=[self.prof_maths.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse('profs:modifier', args=[self.prof_maths.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse('notes:tableau')).status_code, 403)

    def test_creer_compte_professeur(self):
        self.client.force_login(utilisateur(roles.DIRECTION))
        self.client.post(reverse('profs:compte_creer', args=[self.prof_fr.pk]), {'username': 'awa', 'mot_de_passe': 'Tres-Secret-2026'})
        self.prof_fr.refresh_from_db()
        self.assertEqual(self.prof_fr.utilisateur.username, 'awa')
        self.assertEqual(roles.role_de(self.prof_fr.utilisateur), roles.PROFESSEUR)

    def test_emploi_du_temps_conflits(self):
        Creneau.objects.create(enseignement=self.ens_maths, jour=1, heure_debut=time(8), heure_fin=time(10), salle='S1')
        c = Creneau(enseignement=self.ens_fr, jour=1, heure_debut=time(9), heure_fin=time(11))
        with self.assertRaisesMessage(ValidationError, 'La classe 6ème a déjà Mathématiques'):
            c.full_clean()
        autre = Enseignement.objects.create(etablissement=self.etab, classe='5ème', matiere=self.maths, professeur=self.prof_maths)
        with self.assertRaisesMessage(ValidationError, 'a déjà cours en 6ème'):
            Creneau(enseignement=autre, jour=1, heure_debut=time(9), heure_fin=time(10)).full_clean()
        Creneau(enseignement=self.ens_fr, jour=1, heure_debut=time(10), heure_fin=time(11)).full_clean()  # à la suite : OK

    def test_emploi_du_temps_pages(self):
        self.client.force_login(utilisateur(roles.SECRETARIAT))
        url = f"{reverse('profs:emploi_du_temps')}?etab={self.etab.pk}&classe=6ème"
        self.client.post(url, {'enseignement': self.ens_maths.pk, 'jour': 2, 'heure_debut': '08:00', 'heure_fin': '10:00', 'salle': 'S2'})
        self.assertEqual(Creneau.objects.count(), 1)
        self.assertContains(self.client.get(url), 'Mathématiques')
        self.assertTrue(self.client.get(url + '&format=pdf').content.startswith(b'%PDF'))
        self.assertTrue(self.client.get(f"{reverse('profs:emploi_du_temps')}?prof={self.prof_maths.pk}&format=pdf").content.startswith(b'%PDF'))


class ExamensTests(Donnees):
    def test_composition_epreuves_et_saisie(self):
        self.client.force_login(utilisateur(roles.DIRECTION))
        debut = date.today() + timedelta(days=7)
        r = self.client.post(reverse('examens:nouvelle'), {
            'etablissement': self.etab.pk, 'titre': 'Compositions du 1er trimestre', 'periode': 1, 'colonne': 'd2',
            'date_debut': debut.isoformat(), 'date_fin': (debut + timedelta(days=2)).isoformat(),
        })
        comp = Composition.objects.get()
        self.assertRedirects(r, reverse('examens:detail', args=[comp.pk]))
        donnees = {'enseignement': self.ens_maths.pk, 'date': debut.isoformat(), 'heure_debut': '08:00',
                   'heure_fin': '10:00', 'salle': 'S1', 'surveillants': [self.prof_fr.pk]}
        self.client.post(reverse('examens:detail', args=[comp.pk]), donnees)
        self.assertEqual(Epreuve.objects.count(), 1)
        # La même classe compose déjà à ce moment : refusé
        r = self.client.post(reverse('examens:detail', args=[comp.pk]), {**donnees, 'enseignement': self.ens_fr.pk, 'salle': 'S9'})
        self.assertContains(r, 'compose déjà')
        autre = Enseignement.objects.create(etablissement=self.etab, classe='5ème', matiere=self.fr)
        # Même salle au même moment (autre classe) : refusé
        r = self.client.post(reverse('examens:detail', args=[comp.pk]), {**donnees, 'enseignement': autre.pk})
        self.assertContains(r, 'déjà prise')
        # Surveillant déjà occupé : refusé
        r = self.client.post(reverse('examens:detail', args=[comp.pk]), {**donnees, 'enseignement': autre.pk, 'salle': 'S2'})
        self.assertContains(r, 'Déjà en surveillance')
        # Hors des dates de la composition : refusé
        r = self.client.post(reverse('examens:detail', args=[comp.pk]),
                             {**donnees, 'date': (debut + timedelta(days=5)).isoformat(), 'surveillants': []})
        self.assertContains(r, 'doit être comprise')
        # Les notes saisies en D2 font avancer la saisie de l'épreuve
        self.notes(self.eleves[0], self.ens_maths, periode=1, d2=11)
        self.assertEqual(Epreuve.objects.get().avancement_saisie(), (1, 3))
        self.assertTrue(self.client.get(reverse('examens:detail', args=[comp.pk]) + '?format=pdf').content.startswith(b'%PDF'))

    def test_professeur_voit_ses_surveillances(self):
        comp = Composition.objects.create(etablissement=self.etab, titre='Compo', periode=1,
                                          date_debut=date.today(), date_fin=date.today())
        e = Epreuve.objects.create(composition=comp, enseignement=self.ens_maths, date=date.today(),
                                   heure_debut=time(8), heure_fin=time(10))
        e.surveillants.add(self.prof_fr)
        user = utilisateur(roles.PROFESSEUR, 'awa')
        self.prof_fr.utilisateur = user
        self.prof_fr.save()
        self.client.force_login(user)
        r = self.client.get(reverse('examens:liste'))
        self.assertContains(r, 'Mes surveillances')
        self.assertContains(r, 'Mathématiques')
        self.assertEqual(self.client.get(reverse('examens:nouvelle')).status_code, 403)


class PortailTests(Donnees):
    def test_modules_selon_le_role(self):
        self.client.force_login(utilisateur(roles.DIRECTION))
        r = self.client.get(reverse('ecole:portail'))
        for nom in ('Scolarité', 'Notes et moyennes', 'Professeurs', 'Examens'):
            self.assertContains(r, nom)
        self.client.force_login(utilisateur(roles.CAISSIER))
        r = self.client.get(reverse('ecole:portail'))
        self.assertEqual([m.code for m, *_ in r.context['modules']], ['scolarite', 'profs'])
        prof_user = utilisateur(roles.PROFESSEUR, 'k')
        self.prof_maths.utilisateur = prof_user
        self.prof_maths.save()
        self.client.force_login(prof_user)
        r = self.client.get(reverse('ecole:portail'))
        self.assertEqual([m.code for m, *_ in r.context['modules']], ['notes', 'profs', 'examens'])

    def test_toutes_les_pages_repondent(self):
        self.client.force_login(utilisateur(roles.DIRECTION))
        pages = [reverse('notes:tableau'), reverse('notes:matieres'),
                 f"{reverse('notes:classe_config')}?etab={self.etab.pk}&classe=6ème",
                 reverse('notes:saisie', args=[self.ens_maths.pk]) + '?periode=2',
                 f"{reverse('notes:resultats')}?etab={self.etab.pk}&classe=6ème&periode=3",
                 reverse('profs:liste'), reverse('profs:nouveau'), reverse('profs:detail', args=[self.prof_maths.pk]),
                 reverse('profs:modifier', args=[self.prof_maths.pk]),
                 f"{reverse('profs:emploi_du_temps')}?etab={self.etab.pk}&classe=6ème",
                 reverse('examens:liste'), reverse('examens:nouvelle'), reverse('ecole:parametres')]
        for url in pages:
            self.assertEqual(self.client.get(url).status_code, 200, url)


class BarreLateraleTests(Donnees):
    """Chaque module a sa propre barre latérale, sans lien vers les pages des autres modules."""

    def _nav(self, url):
        html = self.client.get(url).content.decode()
        debut = html.index('<nav class="nav"')
        return html[debut:html.index('</nav>', debut)]

    def test_barre_propre_a_chaque_module(self):
        self.client.force_login(utilisateur(roles.DIRECTION))
        prefixes = {'scolarite': ['/scolarite/', '/eleves/', '/encaisser/', '/paiements/', '/suivi/', '/relances/', '/exports/',
                                  '/factures/', '/corrections/', '/messages/', '/grilles/'],
                    'notes': ['/notes/'], 'profs': ['/professeurs/'], 'examens': ['/examens/'],
                    'admin': ['/utilisateurs/', '/parametres/', '/admin/', '/grilles/', '/relances/regles/', '/etablissements/']}
        pages = {'scolarite': reverse('ecole:accueil'), 'notes': reverse('notes:tableau'), 'profs': reverse('profs:liste'),
                 'examens': reverse('examens:liste'), 'admin': reverse('ecole:parametres')}
        import re
        for module, url in pages.items():
            nav = self._nav(url)
            liens = re.findall(r'href="([^"#?]+)', nav)
            self.assertTrue(liens, module)
            for lien in liens:
                self.assertTrue(any(lien.startswith(p) for p in prefixes[module]),
                                f'La barre du module {module} contient un lien étranger : {lien}')
            # Le fil d'Ariane et la classe CSS indiquent bien le module courant
            self.assertIn(f'class="mod-{module}', self.client.get(url).content.decode())

    def test_portail_sans_barre_laterale(self):
        self.client.force_login(utilisateur(roles.DIRECTION))
        html = self.client.get(reverse('ecole:portail')).content.decode()
        self.assertNotIn('class="sidebar"', html)
        self.assertIn('module-tuile', html)


class LangueTests(Donnees):
    """Le sélecteur FR / EN change la langue de toute l'interface (pages, menus, PDF)."""

    def test_bascule_francais_anglais(self):
        self.client.force_login(utilisateur(roles.DIRECTION))
        r = self.client.get(reverse('ecole:accueil'))
        self.assertContains(r, 'Journal des paiements')
        self.assertContains(r, 'name="language" value="en"')
        r = self.client.post(reverse('set_language'), {'language': 'en', 'next': reverse('ecole:accueil')})
        self.assertRedirects(r, reverse('ecole:accueil'), fetch_redirect_response=False)
        r = self.client.get(reverse('ecole:accueil'))
        self.assertContains(r, 'Payment log')
        self.assertContains(r, 'lang="en"')
        self.assertNotContains(r, 'Journal des paiements')
        for url, texte in [(reverse('ecole:portail'), 'Open module'), (reverse('notes:tableau'), 'Grade entry by course'),
                           (reverse('profs:liste'), 'Teacher list'), (reverse('examens:liste'), 'All exams'),
                           (reverse('ecole:parametres'), 'General settings'), (reverse('ecole:eleve_nouveau'), 'Enrol a student')]:
            self.assertContains(self.client.get(url), texte, msg_prefix=url)
        # Libellés venant du code Python : trimestres, rangs, appréciations
        self.notes(self.eleves[0], self.ens_maths, i1=15, d1=15, d2=15)
        r = self.client.get(f"{reverse('notes:resultats')}?etab={self.etab.pk}&classe=6ème&periode=1")
        self.assertContains(r, '1st term')
        self.assertContains(r, 'Good')
        self.assertContains(r, '1st')
        # Retour au français
        self.client.post(reverse('set_language'), {'language': 'fr', 'next': '/'})
        self.assertContains(self.client.get(reverse('ecole:accueil')), 'Journal des paiements')

    def test_pdf_et_montant_en_lettres_en_anglais(self):
        from django.utils import translation

        from ecole.templatetags.ecole_extras import en_lettres
        with translation.override('en'):
            self.assertEqual(en_lettres(31666), 'Thirty-one thousand six hundred sixty-six CFA francs')
        with translation.override('fr'):
            self.assertEqual(en_lettres(31666), 'Trente et un mille six cent soixante-six francs CFA')
        self.client.force_login(utilisateur(roles.DIRECTION))
        self.client.post(reverse('set_language'), {'language': 'en', 'next': '/'})
        r = self.client.get(f"{reverse('notes:bulletins_pdf')}?etab={self.etab.pk}&classe=6ème&periode=1")
        self.assertTrue(r.content.startswith(b'%PDF'))
