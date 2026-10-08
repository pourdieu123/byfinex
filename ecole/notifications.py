"""Envoi des messages aux parents (WhatsApp, SMS, e-mail) et rappels automatiques d'échéances.

Les canaux se règlent dans ``settings.NOTIFICATIONS`` (voir config/settings.py) :
- WhatsApp : « meta » (API WhatsApp Cloud de Meta) ou « console » (simulé, rien n'est envoyé) ;
- SMS : « android » (téléphone de l'école avec l'application « SMS Gateway for Android » et une puce
  MTN ou Celtiis), « twilio » ou « console » ;
- e-mail : « django » (configuration e-mail de Django) ou « console ».
Chaque message, envoyé, simulé ou en échec, est noté dans le journal des messages (modèle Notification).
"""

import base64
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMessage
from django.utils import timezone
from django.utils.translation import gettext as _

from .echeancier import contacts_du_parent
from .models import (
    CanalMessage, Eleve, MomentRelance, Notification, RegleRelance, StatutMessage, TypeMessage,
)
from .templatetags.ecole_extras import fcfa

log = logging.getLogger(__name__)


class ErreurEnvoi(Exception):
    pass


def _config(canal):
    return getattr(settings, 'NOTIFICATIONS', {}).get(canal, {}) or {}


def canal_configure(canal):
    return _config(canal).get('backend', 'console') != 'console'


def fournisseur_sms():
    return {'android': _('Téléphone passerelle (puce MTN / Celtiis)'), 'twilio': 'Twilio'}.get(
        _config('sms').get('backend', 'console'), '')


# --------------------------------------------------------------------------- Transports

def _post(url, donnees, entetes, delai=20):
    requete = urllib.request.Request(url, data=donnees, method='POST', headers=entetes)
    try:
        with urllib.request.urlopen(requete, timeout=delai) as reponse:
            contenu = reponse.read().decode() or '{}'
    except urllib.error.HTTPError as e:
        raise ErreurEnvoi(f'HTTP {e.code} : {e.read().decode(errors="replace")[:300]}') from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ErreurEnvoi(str(e)) from e
    try:
        return json.loads(contenu)
    except ValueError:
        return {}


def _whatsapp_meta(conf, numero, message, pieces):
    version = conf.get('version', 'v21.0')
    base = f"https://graph.facebook.com/{version}/{conf['numero_id']}"
    auth = {'Authorization': f"Bearer {conf['jeton']}"}
    if pieces:
        nom, contenu, type_mime = pieces[0]
        limite = uuid.uuid4().hex
        corps = b''
        for cle, valeur in (('messaging_product', 'whatsapp'), ('type', type_mime)):
            corps += f'--{limite}\r\nContent-Disposition: form-data; name="{cle}"\r\n\r\n{valeur}\r\n'.encode()
        corps += (f'--{limite}\r\nContent-Disposition: form-data; name="file"; filename="{nom}"\r\n'
                  f'Content-Type: {type_mime}\r\n\r\n').encode() + contenu + f'\r\n--{limite}--\r\n'.encode()
        media = _post(f'{base}/media', corps, {**auth, 'Content-Type': f'multipart/form-data; boundary={limite}'})
        if not media.get('id'):
            raise ErreurEnvoi(f'Envoi du document refusé : {media}')
        donnees = {'messaging_product': 'whatsapp', 'to': numero, 'type': 'document',
                   'document': {'id': media['id'], 'filename': nom, 'caption': message[:1000]}}
    else:
        donnees = {'messaging_product': 'whatsapp', 'to': numero, 'type': 'text', 'text': {'body': message[:4096]}}
    _post(f'{base}/messages', json.dumps(donnees).encode(), {**auth, 'Content-Type': 'application/json'})


def _sms_twilio(conf, numero, message):
    url = f"https://api.twilio.com/2010-04-01/Accounts/{conf['compte']}/Messages.json"
    jeton = base64.b64encode(f"{conf['compte']}:{conf['jeton']}".encode()).decode()
    donnees = urllib.parse.urlencode({'To': f'+{numero}', 'From': conf['expediteur'], 'Body': message[:1500]}).encode()
    _post(url, donnees, {'Authorization': f'Basic {jeton}', 'Content-Type': 'application/x-www-form-urlencoded'})


# Alphabet GSM 7 bits : un SMS fait 160 caractères ; un seul caractère hors alphabet (ê, ç, ’, « …)
# le fait passer à 70 caractères et multiplie le coût. On remplace donc ces caractères par leur équivalent.
GSM = set('@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !"#¤%&\'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§'
          '¿abcdefghijklmnopqrstuvwxyzäöñüà')
