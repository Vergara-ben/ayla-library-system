"""The inventory's data-entry rules. The browser checks the same ones as you type (static/js/inventory-checks.js);
these are the second check, on the server, so nothing gets past either."""

import re
from datetime import date
from decimal import Decimal

from django.utils import timezone

EARLIEST_DONATION = date(1950, 1, 1)
MIN_PRICE = Decimal('1')


def reason_problem(text, what='reason'):
    """A reason that will still explain the change when someone reads the history later."""
    t = ' '.join((text or '').split())
    if not t:
        return 'Give the %s.' % what
    words = [w for w in t.split(' ') if re.search(r'[A-Za-z]{2,}', w)]
    letters = re.sub(r'[^a-z]', '', t.lower())
    if len(t) < 6 or len(words) < 2 or not re.search(r'[A-Za-z]{3,}', t) or len(set(letters)) < 4:
        return 'Write the %s in a few words, so the history still makes sense later.' % what
    return None


def supplier_problem(text):
    if len(' '.join((text or '').split())) < 2:
        return 'Type who supplied the shipment.'
    return None


def donated_date_problem(day):
    if day > timezone.localdate():
        return 'The donation date cannot be in the future.'
    if day < EARLIEST_DONATION:
        return 'Check the donation date: %s is too far back.' % day.strftime('%b %d, %Y')
    return None


def price_problem(price):
    """A copy's price is what a patron pays if it is lost, so it is never nothing."""
    if price is not None and price < MIN_PRICE:
        return 'A price must be at least ₱1: it is what a patron pays if the copy is lost.'
    return None
