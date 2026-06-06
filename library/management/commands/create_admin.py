from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand

from library.models import User


class Command(BaseCommand):
    help = 'Create a new admin user in the User model.'

    def add_arguments(self, parser):
        parser.add_argument('fullname', type=str, help='Full name for the admin user')
        parser.add_argument('email', type=str, help='Email address for the admin user')
        parser.add_argument('password', type=str, help='Password for the admin user')

    def handle(self, *args, **options):
        fullname = options['fullname']
        email = options['email']
        password = options['password']

        if User.objects.filter(email=email).exists():
            self.stdout.write(self.style.ERROR(f'Error: A user with email {email} already exists.'))
            return

        hashed_password = make_password(password)

        User.objects.create(
            fullname=fullname,
            email=email,
            password_hash=hashed_password,
            account_status='Active',
        )

        self.stdout.write(self.style.SUCCESS(f'Success: Admin user {fullname} created with email {email}.'))
