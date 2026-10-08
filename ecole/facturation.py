"""Encaissement, facture par paiement, corrections tracées (avoir + nouvelle facture), annulations."""

import logging

from django.db import transaction
from django.utils import timezone

from . import emecef
from .echeancier import appliquer_reductions, imputation_paiement
from .models import (
    ActionCorrection, Facture, HistoriquePaiement, StatutFacture, TypeFacture,
)

log = logging.getLogger(__name__)

CHAMPS_MODIFIABLES = ['montant', 'mode', 'date', 'reference', 'observation']


def _nom(user):
    return (user.get_full_name() or user.username) if user else ''


def _instantane(paiement):
    return {
        'montant': paiement.montant, 'mode': paiement.mode, 'date': paiement.date.isoformat(),
        'reference': paiement.reference, 'observation': paiement.observation, 'motif': paiement.motif,
    }


def normaliser_facture(facture, user=None):
    """Tente la normalisation e-MECeF ; sans e-MECeF actif, la facture reste « non normalisée »."""
    etab = facture.eleve.etablissement
    if not etab.emecef_actif:
        return facture
    try:
        champs = emecef.normaliser(facture, operateur=_nom(user or facture.emise_par))
    except emecef.ErreurEmecef as e:
        facture.statut, facture.erreur = StatutFacture.ERREUR, str(e)
        facture.save(update_fields=['statut', 'erreur'])
        log.warning('Normalisation e-MECeF impossible pour %s : %s', facture.numero, e)
        return facture
    for nom, valeur in champs.items():
        setattr(facture, nom, valeur)
    facture.statut, facture.erreur = StatutFacture.NORMALISEE, ''
    facture.save()
    return facture


def emettre_facture(paiement, user=None):
    eleve = paiement.eleve
    etab = eleve.etablissement
    facture = Facture.objects.create(
        eleve=eleve, paiement=paiement, type=TypeFacture.VENTE, montant=paiement.montant, mode=paiement.mode,
        lignes=[{'libelle': str(l), 'montant': m} for l, m in imputation_paiement(paiement)],
        client_nom=eleve.parent, client_contact=eleve.telephone, ifu=etab.ifu, groupe_taxe=etab.groupe_taxe,
        emise_par=user,
    )
    return normaliser_facture(facture, user)


def emettre_avoir(facture, user=None):
    """Annule une facture par une facture d'avoir du même montant."""
    avoir = Facture.objects.create(
        eleve=facture.eleve, paiement=facture.paiement, type=TypeFacture.AVOIR, montant=facture.montant,
        mode=facture.mode, lignes=facture.lignes, client_nom=facture.client_nom,
        client_contact=facture.client_contact, ifu=facture.ifu, groupe_taxe=facture.groupe_taxe,
        facture_origine=facture, emise_par=user,
    )
    facture.annulee = True
    facture.save(update_fields=['annulee'])
    return normaliser_facture(avoir, user)


def facture_active(paiement):
    return paiement.factures.filter(type=TypeFacture.VENTE, annulee=False).order_by('-date').first()


def _notifier(facture):
    from .notifications import envoyer_facture

    try:
        envoyer_facture(facture)
    except Exception:  # un envoi raté ne doit jamais bloquer la caisse
        log.exception("Envoi de la facture %s impossible", facture.numero)


def enregistrer_paiement(paiement, user, notifier=True):
    """Enregistre un encaissement, émet sa facture et l'envoie au parent. Renvoie la facture."""
    with transaction.atomic():
        paiement.encaisse_par = paiement.encaisse_par or user
        paiement.save()
        appliquer_reductions(paiement.eleve)
        facture = emettre_facture(paiement, user)
    if notifier:
        _notifier(facture)
    return facture


def corriger_paiement(paiement, valeurs, motif, valide_par, demande_par=None):
    """Modifie un paiement : trace (avant / après / motif), avoir sur l'ancienne facture, nouvelle facture."""
    with transaction.atomic():
        avant = _instantane(paiement)
        for champ in CHAMPS_MODIFIABLES:
            if champ in valeurs:
                setattr(paiement, champ, valeurs[champ])
        paiement.save()
        apres = _instantane(paiement)
        HistoriquePaiement.objects.create(
            eleve=paiement.eleve, paiement=paiement, numero_recu=paiement.numero_recu,
            action=ActionCorrection.MODIFICATION, avant=avant, apres=apres, motif=motif,
            demande_par=demande_par or valide_par, valide_par=valide_par,
        )
        ancienne = facture_active(paiement)
        if ancienne:
            emettre_avoir(ancienne, valide_par)
        appliquer_reductions(paiement.eleve)
        nouvelle = emettre_facture(paiement, valide_par)
    _notifier(nouvelle)
    return nouvelle


def annuler_paiement(paiement, motif, valide_par, demande_par=None):
    """Annule un paiement : trace, facture d'avoir, puis suppression du paiement."""
    eleve = paiement.eleve
    with transaction.atomic():
        HistoriquePaiement.objects.create(
            eleve=eleve, paiement=paiement, numero_recu=paiement.numero_recu,
            action=ActionCorrection.ANNULATION, avant=_instantane(paiement), apres={}, motif=motif,
            demande_par=demande_par or valide_par, valide_par=valide_par,
        )
        facture = facture_active(paiement)
        if facture:
            emettre_avoir(facture, valide_par)
        paiement.delete()
        appliquer_reductions(eleve)


def valider_demande(demande, user, accepter, commentaire=''):
    from .models import StatutDemande

    demande.traite_par, demande.traite_le, demande.commentaire = user, timezone.now(), commentaire
    if accepter:
        if demande.action == ActionCorrection.ANNULATION:
            demande.statut = StatutDemande.VALIDEE
            demande.save()
            annuler_paiement(demande.paiement, demande.motif, user, demande.demande_par)
            return
        valeurs = dict(demande.nouvelles_valeurs)
        if 'date' in valeurs:
            from datetime import date

            valeurs['date'] = date.fromisoformat(valeurs['date'])
        corriger_paiement(demande.paiement, valeurs, demande.motif, user, demande.demande_par)
        demande.statut = StatutDemande.VALIDEE
    else:
        demande.statut = StatutDemande.REFUSEE
    demande.save()
