r"""Lit les e-mails reçus des parents (IMAP) et y répond avec l'assistant de scolarité.

Réglages dans config/settings.py : NOTIFICATIONS['email'] = {'imap_hote': ..., 'imap_utilisateur': ..., 'imap_mot_de_passe': ...}
À lancer régulièrement (ex. toutes les 5 minutes) :  .venv\Scripts\python manage.py lire_emails
"""

import email
import imaplib
import ssl
from email.header import decode_header, make_header
from email.utils import parseaddr

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ecole.agent import traiter_message


def _texte(message):
    if message.is_multipart():
        for partie in message.walk():
            if partie.get_content_type() == 'text/plain' and 'attachment' not in str(partie.get('Content-Disposition', '')):
                return partie.get_payload(decode=True).decode(partie.get_content_charset() or 'utf-8', errors='replace')
        return ''
    return message.get_payload(decode=True).decode(message.get_content_charset() or 'utf-8', errors='replace')


class Command(BaseCommand):
    help = "Répond aux e-mails des parents (situation, échéances, factures)."

    def handle(self, *args, **options):
        conf = getattr(settings, 'NOTIFICATIONS', {}).get('email', {})
        if not conf.get('imap_hote'):
            raise CommandError("Boîte de réception non configurée (NOTIFICATIONS['email']['imap_hote']).")
        boite = imaplib.IMAP4_SSL(conf['imap_hote'], int(conf.get('imap_port', 993)),
                                  ssl_context=ssl.create_default_context(), timeout=30)
        boite.login(conf['imap_utilisateur'], conf['imap_mot_de_passe'])
        boite.select('INBOX')
        _statut, ids = boite.search(None, 'UNSEEN')
        traites = 0
        for num in ids[0].split():
            _statut, data = boite.fetch(num, '(RFC822)')
            message = email.message_from_bytes(data[0][1])
            expediteur = parseaddr(message.get('From', ''))[1]
            sujet = str(make_header(decode_header(message.get('Subject', ''))))
            if expediteur:
                traiter_message('email', expediteur, f'{sujet}\n{_texte(message)}'[:2000])
                traites += 1
            boite.store(num, '+FLAGS', r'\Seen')
        boite.logout()
        self.stdout.write(self.style.SUCCESS(f'{traites} e-mail(s) traité(s).'))
