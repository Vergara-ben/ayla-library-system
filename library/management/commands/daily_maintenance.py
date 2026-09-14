"""Everything the library needs done once a day, in one command."""

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
                # Keep going if one job fails.
                failures += 1
                self.stderr.write(self.style.ERROR(
                    f'{label} failed: {type(exc).__name__}: {exc}'))

        self.stdout.write('')
        if failures:
            # Exit with an error if a job failed.
            raise SystemExit(f'{failures} of {len(jobs)} daily job(s) failed.')
        self.stdout.write(self.style.SUCCESS('Daily maintenance complete.'))
