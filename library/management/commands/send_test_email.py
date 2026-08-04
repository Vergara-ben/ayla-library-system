"""Send one test email to prove the SMTP configuration works.

    python manage.py send_test_email you@example.com

Reports which backend is active and, on failure, prints the underlying SMTP
error instead of swallowing it the way the app's best-effort helpers do.
"""

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Send a test email to verify the SMTP settings."

    def add_arguments(self, parser):
        parser.add_argument('recipient', help='Where to send the test message.')

    def handle(self, *args, **options):
        recipient = options['recipient']
        lib = getattr(settings, 'LIBRARY_NAME', 'Ayla Public Library')

        self.stdout.write(f"backend : {settings.EMAIL_BACKEND}")
        self.stdout.write(f"host    : {settings.EMAIL_HOST}:{settings.EMAIL_PORT} "
                          f"(TLS={settings.EMAIL_USE_TLS})")
        self.stdout.write(f"user    : {settings.EMAIL_HOST_USER or '(not set)'}")
        self.stdout.write(f"password: {'set' if settings.EMAIL_HOST_PASSWORD else 'NOT SET'}")
        self.stdout.write(f"from    : {settings.DEFAULT_FROM_EMAIL}")

        if 'console' in settings.EMAIL_BACKEND:
            self.stdout.write(self.style.WARNING(
                "\nConsole backend active — set EMAIL_HOST_USER and EMAIL_HOST_PASSWORD "
                "in .env and restart to send for real. Printing the message below."))

        try:
            # fail_silently=False on purpose: the whole point is to see the error.
            sent = send_mail(
                f"[{lib}] SMTP test",
                "If you can read this, the AYLA library system can send email.",
                settings.DEFAULT_FROM_EMAIL,
                [recipient],
                fail_silently=False,
            )
        except Exception as exc:
            raise CommandError(f"{type(exc).__name__}: {exc}")

        if sent:
            self.stdout.write(self.style.SUCCESS(f"\nSent 1 message to {recipient}."))
        else:
            raise CommandError("The backend accepted the call but reported 0 messages sent.")
