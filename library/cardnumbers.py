"""The number printed on an AYLA library card."""

import random

# Every card the library issues begins with this.
AYLA_PREFIX = '7'

LENGTH = 7


def _check_digit(body):
    """Luhn check digit for the six digits in front of it."""
    total = 0
    # Double every second digit from the right.
    for index, ch in enumerate(reversed(body)):
        digit = int(ch)
        if index % 2 == 0:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return str((10 - (total % 10)) % 10)


def is_valid(number):
    """Is this seven-digit string one this library could have issued?"""
    number = normalise(number)
    if len(number) != LENGTH or not number.isdigit():
        return False
    if not number.startswith(AYLA_PREFIX):
        return False
    return _check_digit(number[:-1]) == number[-1]


def normalise(value):
    """What somebody typed, reduced to the digits in it."""
    return ''.join(ch for ch in (value or '') if ch.isdigit())


def format_card(number):
    """The card number as it is shown anywhere: seven digits, unbroken."""
    return normalise(number)


def generate(exists=None):
    """A fresh card number that nobody else holds."""
    if exists is None:
        from .models import Patron

        def exists(candidate):
            return Patron.objects.filter(card_number=candidate).exists()

    # Retry if the number is already taken.
    for _ in range(50):
        body = AYLA_PREFIX + ''.join(random.choice('0123456789') for _ in range(5))
        candidate = body + _check_digit(body)
        if not exists(candidate):
            return candidate
    raise RuntimeError('Could not find an unused card number after 50 tries.')
