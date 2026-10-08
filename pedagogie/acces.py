"""Qui voit quoi : la direction et le secrétariat voient tout, un professeur seulement ses cours."""

from django.core.exceptions import PermissionDenied

from .models import Enseignement, Professeur


def voit_tout(user):
    return user.has_perm('pedagogie.voir_tout')


def professeur_de(user):
    try:
        return user.professeur
    except (Professeur.DoesNotExist, AttributeError):
        return None


def enseignements_visibles(user):
    qs = Enseignement.objects.select_related('etablissement', 'matiere', 'professeur')
    if voit_tout(user):
        return qs
    prof = professeur_de(user)
    return qs.filter(professeur=prof) if prof else qs.none()


def verifier_enseignement(user, enseignement, saisie=False):
    """Refuse l'accès à un cours qui n'est pas celui du professeur connecté."""
    if saisie and not user.has_perm('pedagogie.change_fichenote'):
        raise PermissionDenied
    if voit_tout(user):
        return
    prof = professeur_de(user)
    if not prof or enseignement.professeur_id != prof.pk:
        raise PermissionDenied


def verifier_professeur(user, professeur):
    if voit_tout(user):
        return
    if professeur_de(user) != professeur:
        raise PermissionDenied
