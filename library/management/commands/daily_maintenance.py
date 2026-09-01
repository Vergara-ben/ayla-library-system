"""Everything the library needs done once a day, in one command.

There are two jobs -- flag and email overdue loans, and close visits left open
past closing time -- and they were two commands. That is the right shape until
you meet a host that grants exactly one scheduled task slot, which the free
PythonAnywhere tier does. Rather than choose which job to skip, run both from
one entry point.

    python manage.py daily_maintenance

The individual commands still exist and still work; this only calls them, so
nothing is duplicated and either can still be run alone while debugging.

One job failing does not stop the other. A mail server refusing connections
should not also mean yesterday's visitors stay signed in forever.
"""

from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Run the daily overdue sweep and close stale visits (one scheduled task)."

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report what each job would do without changing anything.',
        )

    def handle(self, *args, **options):
        dry = options['dry_run']
        jobs = [
            ('Overdue loans', 'send_overdue_notifications'),
            ('Open visits', 'close_open_visits'),
        ]

        failures = 0
        for label, command in jobs:
            self.stdout.write(self.style.MIGRATE_HEADING(f'\n== {label} =='))
            try:
                call_command(command, dry_run=dry)
            except Exception as exc:
                # Caught, counted, and carried on: the two jobs are unrelated, and
                # a failure in one is not a reason to skip the other.
                failures += 1
                self.stderr.write(self.style.ERROR(
                    f'{label} failed: {type(exc).__name__}: {exc}'))

        self.stdout.write('')
        if failures:
            # Non-zero exit so a scheduler that reports task status shows this
            # as a failure rather than a silent success.
            raise SystemExit(f'{failures} of {len(jobs)} daily job(s) failed.')
        self.stdout.write(self.style.SUCCESS('Daily maintenance complete.'))
