"""Assistant conversationnel pour les parents (WhatsApp, SMS, e-mail).

Le parent est reconnu par son numéro (ou son e-mail) ; il écrit un mot-clé ou un chiffre :
  1 / SITUATION  : ce qui est versé, ce qui reste, retard éventuel, prochaine échéance ;
  2 / ECHEANCES  : l'échéancier de chaque enfant ;
  3 / FACTURE    : la dernière facture (en pièce jointe sur WhatsApp et par e-mail) ;
  4 / HISTORIQUE : les derniers paiements ;
  AIDE / MENU    : la liste des commandes.
"""

import re
import unicodedata

from django.utils.translation import gettext as _

from .models import ContactParent, Eleve, Facture, TypeFacture, numero_international
from .templatetags.ecole_extras import fcfa


def _simplifier(texte):
    texte = unicodedata.normalize('NFKD', texte or '').encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9 ]', ' ', texte).strip()


def enfants_de(identifiant):
    """Élèves rattachés à un numéro de téléphone (format international) ou à une adresse e-mail."""
    identifiant = (identifiant or '').strip()
    if '@' in identifiant:
        email = identifiant.lower()
        ids = set(Eleve.objects.filter(email__iexact=email).values_list('pk', flat=True))
        ids |= set(ContactParent.objects.filter(email__iexact=email).values_list('eleve_id', flat=True))
    else:
        numero = numero_international(identifiant)
        if not numero:
            return []
        ids = {e.pk for e in Eleve.objects.only('pk', 'telephone', 'whatsapp')
               if numero in (e.telephone_international, numero_international(e.whatsapp))}
        ids |= {c.eleve_id for c in ContactParent.objects.only('eleve_id', 'telephone', 'whatsapp')
                if numero in (numero_international(c.telephone), numero_international(c.whatsapp))}
    return list(Eleve.objects.filter(pk__in=ids).avec_totaux().select_related('etablissement')
                .prefetch_related('echeances').order_by('nom', 'prenom'))


COMMANDES = {
    'situation': ('1', 'situation', 'solde', 'reste', 'combien', 'dette'),
    'echeances': ('2', 'echeance', 'echeances', 'echeancier', 'tranche', 'tranches', 'date', 'dates'),
    'facture': ('3', 'facture', 'factures', 'recu', 'recus'),
    'historique': ('4', 'historique', 'paiement', 'paiements', 'verse', 'versements'),
}


def intention(texte):
    mots = _simplifier(texte).split()
    for commande, cles in COMMANDES.items():
        if any(m in cles for m in mots):
            return commande
    return 'menu'


def _menu():
    return _('Répondez par :\n1 - Situation (reste à payer)\n2 - Échéances\n3 - Dernière facture\n4 - Historique des paiements')


def _situation(eleve):
    sit = eleve.situation
    lignes = [f'{eleve} ({eleve.classe}) : ' + _('versé %(verse)s, reste %(reste)s.') % {
        'verse': fcfa(eleve.total_verse), 'reste': fcfa(eleve.reste_a_payer)}]
    if sit.en_retard:
        lignes.append(_('En retard : %(montant)s (depuis %(jours)s jour(s)).') % {
            'montant': fcfa(sit.montant_en_retard), 'jours': sit.jours_de_retard})
    elif sit.prochaine:
        lignes.append(_('Prochaine échéance : %(montant)s le %(date)s.') % {
            'montant': fcfa(sit.prochaine.reste), 'date': sit.prochaine.echeance.echeance.strftime('%d/%m/%Y')})
    elif not eleve.reste_a_payer:
        lignes.append(_('Scolarité soldée. Merci !'))
    return '\n'.join(lignes)


def _echeances(eleve):
    sit = eleve.situation
    if not sit.lignes:
        return f'{eleve} : ' + _('pas d’échéancier défini, reste %(reste)s.') % {'reste': fcfa(eleve.reste_a_payer)}
    lignes = [f'{eleve} ({eleve.classe})']
    for l in sit.lignes:
        lignes.append(f'- {l.echeance} · {l.echeance.echeance:%d/%m/%Y} · {fcfa(l.montant)} · {l.statut_libelle}'
                      + (f' ({_("reste")} {fcfa(l.reste)})' if l.reste and l.paye else ''))
    return '\n'.join(lignes)