GSM_ETENDU = set('^{}\\[~]|€')
EQUIVALENTS = {'ê': 'e', 'ë': 'e', 'â': 'a', 'ä': 'a', 'î': 'i', 'ï': 'i', 'ô': 'o', 'û': 'u', 'ü': 'u', 'ç': 'c',
               'È': 'E', 'Ê': 'E', 'À': 'A', 'Â': 'A', 'Î': 'I', 'Ô': 'O', 'Û': 'U', 'Ù': 'U',
               '’': "'", '‘': "'", '«': '"', '»': '"', '“': '"', '”': '"', '—': '-', '–': '-', '…': '...',
               '\u202f': ' ', '\xa0': ' ', '\u2009': ' ', '°': 'o'}


def texte_sms(message):
    """Texte prêt pour un SMS : alphabet GSM (160 caractères par SMS), espaces normalisés."""
    texte = ''.join(EQUIVALENTS.get(c, c) for c in message)
    texte = ''.join(c if c in GSM or c in GSM_ETENDU else '?' if ord(c) > 127 else c for c in texte)
    return ' '.join(texte.split(' ')).strip()


def nombre_sms(texte):
    """Nombre de SMS facturés pour ce texte (160 caractères, ou 153 par morceau s'il est long)."""
    longueur = sum(2 if c in GSM_ETENDU else 1 for c in texte)
    return 1 if longueur <= 160 else -(-longueur // 153)


def _sms_android(conf, numero, message):
    """Application « SMS Gateway for Android » (sms-gate.app) : en local (http://<ip-du-téléphone>:8080)
    ou via son relais en ligne (https://api.sms-gate.app/3rdparty/v1)."""
    url = conf['passerelle_url'].rstrip('/') + '/message'
    jeton = base64.b64encode(f"{conf['passerelle_utilisateur']}:{conf['passerelle_mot_de_passe']}".encode()).decode()
    donnees = json.dumps({'message': message, 'phoneNumbers': [f'+{numero}']}).encode()
    _post(url, donnees, {'Authorization': f'Basic {jeton}', 'Content-Type': 'application/json'})


def transmettre(canal, destinataire, message, objet='', pieces=None):
    """Envoie réellement le message ; renvoie le statut ('envoye' ou 'simule') ou lève ErreurEnvoi."""
    conf = _config(canal)
    backend = conf.get('backend', 'django' if canal == CanalMessage.EMAIL else 'console')
    if canal == CanalMessage.SMS:
        message = texte_sms(message)
    if backend == 'console':
        log.info('[%s simulé] %s : %s', canal, destinataire, message)
        return StatutMessage.SIMULE
    if canal == CanalMessage.WHATSAPP and backend == 'meta':
        _whatsapp_meta(conf, destinataire, message, pieces)
    elif canal == CanalMessage.SMS and backend == 'android':
        _sms_android(conf, destinataire, message)
    elif canal == CanalMessage.SMS and backend == 'twilio':
        _sms_twilio(conf, destinataire, message)
    elif canal == CanalMessage.EMAIL and backend == 'django':
        email = EmailMessage(objet or settings.APP_NOM, message, to=[destinataire])
        for nom, contenu, type_mime in pieces or []:
            email.attach(nom, contenu, type_mime)
        email.send()
    else:
        raise ErreurEnvoi(f'Canal {canal} : backend « {backend} » inconnu.')
    return StatutMessage.ENVOYE


def notifier(eleve, type_message, message, objet='', canaux=('whatsapp', 'sms', 'email'), facture=None,
             cle='', pieces=None, destinataires=None):
    """Envoie un message aux contacts du parent sur les canaux demandés et le note au journal."""
    destinataires = destinataires or contacts_du_parent(eleve)
    journal = []
    for canal in canaux:
        for dest in destinataires.get(canal, []):
            statut, erreur = StatutMessage.ECHEC, ''
            try:
                statut = transmettre(canal, dest, message, objet,
                                     pieces if canal in (CanalMessage.WHATSAPP, CanalMessage.EMAIL) else None)
            except Exception as e:  # noqa: BLE001 — l'erreur est notée au journal
                erreur = str(e)[:1000]
                log.warning('Envoi %s à %s impossible : %s', canal, dest, e)
            journal.append(Notification.objects.create(
                eleve=eleve, facture=facture, canal=canal, type=type_message, destinataire=dest,
                objet=objet, message=message, statut=statut, erreur=erreur, cle=cle,
            ))
    return journal


# --------------------------------------------------------------------------- Messages types

def remplir(modele, **valeurs):
    from .models import remplacer_variables

    return remplacer_variables(modele, **valeurs)


def envoyer_facture(facture):
    from .pdf import pdf_facture

    eleve = facture.eleve
    if facture.est_avoir:
        texte = _("Bonjour %(parent)s, la facture %(origine)s de %(montant)s a été annulée par l'avoir %(numero)s "
                  "(correction d'un paiement de %(eleve)s). — %(ecole)s")
    else:
        texte = _('Bonjour %(parent)s, nous avons bien reçu %(montant)s pour la scolarité de %(eleve)s (%(classe)s). '
                  'Facture n° %(numero)s. Reste à payer : %(reste)s. — %(ecole)s')
    eleve.refresh_from_db()
    message = texte % {
        'parent': eleve.parent, 'montant': fcfa(facture.montant), 'eleve': f'{eleve.prenom} {eleve.nom.upper()}',
        'classe': eleve.classe, 'numero': facture.numero, 'reste': fcfa(eleve.reste_a_payer),
        'ecole': eleve.etablissement.nom,
        'origine': facture.facture_origine.numero if facture.facture_origine_id else '',
    }
    objet = _('Facture %(numero)s — %(ecole)s') % {'numero': facture.numero, 'ecole': eleve.etablissement.nom}
    pieces = [(f'facture-{facture.numero}.pdf', pdf_facture(facture), 'application/pdf')]
    return notifier(eleve, TypeMessage.FACTURE, message, objet, facture=facture, pieces=pieces,
                    cle=f'facture:{facture.pk}')


def message_relance(regle, eleve, ligne, situation, aujourdhui):
    return remplir(
        regle.message,
        parent=eleve.parent, eleve=f'{eleve.prenom} {eleve.nom.upper()}', classe=eleve.classe,
        tranche=str(ligne.echeance), montant=fcfa(ligne.reste),
        echeance=ligne.echeance.echeance.strftime('%d/%m/%Y'), reste=fcfa(situation.reste),
        retard=str(max(0, (aujourdhui - ligne.echeance.echeance).days)), ecole=eleve.etablissement.nom,
    )


def _regle_concerne(regle, date_echeance, aujourdhui):
    if regle.moment == MomentRelance.AVANT:
        return date_echeance - timedelta(days=regle.jours) == aujourdhui
    if regle.moment == MomentRelance.JOUR:
        return date_echeance == aujourdhui
    premier = date_echeance + timedelta(days=max(regle.jours, 1))
    if aujourdhui < premier:
        return False
    if regle.repeter_tous_les:
        return (aujourdhui - premier).days % regle.repeter_tous_les == 0
    return aujourdhui == premier


def executer_relances(aujourdhui=None):
    """Envoie les rappels et relances prévus aujourd'hui (à lancer une fois par jour). Renvoie le nombre d'envois."""
    aujourdhui = aujourdhui or timezone.localdate()
    regles = list(RegleRelance.objects.filter(actif=True))
    if not regles:
        return 0
    envois = 0
    eleves = (Eleve.objects.actifs().filter(suivi_actif=True, echeances__isnull=False).distinct()
              .avec_totaux().select_related('etablissement').prefetch_related('echeances', 'contacts'))
    for eleve in eleves:
        sit = eleve.situation
        for ligne in sit.lignes:
            if not ligne.reste:
                continue
            for regle in regles:
                if regle.etablissement_id and regle.etablissement_id != eleve.etablissement_id:
                    continue
                if not _regle_concerne(regle, ligne.echeance.echeance, aujourdhui):
                    continue
                cle = f'relance:{regle.pk}:{ligne.echeance.pk}:{aujourdhui.isoformat()}'
                if Notification.objects.filter(cle=cle).exists():
                    continue
                type_message = TypeMessage.RETARD if regle.moment == MomentRelance.APRES else TypeMessage.RAPPEL
                objet = (_('Retard de paiement — %(eleve)s') if type_message == TypeMessage.RETARD
                         else _('Rappel d’échéance — %(eleve)s')) % {'eleve': eleve}
                envois += len(notifier(eleve, type_message, message_relance(regle, eleve, ligne, sit, aujourdhui),
                                       objet, canaux=regle.canaux, cle=cle))
    return envois
