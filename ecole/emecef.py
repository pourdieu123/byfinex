"""Client de l'API e-MECeF de la DGI (Bénin) pour normaliser les factures.

Déroulé : 1) POST /invoice (demande de facture) -> uid ; 2) PUT /invoice/<uid>/confirm -> code MECeF/DGI,
QR code, compteurs, NIM. Une facture d'avoir (type « FA ») porte en « reference » le code MECeF de la
facture corrigée. Le jeton (JWT) est délivré par la DGI pour l'IFU de l'établissement.

À valider d'abord avec l'environnement de test de la DGI (Établissement › environnement « Test »).
"""

import json
import urllib.error
import urllib.request

from django.conf import settings

URLS = {
    'test': 'https://developper.impots.bj/sygmef-emcf/api',
    'production': 'https://sygmef.impots.bj/emcf/api',
}

# Modes de paiement de l'application -> codes e-MECeF
MODES = {
    'especes': 'ESPECES',
    'mobile_money': 'MOBILEMONEY',
    'virement': 'VIREMENT',
    'cheque': 'CHEQUES',
}


class ErreurEmecef(Exception):
    pass


def _url(etablissement):
    urls = getattr(settings, 'EMECEF_URLS', URLS)
    return urls.get(etablissement.emecef_mode, URLS['test']).rstrip('/')


def _appel(methode, url, jeton, corps=None, delai=20):
    donnees = json.dumps(corps).encode() if corps is not None else None
    requete = urllib.request.Request(url, data=donnees, method=methode, headers={
        'Authorization': f'Bearer {jeton}', 'Content-Type': 'application/json', 'Accept': 'application/json',
    })
    try:
        with urllib.request.urlopen(requete, timeout=delai) as reponse:
            contenu = reponse.read().decode() or '{}'
    except urllib.error.HTTPError as e:
        raise ErreurEmecef(f'HTTP {e.code} : {e.read().decode(errors="replace")[:300]}') from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ErreurEmecef(f'Service e-MECeF injoignable : {e}') from e
    try:
        resultat = json.loads(contenu)
    except ValueError as e:
        raise ErreurEmecef(f'Réponse illisible : {contenu[:200]}') from e
    if isinstance(resultat, dict) and resultat.get('errorCode') not in (None, '', '0', 0):
        raise ErreurEmecef(f"{resultat.get('errorCode')} : {resultat.get('errorDesc', '')}")
    return resultat


def normaliser(facture, operateur=''):
    """Envoie la facture à e-MECeF et renvoie les champs à enregistrer (code, QR, compteurs, NIM, date)."""
    etab = facture.eleve.etablissement
    if not (etab.ifu and etab.emecef_jeton):
        raise ErreurEmecef("IFU ou jeton e-MECeF manquant (Administration › Établissement).")
    corps = {
        'ifu': etab.ifu,
        'type': facture.type,
        'items': [{'name': l['libelle'][:120], 'price': int(l['montant']), 'quantity': 1,
                   'taxGroup': facture.groupe_taxe} for l in facture.lignes],
        'client': {'name': facture.client_nom[:100], 'contact': facture.client_contact},
        'operator': {'name': (operateur or 'Caisse')[:60]},
        'payment': [{'name': MODES.get(facture.mode, 'AUTRE'), 'amount': int(facture.montant)}],
    }
    if facture.facture_origine_id:
        corps['reference'] = facture.facture_origine.code_mecef
    base = _url(etab)
    demande = _appel('POST', f'{base}/invoice', etab.emecef_jeton, corps)
    uid = demande.get('uid')
    if not uid:
        raise ErreurEmecef(f'Pas d’identifiant de facture dans la réponse : {demande}')
    confirmation = _appel('PUT', f'{base}/invoice/{uid}/confirm', etab.emecef_jeton)
    return {
        'emecef_uid': uid,
        'code_mecef': confirmation.get('codeMECeFDGI', ''),
        'qr_code': confirmation.get('qrCode', ''),
        'compteurs': confirmation.get('counters', ''),
        'nim': confirmation.get('nim', ''),
        'date_mecef': confirmation.get('dateTime', ''),
    }
