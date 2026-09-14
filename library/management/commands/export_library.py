"""Write everything the library owns to one fixture, ready to load elsewhere."""

import io
import os
from datetime import date

from django.apps import apps
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

# Transient security state. Never exported: see the module docstring.
ALWAYS_EXCLUDE = ['library.LoginAttempt', 'library.PasswordResetOTP']

# The audit trail, exported only when asked for.
LOG_MODELS = ['library.SystemLog']


class Command(BaseCommand):
    help = 'Export the whole library to a UTF-8 fixture that loads on any database.'

    def add_arguments(self, parser):
        parser.add_argument(
            '-o', '--output',
            help='Where to write it. Defaults to ayla-data-<today>.json.',
        )
        parser.add_argument(
            '--with-logs',
            action='store_true',
            help='Include the activity log (SystemLog), which is excluded by default.',
        )

    def handle(self, *args, **options):
        path = options['output'] or 'ayla-data-%s.json' % date.today().isoformat()
        exclude = list(ALWAYS_EXCLUDE)
        if not options['with_logs']:
            exclude += LOG_MODELS

        # Serialise to a string first.
        buffer = io.StringIO()
        call_command('dumpdata', 'library', format='json', indent=1,
                     exclude=exclude, stdout=buffer)
        payload = buffer.getvalue()

        directory = os.path.dirname(os.path.abspath(path))
        if not os.path.isdir(directory):
            raise CommandError('No such directory: %s' % directory)

        with io.open(path, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(payload)

        # Prove it is readable as UTF-8 before saying it worked.
        with io.open(path, encoding='utf-8') as handle:
            handle.read()

        counts = {}
        for model in apps.get_app_config('library').get_models():
            label = '%s.%s' % (model._meta.app_label, model._meta.object_name)
            if label in exclude:
                continue
            count = model.objects.count()
            if count:
                counts[model._meta.object_name] = count

        size_kb = os.path.getsize(path) / 1024.0
        self.stdout.write(self.style.SUCCESS(
            'Wrote %s (%.0f KB, %d rows)' % (path, size_kb, sum(counts.values()))))
        for name in sorted(counts, key=lambda n: -counts[n]):
            self.stdout.write('  %-24s %d' % (name, counts[name]))

        skipped = [e.split('.')[-1] for e in exclude]
        self.stdout.write('')
        self.stdout.write('Not included: %s' % ', '.join(skipped))
        if not options['with_logs']:
            self.stdout.write('  (pass --with-logs to carry the activity log across)')
        self.stdout.write('')
        self.stdout.write('Load it on the server with:')
        self.stdout.write('  python manage.py loaddata %s' % os.path.basename(path))
        self.stdout.write('')
        self.stdout.write(self.style.WARNING(
            'This file holds patron details and uploaded IDs. '
            'Keep it private and never commit it.'))
