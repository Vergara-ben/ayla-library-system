"""Close visits left open past the library's closing time.

This work already existed, but it only ran opportunistically: close_stale_visits()
is called when somebody happens to load Log Management or Manage Patrons. On a
quiet day when nobody opens either page, yesterday's visitors stay "inside"
overnight and the occupancy figure -- and every visit-duration statistic derived
from it -- drifts.

Giving it a command means it can be scheduled, which is what makes it reliable:

    python manage.py close_open_visits

Run it once daily, after closing time. On PythonAnywhere that is a scheduled
task; on a normal host, a cron line. Pair it with the overdue sweep:

    python manage.py send_overdue_notifications
"""

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
