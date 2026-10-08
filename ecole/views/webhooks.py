"""Points d'entrée des messages des parents vers l'assistant (WhatsApp Cloud API de Meta, SMS Twilio).

URL à déclarer chez le fournisseur :
  WhatsApp : https://<domaine>/webhooks/whatsapp/  (jeton de vérification : NOTIFICATIONS['whatsapp']['verification'])
  SMS      : https://<domaine>/webhooks/sms/
"""

import base64
import hashlib
import hmac
import json
import logging
from xml.sax.saxutils import escape

from django.conf import settings
from django.http import HttpResponse, HttpResponseForbidden
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from ..agent import traiter_message

log = logging.getLogger(__name__)


def _conf(canal):
    return getattr(settings, 'NOTIFICATIONS', {}).get(canal, {}) or {}


@csrf_exempt
@require_http_methods(['GET', 'POST'])
def whatsapp(request):
    conf = _conf('whatsapp')
    if conf.get('backend') != 'meta' or not conf.get('verification') or not conf.get('secret_app'):
        return HttpResponseForbidden()  # canal non configuré : aucune réponse automatique
    if request.method == 'GET':  # vérification de l'URL par Meta
        if (request.GET.get('hub.mode') == 'subscribe' and conf.get('verification')
                and request.GET.get('hub.verify_token') == conf['verification']):
            return HttpResponse(request.GET.get('hub.challenge', ''))
        return HttpResponseForbidden()
    attendu = 'sha256=' + hmac.new(conf['secret_app'].encode(), request.body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(attendu, request.headers.get('X-Hub-Signature-256', '')):
        return HttpResponseForbidden()
    try:
        donnees = json.loads(request.body or b'{}')
    except ValueError:
        return HttpResponse(status=400)
    for entree in donnees.get('entry', []):
        for changement in entree.get('changes', []):
            for message in changement.get('value', {}).get('messages', []):
                texte = (message.get('text') or {}).get('body') or (message.get('button') or {}).get('text', '')
                if message.get('from'):
                    try:
                        traiter_message('whatsapp', '+' + message['from'], texte or '')
                    except Exception:  # noqa: BLE001 — Meta renverrait le message en boucle
                        log.exception('Réponse WhatsApp impossible')
    return HttpResponse('ok')


def _signature_twilio_valide(request, jeton):
    url = request.build_absolute_uri()
    donnees = url + ''.join(f'{k}{v}' for k, v in sorted(request.POST.items()))
    attendu = base64.b64encode(hmac.new(jeton.encode(), donnees.encode(), hashlib.sha1).digest()).decode()
    return hmac.compare_digest(attendu, request.headers.get('X-Twilio-Signature', ''))


@csrf_exempt
@require_http_methods(['POST'])
def sms(request):
    conf = _conf('sms')
    # La réponse part dans la réponse HTTP : sans signature Twilio valide, n'importe qui pourrait
    # se faire passer pour un parent et lire sa situation.
    if conf.get('backend') != 'twilio' or not conf.get('jeton') or not _signature_twilio_valide(request, conf['jeton']):
        return HttpResponseForbidden()
    expediteur, texte = request.POST.get('From', ''), request.POST.get('Body', '')
    reponse = traiter_message('sms', expediteur, texte) if expediteur else ''
    return HttpResponse(f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{escape(reponse)}</Message></Response>',
                        content_type='text/xml')
