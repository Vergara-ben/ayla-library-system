"""Close visits left open past the library's closing time."""

from django.core.management.base import BaseCommand

from library.desk import close_stale_visits


class Command(BaseCommand):
    help = "Close visits left open past closing time (run daily, after hours)."

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report what would be closed without changing anything.',
        )

    def handle(self, *args, **options):
        from library.models import PatronLog

        if options['dry_run']:
            open_now = PatronLog.objects.filter(exit_time__isnull=True).count()
            self.stdout.write(f'{open_now} visit(s) currently open.')
            self.stdout.write('Dry run: nothing was changed.')
            return

        closed = close_stale_visits()
        if closed:
            self.stdout.write(self.style.SUCCESS(
                f'Closed {closed} stale visit(s), marked as assumed exits.'))
        else:
            self.stdout.write('Nothing to close.')
