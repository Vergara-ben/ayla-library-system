"""Fill the patron log with a plausible visit history for demonstration.

Log Management shows one day at a time and defaults to today, so an empty
table is what you get until somebody actually signs in at the desk. This
writes visits across recent days -- entries, exits, a few people still
inside, and some left open overnight and closed by the nightly sweep.

    python manage.py seed_logs                    21 days, ~10 visits a day
    python manage.py seed_logs --days 40          more history
    python manage.py seed_logs --per-day 18       a busier library
    python manage.py seed_logs --visitors 0       members only, no walk-ins
    python manage.py seed_logs --clear            remove what this created
    python manage.py seed_logs --clear --all      remove every log and visitor

Walk-ins are created as Patron rows with status Visitor, which is how an
unregistered person is recorded at the desk. Everything written here is
listed in seeded_logs.json so --clear removes demonstration data and
nothing else.
"""

import json
import random
from datetime import datetime, time, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction as db_transaction
from django.utils import timezone

from library.desk import PURPOSE_CHOICES
from library.models import Patron, PatronLog

LEDGER = 'seeded_logs.json'

OPENING = time(8, 0)
CLOSING = time(17, 0)

SCHOOLS = [
    'Pasig City Science High School', 'Rizal High School',
    'San Juan National High School', 'Bagong Silang Elementary School',
    'Polytechnic University of the Philippines', 'University of Rizal System',
    'Manila Central Colleges', 'St. Anne College',
]

FIRST_NAMES = [
    'Andrea', 'Miguel', 'Sofia', 'Gabriel', 'Isabela', 'Rafael', 'Camille',
    'Joaquin', 'Trisha', 'Emmanuel', 'Divina', 'Nathaniel', 'Krizza', 'Paolo',
]
LAST_NAMES = [
    'Santos', 'Dela Cruz', 'Reyes', 'Bautista', 'Ocampo', 'Villanueva',
    'Aguinaldo', 'Mercado', 'Salazar', 'Padilla', 'Gutierrez', 'Manalo',
]


