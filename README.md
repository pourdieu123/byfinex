# By Finex — gestion scolaire

Application web Django (sans framework JavaScript), utilisable sur ordinateur et sur téléphone.
Après la connexion, l'accueil présente quatre modules : **Scolarité**, **Notes et moyennes**,
**Professeurs** et **Examens** (seuls ceux autorisés par le rôle de l'utilisateur apparaissent).
Le nom affiché se change dans `config/settings.py` (`APP_NOM`).
Interface en noir et blanc ; chaque module a sa propre barre latérale.

## Langues (français / anglais)

Un sélecteur **FR / EN** est présent sur la connexion, l'accueil et dans chaque module ; le choix est
mémorisé. Pages, menus, messages, PDF, exports Excel, dates et montants en lettres suivent la langue.

- Textes source (français) : dans les gabarits `{% translate "…" %}` et dans le code `_("…")`.
- Traductions anglaises : `locale/en/LC_MESSAGES/django.po` (lignes `msgstr`).
- Après une modification (sans outil externe) :

```powershell
.venv\Scripts\python manage.py traductions extraire   # ajoute les nouveaux textes au .po
.venv\Scripts\python manage.py traductions compiler   # génère le .mo utilisé par l'application
```

## Notes et moyennes (système béninois)

- Matières communes ; pour chaque classe : matières, coefficients et professeur (copie possible d'une classe à l'autre).
- Trimestres ou semestres au choix par établissement (Paramètres).
- Saisie en grille : 4 interrogations, 1er et 2e devoir. MI = moyenne des interrogations ;
  moyenne de la matière = (MI + D1 + D2) ÷ 3 sur les notes disponibles.
- Moyenne générale coefficientée, rangs (avec ex aequo), appréciations, moyenne annuelle à la dernière période.
- Bulletins PDF (un élève ou toute la classe).

## Professeurs

- Fiche, statut (permanent / vacataire), taux horaire, matières ; classes attribuées via Notes.
- Compte de connexion « Professeur » : il ne voit que ses cours, son emploi du temps, ses heures et ses surveillances.
- Heures pointées, paiements avec bon de paiement PDF, reste à verser.
- Emploi du temps par classe ou par professeur (conflits de classe, de professeur et de salle refusés), PDF.

## Examens (compositions internes)

- Session de compositions par établissement et par période ; notes reportées en D1 ou D2.
- Épreuves : classe, matière, date, horaire, salle, surveillants (conflits refusés), suivi de la saisie, planning PDF.

## Scolarité

### 1. Paramétrage (Administration)
- **Établissement** (Paramètres › « IFU, factures, fratrie ») : IFU, RCCM, groupe de taxation,
  activation e-MECeF (jeton DGI, environnement Test puis Production), réductions **fratrie** (% 2ᵉ enfant,
  % 3ᵉ et suivants — fratrie = même numéro de parent dans l'établissement).
- **Frais par classe** (module Scolarité › Frais par classe ; direction et secrétariat) : pour chaque classe,
  frais d'inscription et scolarité découpée en tranches (montant + date d'échéance), copie vers d'autres classes.
  À l'inscription, choisir la classe affiche et remplit automatiquement la scolarité et les frais d'inscription.
  Modifier les frais d'une classe met à jour l'échéancier de ses élèves.
- **Règles de relance** : quand prévenir (ex. 7 jours avant, le jour même, 3 jours de retard puis tous les 7 jours),
  par quels canaux (WhatsApp, SMS, e-mail) et avec quel message. Trois règles sont créées par défaut.

### 2. Élèves
- **Import Excel/CSV** (Élèves › Importer) pour faire entrer d'un coup des élèves qui ne sont pas encore dans
  la base, avec toutes leurs informations : identité (sexe, date et lieu de naissance, Educ Master, résidence),
  classe, parent principal et second parent (téléphone, WhatsApp, e-mail), frais, réductions (scolarité et/ou
  inscription, en % ou en montant), paiements déjà effectués (repris avec facture et affectés aux tranches),
  rappels. Le modèle téléchargeable contient une feuille « Mode d'emploi » et des listes déroulantes.
  Deux étapes : analyse et aperçu (rien n'est créé ; erreurs détaillées par ligne), puis confirmation.
  Les factures reprises ne sont envoyées aux parents que si on le demande.
- **Jamais deux fois la même information** : un élève déjà dans la base (archivés compris) apparaît **en bleu**
  « Déjà dans la base » avec la date et l'auteur de son import et un lien vers sa fiche ; il n'est pas recréé.
  Il est reconnu même si le nom et le prénom sont inversés ou écrits autrement (majuscules, accents), même sans
  établissement indiqué, ou par son n° Educ Master. Un fichier déjà importé (même contenu, quel que soit son nom)
  déclenche un avertissement « Ce fichier a déjà été importé le … par … », et un second clic sur « Importer »
  est refusé. Les doublons à l'intérieur d'un même fichier sont signalés et importés une seule fois.
- **Fiche élève** : identité, parent principal (téléphone, WhatsApp, e-mail) et autres parents,
  rappels automatiques activables, **échéancier** (personnalisable par la direction), réductions
  (fratrie automatique, bourses, remises — réparties sur les tranches restantes sans recalcul manuel),
  historique des paiements, des corrections et des messages, accès à l'espace parents.
- **Archivage (jamais de suppression)** : un élève qui part (transfert, abandon, fin de scolarité…) est
  **archivé** depuis sa fiche par la Direction, avec un motif obligatoire. Sa fiche, ses paiements, factures et
  messages sont conservés ; il n'apparaît plus dans les listes, le tableau de bord, les retards, les relances
  (manuelles et automatiques), les exports ni le rang de fratrie, et on ne peut plus encaisser pour lui.
  Élèves › Élèves archivés permet de le retrouver et de le **réactiver**. La suppression d'un élève n'existe
  plus nulle part (ni dans l'application, ni dans l'administration Django).

### 3. Encaissement et facturation
- **Tout fonctionne par tranches** : chaque élève a les tranches de sa classe (ou une tranche unique, échéance
  au 30 juin, tant que les frais de la classe ne sont pas définis). Chaque paiement de scolarité remplit la plus
  ancienne tranche non soldée ; le surplus passe à la tranche suivante. Une tranche n'est **soldée** que payée
  en totalité ; sinon elle est « partiellement payée » avec son reste. Statuts : à venir, en cours, partiellement
  payée, soldée, en retard (y compris « en retard — partiellement payée »).
- L'**affectation de chaque paiement** est enregistrée (tranches concernées, montant affecté à chacune, payé et
  reste sur la tranche, reste de la scolarité après le paiement) et affichée sur la fiche, la page du paiement
  et la facture. L'écran d'encaissement montre l'affectation avant validation. Elle est recalculée après une
  correction, une annulation, un paiement antidaté ou un changement d'échéancier, sans jamais modifier les montants
  versés. `manage.py recalculer_tranches` refait le calcul pour tous les élèves (sans danger).
- Chaque paiement a une **référence unique** (R2026-00001), un mode, un n° de transaction, une observation,
  et une **facture** (F2026-00001) envoyée immédiatement au parent (WhatsApp + e-mail avec le PDF, SMS résumé).
- Avec e-MECeF activé, la facture est **normalisée** (code MECeF/DGI, NIM, compteurs, QR code). Si le service
  ne répond pas, la facture est émise quand même et reprise plus tard (`manage.py normaliser_factures`).
- **Correction / annulation** : la direction corrige directement ; la caisse et le secrétariat envoient une
  demande que la direction valide (menu « Corrections »). Chaque correction est tracée (qui, quand, pourquoi,
  avant/après) ; l'ancienne facture est annulée par une **facture d'avoir** et une nouvelle facture est émise.

### 4. Échéances et retards
- Une tranche échue non réglée met l'élève **« en retard »** (badge, onglet « En retard », tuile du tableau de bord).
- `manage.py envoyer_relances` envoie chaque jour les rappels et relances selon les règles (jamais deux fois
  le même), uniquement aux élèves dont les rappels sont activés.

### 5. Espaces et rôles
- **Surveillant** : module « Contrôle des paiements », statut (à jour / en retard / soldé) des élèves de son
  périmètre (classes cochées sur sa fiche utilisateur), sans aucun montant.
- **Parent** : accès créé depuis la fiche élève (« Créer l'accès parent ») ; ses enfants (même numéro),
  échéancier, paiements, factures PDF. Il arrive directement dans son espace, sans code de module.

### 6. Temps réel et assistant
- Tableau de bord, fiche élève, retards, contrôle et espace parents se rechargent seuls dès qu'un paiement,
  une correction ou un élève change (vérification toutes les 10 secondes).
- **Assistant** pour les parents (WhatsApp, SMS, e-mail) : reconnaît le numéro ou l'e-mail du parent et répond
  à « 1 / SITUATION », « 2 / ECHEANCES », « 3 / FACTURE » (PDF joint), « 4 / HISTORIQUE », « MENU ».

### Autres fonctions
- Journal des paiements, exports Excel/PDF, relances manuelles SMS/WhatsApp, journal de tous les messages
  (envoyés, simulés, en échec, reçus), liste des factures avec leur statut e-MECeF.

## Envois aux parents (WhatsApp, SMS, e-mail)

### Relances par SMS avec une puce MTN ou Celtiis (le plus simple)

Les relances sont réglées en **SMS uniquement**, avec des messages courts (1 SMS = 160 caractères ; la page
« Règles de relance » indique combien de SMS coûte chaque règle). Les caractères qui doubleraient le prix
(ê, ç, ’, «, —…) sont remplacés automatiquement.

1. Sur un téléphone **Android** de l'école avec une puce **MTN ou Celtiis** (un forfait SMS est conseillé),
   installer l'application gratuite **« SMS Gateway for Android »** (sms-gate.app) et autoriser l'envoi de SMS.
2. Dans l'application, activer **« Local server »** (téléphone sur le même Wi-Fi que l'ordinateur) ou
   **« Cloud server »** (par Internet), puis **« Online »**. Noter l'adresse, l'utilisateur et le mot de passe.
3. Copier `reglages.env.exemple` en **`reglages.env`** et le compléter :
   `SMS_PASSERELLE_URL` (ex. `http://192.168.1.20:8080`, ou `https://api.sms-gate.app/3rdparty/v1` en mode Cloud),
   `SMS_PASSERELLE_UTILISATEUR`, `SMS_PASSERELLE_MOT_DE_PASSE`. Redémarrer FinMob.
4. Administration › Règles de relance › **« Envoyer un SMS d'essai »** à votre propre numéro.

Le coût est celui du forfait de la puce. Le téléphone doit rester allumé, chargé et connecté.

### Autres canaux

Sans configuration, les messages sont **simulés** : notés dans « Messages envoyés » mais pas envoyés.
Pour les envoyer réellement, définir ces réglages dans `reglages.env` (ou en variables d'environnement) :

| Canal | Variables |
|---|---|
| WhatsApp (API WhatsApp Cloud de Meta) | `WHATSAPP_JETON`, `WHATSAPP_NUMERO_ID`, `WHATSAPP_VERIFICATION` (jeton du webhook), `WHATSAPP_SECRET_APP` |
| SMS (téléphone passerelle) | `SMS_PASSERELLE_URL`, `SMS_PASSERELLE_UTILISATEUR`, `SMS_PASSERELLE_MOT_DE_PASSE` |
| SMS (Twilio) | `TWILIO_COMPTE`, `TWILIO_JETON`, `TWILIO_EXPEDITEUR` |
| E-mail (SMTP) | `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL` |
| Réponses aux e-mails (IMAP) | `IMAP_HOTE`, `IMAP_UTILISATEUR`, `IMAP_MOT_DE_PASSE` |

- Adresses à déclarer chez les fournisseurs (serveur accessible depuis Internet, en HTTPS) :
  `https://<domaine>/webhooks/whatsapp/` et `https://<domaine>/webhooks/sms/`.
- WhatsApp n'autorise un message libre que dans les 24 h qui suivent un message du parent ; pour les
  factures et rappels envoyés à l'initiative de l'école, faire valider des modèles de messages chez Meta.

Tâches à planifier (Planificateur de tâches Windows) :

```powershell
.venv\Scripts\python manage.py envoyer_relances      # chaque jour, ex. à 8 h
.venv\Scripts\python manage.py normaliser_factures   # chaque heure, si e-MECeF est activé
.venv\Scripts\python manage.py lire_emails           # toutes les 5 minutes, si l'IMAP est configuré
```

## Rôles

| Rôle | Droits |
|---|---|
| Direction | Tout, y compris suppression, annulation de paiement, utilisateurs et paramètres |
| Secrétariat | Élèves, relances, exports, matières, professeurs, emplois du temps, compositions, bulletins |
| Caissier | Encaisse, imprime les factures, consulte le journal, relance les parents, demande les corrections |
| Surveillant | Consulte le statut de paiement (à jour, en retard, soldé) des élèves de son périmètre |
| Parent | Espace parents : situation de ses enfants, échéancier, paiements et factures |
| Professeur | Saisit les notes de ses classes, voit son emploi du temps, ses heures et ses surveillances |

Le compte créé avec `createsuperuser` a automatiquement tous les droits (Direction).
Les autres comptes se créent dans **Administration › Utilisateurs**.

## Modules « Bientôt disponible »

Pour l'instant seule la **Scolarité** (et l'Administration) est ouverte : Notes, Professeurs et Examens
apparaissent grisés sur l'accueil avec la mention « Bientôt disponible » et leurs pages sont fermées.
Pour rouvrir un module, retirer son code de `MODULES_BIENTOT` dans `config/settings.py`.

## Codes d'accès des modules

Chaque utilisateur a **son propre code pour chaque module** de son rôle (Scolarité, Notes, Professeurs,
Examens, Administration). La direction les attribue dans **Administration › Utilisateurs › (le compte)**,
section « Codes d'accès aux modules » ; ils sont stockés chiffrés et peuvent être changés ou retirés.

- **La connexion** reste identifiant + mot de passe ; on arrive sur l'accueil avec les modules.
- **En cliquant sur un module**, la personne saisit son code pour ce module. Revenir à l'accueil ou passer
  dans un autre module le referme, ainsi que 30 minutes d'inactivité (`VERROU_MODULE_MINUTES` dans
  `config/settings.py`). Après 5 codes faux, la saisie est bloquée 5 minutes.
