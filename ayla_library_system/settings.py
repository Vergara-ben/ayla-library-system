"""Django settings for ayla_library_system project."""

import os
from pathlib import Path
from dotenv import load_dotenv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env from the project folder.
load_dotenv(BASE_DIR / '.env')


# Development settings. See https://docs.djangoproject.com/en/6.0/howto/deployment/checklist/

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.environ.get('DEBUG', 'False').lower() in ('1', 'true', 'yes')

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ.get('SECRET_KEY')
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = 'django-insecure-dev-key-do-not-use-in-production'
    else:
        raise RuntimeError('SECRET_KEY environment variable must be set in production.')

# Comma-separated list in the env, e.g.
ALLOWED_HOSTS = [h.strip() for h in os.environ.get('ALLOWED_HOSTS', '').split(',') if h.strip()]
if not ALLOWED_HOSTS:
    ALLOWED_HOSTS = ['*'] if DEBUG else []

# HTTPS origins trusted for CSRF (needed once served over a real domain), e.g.
CSRF_TRUSTED_ORIGINS = [o.strip() for o in os.environ.get('CSRF_TRUSTED_ORIGINS', '').split(',') if o.strip()]

# Render sets this to the service's own address (e.g. ayla-library.onrender.com).
RENDER_HOST = os.environ.get('RENDER_EXTERNAL_HOSTNAME', '').strip()
if RENDER_HOST:
    if RENDER_HOST not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(RENDER_HOST)
    CSRF_TRUSTED_ORIGINS.append(f'https://{RENDER_HOST}')

# Trust tunnel origins for phone testing (DEBUG only).
if DEBUG:
    CSRF_TRUSTED_ORIGINS += [
        'https://*.trycloudflare.com',
        'https://*.ngrok-free.app',
        'https://*.ngrok.io',
        'https://*.loca.lt',
    ]


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'library',
]

MIDDLEWARE = [
    # First, so the timing covers everything else in the stack.
    'library.middleware.SlowRequestLoggingMiddleware',
    'django.middleware.security.SecurityMiddleware',
    # Serves collected static files from the app itself.
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    # Signed-in pages are not kept by the browser, so Back after logout cannot show them.
    'library.middleware.NoStoreSignedInPagesMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    # Signs out an account deactivated, suspended or archived mid-session.
    'library.middleware.SignOutInactiveAccountsMiddleware',
    'library.middleware.ContentSecurityPolicyMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    # Suspends the portal in a browser that has been handed to the public.
    'library.middleware.DeskModeMiddleware',
]

ROOT_URLCONF = 'ayla_library_system.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'library.context_processors.staff_modules',
                'library.context_processors.patron_session',
            ],
        },
    },
]

WSGI_APPLICATION = 'ayla_library_system.wsgi.application'


# Database https://docs.djangoproject.com/en/6.0/ref/settings/#databases.

# Fail early if DATABASE_NAME is missing.
if not os.environ.get('DATABASE_NAME'):
    raise RuntimeError(
        'DATABASE_NAME is not set. Copy .env.example to .env and fill in the '
        'database settings before starting the application.'
    )

# The engine is configurable because the deployment host decides it, not the code.
DATABASE_ENGINE = os.environ.get('DATABASE_ENGINE', 'postgresql')
if DATABASE_ENGINE not in ('postgresql', 'mysql'):
    raise RuntimeError(
        f"DATABASE_ENGINE must be 'postgresql' or 'mysql', not {DATABASE_ENGINE!r}. "
        "SQLite cannot lock rows and would silently disable the protection "
        "against the same copy being lent to two patrons at once."
    )

DATABASES = {
    'default': {
        'ENGINE': f'django.db.backends.{DATABASE_ENGINE}',
        'NAME': os.environ.get('DATABASE_NAME'),
        'USER': os.environ.get('DATABASE_USER'),
        'PASSWORD': os.environ.get('DATABASE_PASSWORD'),
        'HOST': os.environ.get('DATABASE_HOST'),
        'PORT': os.environ.get('DATABASE_PORT'),
        # Reuse database connections.
        'CONN_MAX_AGE': int(os.environ.get('DB_CONN_MAX_AGE', '60')),
        # Reopen a connection the pooler has already closed.
        'CONN_HEALTH_CHECKS': True,
    }
}

# Supabase and most hosted PostgreSQL need an encrypted connection (require).
DATABASE_SSLMODE = os.environ.get('DATABASE_SSLMODE', '').strip()
if DATABASE_SSLMODE and DATABASE_ENGINE == 'postgresql':
    DATABASES['default']['OPTIONS'] = {'sslmode': DATABASE_SSLMODE}


# Password validation.

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization https://docs.djangoproject.com/en/6.0/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'Asia/Manila'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, images).

STATIC_URL = '/static/'

# Media files
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'
# Create media folders if missing. Uploaded IDs are stored in the database.
(MEDIA_ROOT / 'qrcodes').mkdir(parents=True, exist_ok=True)

SESSION_ENGINE = 'django.contrib.sessions.backends.db'
# No idle sign-out: the library removed it.
SESSION_COOKIE_AGE = 14 * 24 * 3600
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
LOGIN_URL = '/patron/login/'

