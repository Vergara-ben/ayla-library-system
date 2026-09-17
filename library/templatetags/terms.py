"""The library's terms and conditions, filled in from the current borrowing rules."""

from django import template

register = template.Library()

# Changes whenever the wording changes, so each acceptance says which terms were agreed to.
TERMS_VERSION = '2026-09-17'


@register.inclusion_tag('patron/_terms_body.html')
def terms_body():
    from ..models import BorrowingRule
    return {'rule': BorrowingRule.current(), 'version': TERMS_VERSION}