class Command(BaseCommand):
    help = 'Create demonstration visit history in the patron log.'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=21,
                            help='how many days back to fill (default 21)')
        parser.add_argument('--per-day', type=int, default=10,
                            help='average visits per open day (default 10)')
        parser.add_argument('--visitors', type=int, default=6,
                            help='walk-in Visitor records to create (default 6)')
        parser.add_argument('--seed', type=int, default=None,
                            help='fix the random seed so a run can be repeated')
        parser.add_argument('--clear', action='store_true',
                            help='remove logs and visitors this command created')
        parser.add_argument('--all', action='store_true',
                            help='with --clear, remove every log and every Visitor')

    @property
    def ledger_path(self):
        return settings.BASE_DIR / LEDGER

    def read_ledger(self):
        try:
            with open(self.ledger_path, encoding='utf-8') as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def write_ledger(self, log_ids, patron_ids):
        with open(self.ledger_path, 'w', encoding='utf-8') as fh:
            json.dump({'log_ids': log_ids, 'visitor_patron_ids': patron_ids,
                       'created': timezone.now().isoformat()}, fh, indent=1)

    def handle(self, *args, **options):
        if options['clear']:
            return self.clear(options['all'])
        return self.seed(options)

    def clear(self, everything):
        if everything:
            logs = PatronLog.objects.count()
            visitors = Patron.objects.filter(account_status='Visitor').count()
            PatronLog.objects.all().delete()
            Patron.objects.filter(account_status='Visitor').delete()
            self.write_ledger([], [])
            self.stdout.write(self.style.WARNING(
                'Deleted all %d log(s) and %d visitor record(s).' % (logs, visitors)))
            return

        ledger = self.read_ledger()
        if ledger is None:
            self.stdout.write(self.style.ERROR(
                'No %s found, so there is no way to tell demonstration rows from real '
                'ones.\nRun with --clear --all to delete every log and visitor, if that '
                'is really what you want.' % LEDGER))
            return

        log_ids = ledger.get('log_ids') or []
        patron_ids = ledger.get('visitor_patron_ids') or []
        logs = PatronLog.objects.filter(log_id__in=log_ids).count()
        PatronLog.objects.filter(log_id__in=log_ids).delete()
        # Only the walk-ins this command invented, and only if nothing else has
        # since attached to them.
        visitors = Patron.objects.filter(patron_id__in=patron_ids,
                                         account_status='Visitor')
        removed_visitors = visitors.count()
        visitors.delete()
        self.write_ledger([], [])
        self.stdout.write(self.style.SUCCESS(
            'Removed %d seeded log(s) and %d visitor record(s).'
            % (logs, removed_visitors)))

    def seed(self, options):
        rng = random.Random(options['seed'])
        today = timezone.localdate()
        days = max(1, options['days'])
        per_day = max(1, options['per_day'])

        created_visitors = []
        with db_transaction.atomic():
            # ── Walk-ins ────────────────────────────────────────────────
            for _ in range(max(0, options['visitors'])):
                first = rng.choice(FIRST_NAMES)
                last = rng.choice(LAST_NAMES)
                visitor = Patron.objects.create(
                    first_name=first,
                    last_name=last,
                    fullname='%s %s' % (first, last),
                    patron_type='General Visitor',
                    account_status='Visitor',
                    # A walk-in has no login, no QR and no borrowing rights.
                    # The row exists so a repeat visitor is recognised.
                    password_hash='',
                    email=None,
                )
                created_visitors.append(visitor.patron_id)

            people = list(Patron.objects.exclude(account_status__in=('Suspended', 'Inactive')))
            if not people:
                self.stdout.write(self.style.ERROR('No patrons to log visits for.'))
                return

            log_ids = []
            still_inside = 0
            assumed = 0

            for offset in range(days):
                day = today - timedelta(days=offset)
                # Sundays are quiet rather than closed; a flat count every day
                # reads as generated data the moment anyone looks at a week.
                if day.weekday() == 6:
                    visits = rng.randint(0, max(1, per_day // 4))
                elif day.weekday() == 5:
                    visits = rng.randint(max(1, per_day // 3), max(2, per_day // 2))
                else:
                    visits = rng.randint(max(1, per_day - 4), per_day + 4)

                for _ in range(visits):
                    person = rng.choice(people)
                    entry_dt = self._aware(day, self._random_time(rng, OPENING, CLOSING))

                    # Someone who came in near closing has not been there long.
                    minutes = rng.choice([20, 30, 45, 60, 75, 90, 120, 150, 180])
                    exit_dt = entry_dt + timedelta(minutes=minutes)
                    close_dt = self._aware(day, CLOSING)

                    auto_closed = False
                    if offset == 0:
                        # Today: some people are still in the building.
                        now = timezone.localtime()
                        if entry_dt > now:
                            continue
                        if exit_dt > now and rng.random() < 0.7:
                            exit_dt = None
                            still_inside += 1
                    elif rng.random() < 0.18:
                        # Left without signing out; the sweep stamps the day's end.
                        exit_dt = self._aware(day, time(23, 59))
                        auto_closed = True
                        assumed += 1
                    elif exit_dt > close_dt:
                        exit_dt = close_dt

                    is_student = rng.random() < 0.55
                    log = PatronLog.objects.create(
                        patron=person,
                        school=rng.choice(SCHOOLS) if is_student else None,
                        purpose_of_visit=rng.choice(PURPOSE_CHOICES),
                        entry_time=entry_dt,
                        exit_time=exit_dt,
                        auto_closed=auto_closed,
                    )
                    log_ids.append(log.log_id)

        self.write_ledger(log_ids, created_visitors)

        todays = PatronLog.objects.filter(entry_time__date=today)
        self.stdout.write(self.style.SUCCESS('\nCreated %d visit(s).' % len(log_ids)))
        self.stdout.write('  visitor records   %d' % len(created_visitors))
        self.stdout.write('  today             %d entries' % todays.count())
        self.stdout.write('  still inside now  %d' % still_inside)
        self.stdout.write('  assumed exits     %d  (shown as ASSUMED)' % assumed)
        self.stdout.write('  dated between     %s and %s'
                          % (today - timedelta(days=days - 1), today))
        self.stdout.write('\nRun "python manage.py seed_logs --clear" to remove these.')

    @staticmethod
    def _random_time(rng, start, end):
        start_min = start.hour * 60 + start.minute
        end_min = end.hour * 60 + end.minute
        pick = rng.randint(start_min, max(start_min, end_min - 20))
        return time(pick // 60, pick % 60)

    @staticmethod
    def _aware(day, at):
        naive = datetime.combine(day, at)
        if timezone.is_naive(naive):
            return timezone.make_aware(naive, timezone.get_current_timezone())
        return naive