# Secret for the daily task link called by an outside scheduler (e.g. cron-job.org).
DAILY_TASK_TOKEN = os.environ.get('DAILY_TASK_TOKEN', '').strip()
ADMIN_LOGIN_URL = '/admin-portal/login/'

STATICFILES_DIRS = [
    BASE_DIR / 'static',
]

STATIC_ROOT = BASE_DIR / 'staticfiles'

# Hashed static files with a manifest.
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'
        if not DEBUG else 'django.contrib.staticfiles.storage.StaticFilesStorage'
    },
}


# Email settings.
LIBRARY_NAME = 'Ayla Public Library'
EMAIL_HOST = os.environ.get('EMAIL_HOST', 'smtp.gmail.com')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '587'))
EMAIL_USE_TLS = os.environ.get('EMAIL_USE_TLS', 'True').lower() in ('1', 'true', 'yes')
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
DEFAULT_FROM_EMAIL = os.environ.get(
    'DEFAULT_FROM_EMAIL',
    f'{LIBRARY_NAME} <{EMAIL_HOST_USER}>' if EMAIL_HOST_USER else 'noreply@aylalibrary.local'
)
EMAIL_TIMEOUT = 20

# How mail actually leaves the building.
EMAIL_PROVIDER = os.environ.get('EMAIL_PROVIDER', 'smtp').strip().lower()
EMAIL_API_KEY = os.environ.get('EMAIL_API_KEY', '')

_ANYMAIL_BACKENDS = {
    'brevo': ('anymail.backends.brevo.EmailBackend', 'BREVO_API_KEY'),
    'sendgrid': ('anymail.backends.sendgrid.EmailBackend', 'SENDGRID_API_KEY'),
    'mailgun': ('anymail.backends.mailgun.EmailBackend', 'MAILGUN_API_KEY'),
    'resend': ('anymail.backends.resend.EmailBackend', 'RESEND_API_KEY'),
}

ANYMAIL = {}

if EMAIL_PROVIDER in _ANYMAIL_BACKENDS:
    backend_path, key_name = _ANYMAIL_BACKENDS[EMAIL_PROVIDER]
    if not EMAIL_API_KEY:
        raise RuntimeError(
            f"EMAIL_PROVIDER is '{EMAIL_PROVIDER}' but EMAIL_API_KEY is empty. "
            'Set the API key from your provider, or use EMAIL_PROVIDER=smtp.'
        )
    try:
        import anymail  # noqa: F401
    except ImportError:
        raise RuntimeError(
            f"EMAIL_PROVIDER is '{EMAIL_PROVIDER}', which needs django-anymail. "
            'Run: pip install django-anymail'
        )
    EMAIL_BACKEND = backend_path
    ANYMAIL = {key_name: EMAIL_API_KEY}
    if EMAIL_PROVIDER == 'mailgun':
        ANYMAIL['MAILGUN_SENDER_DOMAIN'] = os.environ.get('MAILGUN_SENDER_DOMAIN', '')

elif EMAIL_PROVIDER == 'console':
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

elif EMAIL_HOST_USER and EMAIL_HOST_PASSWORD:
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'

else:
    # Nothing configured.
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

# Logging to the console and a rotating file.
LOG_DIR = BASE_DIR / 'logs'
LOG_DIR.mkdir(exist_ok=True)

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'simple': {'format': '[{asctime}] {levelname} {name}: {message}', 'style': '{'},
    },
    'handlers': {
        'console': {'class': 'logging.StreamHandler', 'formatter': 'simple'},
        'file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': LOG_DIR / 'ayla.log',
            # Keep five 2 MB log files.
            'maxBytes': 2 * 1024 * 1024,
            'backupCount': 5,
            'formatter': 'simple',
            'encoding': 'utf-8',
        },
    },
    'loggers': {
        'library': {'handlers': ['console', 'file'], 'level': 'INFO', 'propagate': False},
        'django.request': {'handlers': ['console', 'file'], 'level': 'ERROR', 'propagate': False},
    },
}


# Security settings.
SECURE_CONTENT_TYPE_NOSNIFF = True

# Content Security Policy.
CSP_DIRECTIVES = [
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com "
    "https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://unpkg.com",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com "
    "https://cdnjs.cloudflare.com https://unpkg.com",
    "font-src 'self' data: https://fonts.gstatic.com https://cdnjs.cloudflare.com",
    # CDN hosts used by Leaflet.
    "img-src 'self' data: blob: https://cdnjs.cloudflare.com https://unpkg.com",
    "connect-src 'self'",
    "media-src 'self' blob:",
    "worker-src 'self' blob:",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
]
CONTENT_SECURITY_POLICY = '; '.join(CSP_DIRECTIVES)

SECURE_REFERRER_POLICY = 'same-origin'
SESSION_COOKIE_HTTPONLY = True
X_FRAME_OPTIONS = 'DENY'

# HTTPS-only settings for production.
if not DEBUG:
    # Render (and most hosts) handle HTTPS at a proxy in front of the app.
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SECURE_SSL_REDIRECT = os.environ.get('SECURE_SSL_REDIRECT', 'True').lower() in ('1', 'true', 'yes')
    # The host's internal health check calls over plain HTTP.
    SECURE_REDIRECT_EXEMPT = [r'^healthz/$']
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(os.environ.get('SECURE_HSTS_SECONDS', '31536000'))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True