- Sans code pour un module, on n'y entre pas (« demandez-le à la direction »). Seule la direction,
  à sa première entrée dans un module, choisit elle-même son code.

```powershell
.venv\Scripts\python manage.py codes_modules                          # qui a un code pour quel module
.venv\Scripts\python manage.py codes_modules definir awa scolarite 4821
.venv\Scripts\python manage.py codes_modules retirer directeur admin   # code oublié
```

## Installation (une seule fois)

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python manage.py migrate
.venv\Scripts\python manage.py createsuperuser
```

## Lancer l'application

```powershell
.venv\Scripts\python manage.py runserver 0.0.0.0:8000
```

- Sur le PC : http://127.0.0.1:8000 (et non `0.0.0.0:8000`)
- Sur un téléphone connecté au même Wi-Fi : http://ADRESSE-IP-DU-PC:8000
  (adresse donnée par `ipconfig`, ligne « Adresse IPv4 »). Si la page ne s'ouvre pas,
  autorisez Python dans le pare-feu Windows (réseau privé).

## Après une mise à jour du code

```powershell
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python manage.py migrate
```

## Tests

```powershell
.venv\Scripts\python manage.py test ecole pedagogie
```

## Règle de calcul des paiements

Un élève est « soldé » quand ses paiements de scolarité ≥ montant total de son échéancier (après réductions).
Les frais d'inscription sont suivis à part. Imputation, réductions et retards : `ecole/echeancier.py` ;
factures, avoirs et corrections : `ecole/facturation.py` ; envois : `ecole/notifications.py`.

## Sécurité

- **Mode débogage coupé par défaut** (aucune page d'erreur ne montre le code). Pour développer : `DJANGO_DEBUG=1`.
- **Clé secrète** : `DJANGO_SECRET_KEY`, sinon une clé aléatoire est créée une fois dans `.cle_secrete` (à ne jamais partager).
- **Adresses autorisées** : ce PC et ses adresses sur le réseau local (accès depuis un téléphone du même Wi-Fi) ;
  en ligne, `DJANGO_ALLOWED_HOSTS=mon-domaine.com`.
- **Connexion** : mot de passe de 10 caractères minimum ; blocage de 15 minutes après 5 essais ratés (par identifiant
  et par adresse). Codes des modules : blocage de 5 minutes après 5 codes faux, même en se reconnectant.
- **Session** de 8 h, fermée avec le navigateur ; cookies illisibles par JavaScript.
- **En-têtes** : politique de contenu (CSP, seuls les scripts de l'application s'exécutent), anti-iframe, nosniff.
- **Webhooks** WhatsApp/SMS refusés tant que le canal n'est pas configuré, et signature du fournisseur obligatoire.
- Un compte Direction ne peut pas modifier un super-administrateur ; corrections validées une seule fois ;
  exports Excel protégés contre les formules ; messages modèles sans exécution de code.
- `manage.py check --deploy` ne doit plus signaler que l'HTTPS : il s'active en ligne avec `DJANGO_HTTPS=1`.

## Mise en production

Avant d'héberger l'application sur un serveur (en HTTPS), définir les variables d'environnement
`DJANGO_SECRET_KEY` (valeur secrète longue), `DJANGO_ALLOWED_HOSTS=mon-domaine.com` et `DJANGO_HTTPS=1`
(redirection HTTPS, cookies sécurisés, HSTS), plus `EMAIL_HOST`… pour l'envoi des e-mails. `FINMOB_DB` permet de choisir le fichier de base SQLite.