def _historique(eleve):
    paiements = list(eleve.paiements.order_by('-date', '-cree_le')[:5])
    if not paiements:
        return f'{eleve} : ' + _('aucun paiement enregistré.')
    return '\n'.join([f'{eleve} :'] + [f'- {p.date:%d/%m/%Y} · {fcfa(p.montant)} · {p.numero_recu}' for p in paiements])


def repondre(identifiant, texte):
    """Renvoie (réponse, factures_à_joindre, élèves) pour un message reçu."""
    eleves = enfants_de(identifiant)
    if not eleves:
        return (_('Bonjour. Ce numéro n’est rattaché à aucun élève. Merci de contacter l’établissement '
                  'pour mettre à jour vos coordonnées.'), [], [])
    commande = intention(texte)
    factures = []
    if commande == 'situation':
        corps = '\n\n'.join(_situation(e) for e in eleves)
    elif commande == 'echeances':
        corps = '\n\n'.join(_echeances(e) for e in eleves)
    elif commande == 'historique':
        corps = '\n\n'.join(_historique(e) for e in eleves)
    elif commande == 'facture':
        for e in eleves:
            f = (Facture.objects.filter(eleve=e, type=TypeFacture.VENTE, annulee=False).order_by('-date').first())
            if f:
                factures.append(f)
        if factures:
            corps = '\n'.join(_('Facture %(numero)s du %(date)s : %(montant)s (%(eleve)s).') % {
                'numero': f.numero, 'date': f.date.strftime('%d/%m/%Y'), 'montant': fcfa(f.montant), 'eleve': f.eleve}
                for f in factures)
        else:
            corps = _('Aucune facture pour le moment.')
    else:
        noms = ', '.join(e.prenom for e in eleves)
        corps = _('Bonjour ! Assistant scolarité de %(ecole)s (%(noms)s).') % {
            'ecole': eleves[0].etablissement.nom, 'noms': noms} + '\n' + _menu()
        return corps, [], eleves
    return corps + '\n\n' + _('Tapez MENU pour les autres options.'), factures, eleves


def traiter_message(canal, expediteur, texte):
    """Note le message reçu, prépare la réponse, l'envoie (avec les factures) et la note au journal.

    Renvoie le texte de la réponse (utile pour les SMS, où la réponse part dans la réponse HTTP)."""
    from .models import Notification, StatutMessage, TypeMessage
    from .notifications import transmettre
    from .pdf import pdf_facture

    reponse, factures, eleves = repondre(expediteur, texte)
    eleve = eleves[0] if eleves else None
    expediteur = expediteur.strip() if canal == 'email' else (numero_international(expediteur) or expediteur)
    Notification.objects.create(eleve=eleve, canal=canal, type=TypeMessage.QUESTION, destinataire=expediteur,
                                message=texte[:2000], statut=StatutMessage.RECU)
    envois = [(reponse, None, None)]
    if canal != 'sms':
        envois += [(_('Facture %(numero)s') % {'numero': f.numero}, f,
                    [(f'facture-{f.numero}.pdf', pdf_facture(f), 'application/pdf')]) for f in factures]
    if canal == 'sms':
        Notification.objects.create(eleve=eleve, canal=canal, type=TypeMessage.REPONSE, destinataire=expediteur,
                                    message=reponse, statut=StatutMessage.ENVOYE)
        return reponse
    for message, facture, pieces in envois:
        statut, erreur = StatutMessage.ECHEC, ''
        try:
            statut = transmettre(canal, expediteur, message, objet=_('Votre scolarité'), pieces=pieces)
        except Exception as e:  # noqa: BLE001 — noté au journal
            erreur = str(e)[:1000]
        Notification.objects.create(eleve=eleve, facture=facture, canal=canal, type=TypeMessage.REPONSE,
                                    destinataire=expediteur, message=message, statut=statut, erreur=erreur)
    return reponse
