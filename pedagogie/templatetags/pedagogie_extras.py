from decimal import Decimal

from django import template

from ..calculs import libelle_rang

register = template.Library()


@register.filter
def note(valeur):
    """Decimal('12.5') -> '12,50' ; vide -> '—'."""
    if valeur in (None, ''):
        return '—'
    return f'{Decimal(valeur):.2f}'.replace('.', ',')


@register.filter
def classe_note(valeur):
    """Classe CSS selon la moyenne : verte à partir de 10, rouge en dessous."""
    if valeur in (None, ''):
        return ''
    return 'note-ok' if Decimal(valeur) >= 10 else 'note-ko'


@register.filter
def rang(valeur):
    return libelle_rang(valeur)


@register.filter
def heures(valeur):
    """Decimal('1.50') -> '1 h 30'."""
    if valeur in (None, ''):
        return '—'
    total = round(Decimal(valeur) * 60)
    h, m = divmod(int(total), 60)
    return f'{h} h {m:02d}' if m else f'{h} h'
