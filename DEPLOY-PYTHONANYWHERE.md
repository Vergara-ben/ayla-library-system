# Deploying AYLA on PythonAnywhere

Click-by-click, start to finish. Budget about an hour for the first time.

**Everything in the system works on a free account** — including email, which
needs one extra step (§5b).

PythonAnywhere is chosen because this app needs three things that most "free
tier" hosts do not give you together: a **real filesystem** (patron ID scans are
saved to disk), **HTTPS** (the QR scanners will not open a camera without it),
and **no sleeping** (a host that cold-starts is a bad thing to demo on).

---

## Before you start: pick your tier

This is the one decision that matters, and it is worth understanding before you
sign up rather than after.

| | Free account | Paid account |
|---|---|---|
| Web app at `you.pythonanywhere.com` | ✅ | ✅ |
| HTTPS | ✅ | ✅ |
| Persistent disk for ID uploads | ✅ | ✅ |
| Database | MySQL only | MySQL **or** PostgreSQL |
| Outgoing email over **SMTP** | ❌ blocked | ✅ |
| Outgoing email over **HTTPS API** | ✅ (see §5b) | ✅ |
| Scheduled tasks | 1 slot | Several |
| Outbound internet | Whitelisted sites only | Unrestricted |

**Email works on a free account, but not over SMTP.** Free accounts cannot open
arbitrary outbound ports, so Gmail's port 587 is unreachable. The way round it is
to send over HTTPS instead, which free accounts *do* allow — see §5b. Every
provider listed there has a free tier well above a library's volume.

**One scheduled-task slot is also fine**, because both daily jobs run from a
single command (§9).

So a free account runs everything. Verify the current limits on PythonAnywhere's
own pages anyway — terms change, and this was written from the shape of the
platform rather than today's pricing page.

---

## 1. Push your code to GitHub

PythonAnywhere pulls from your repository. From your laptop:

```bash
git add -A
git commit -m "Prepare for deployment"
git push origin develop
```

If the repo is private, you will need a GitHub personal access token when
pulling on the server — GitHub no longer accepts account passwords over HTTPS.

---

## 2. Create the account and pull the code

1. Sign up at pythonanywhere.com
2. Open a **Bash console** from the Consoles tab
3. Clone your repository:

```bash
git clone https://github.com/Vergara-ben/ayla-library-system.git
cd ayla-library-system
git checkout develop
```

---

## 3. Create the virtualenv

Use the newest Python your account offers.

```bash
mkvirtualenv ayla --python=/usr/bin/python3.11
pip install -r requirements.txt
```

If you chose **MySQL**, also:

```bash
pip install mysqlclient
```

The prompt should now start with `(ayla)`. If you open a new console later,
re-enter it with `workon ayla`.

---

## 4. Create the database

### MySQL (free accounts)

1. **Databases** tab → set a MySQL password → **Create database** named `ayla`
2. The full name will be `yourusername$ayla` — you need the whole thing
3. Host is shown on that page, something like `yourusername.mysql.pythonanywhere-services.com`

### PostgreSQL (paid accounts)

1. **Databases** tab → **Postgres** → start the server, note host and port
2. In a Bash console: `createdb ayla`

---

## 5. Write the `.env` file

```bash
cd ~/ayla-library-system
cp .env.example .env
nano .env
```

Fill it in. Generate the secret key first, in another console:

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

A working MySQL example:

```
SECRET_KEY=<the long random string you just generated>
DEBUG=False
ALLOWED_HOSTS=yourusername.pythonanywhere.com
CSRF_TRUSTED_ORIGINS=https://yourusername.pythonanywhere.com

DATABASE_ENGINE=mysql
DATABASE_NAME=yourusername$ayla
DATABASE_USER=yourusername
DATABASE_PASSWORD=<your MySQL password>
DATABASE_HOST=yourusername.mysql.pythonanywhere-services.com
DATABASE_PORT=3306
```

For Postgres: `DATABASE_ENGINE=postgresql`, port `5432`, host and name from the
Databases tab.

Save with `Ctrl+O`, `Enter`, then `Ctrl+X`.

> `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` are not optional. Leave either
> blank with `DEBUG=False` and every request is refused — with a message that
> does not obviously point at this file.

---

## 5b. Email (required on a free account)

Skip this only if you are on a paid account and happy with SMTP.

Free accounts cannot reach SMTP ports, so mail must go over HTTPS instead. The
application supports this directly — **no code changes, just settings.**

1. Sign up with a transactional email provider. **Brevo**, **SendGrid**,
   **Mailgun** and **Resend** are all supported and all have free tiers.
2. Verify a sender address with them. Mail sent *from* an unverified address is
   rejected, and this is the step people skip.
3. Create an API key.
4. Check the provider's API host is on PythonAnywhere's whitelist (their site
   lists it). Brevo's is `api.brevo.com`.
5. Add to your `.env`:

```
EMAIL_PROVIDER=brevo
EMAIL_API_KEY=<the key from your provider>
DEFAULT_FROM_EMAIL=Ayla Public Library <the-address-you-verified@example.com>
```

Then install the adapter and reload the web app:

```bash
pip install django-anymail
```

Test it end to end before trusting it:

```bash
python manage.py send_test_email your-own-address@example.com
```

If the key is missing the app refuses to start and says so, rather than
appearing to work and dropping every message.

> Valid values for `EMAIL_PROVIDER`: `smtp`, `brevo`, `sendgrid`, `mailgun`,
> `resend`, `console`. Use `console` to print mail to the log instead of sending
> — useful while testing, useless in production.

---

## 6. Set up the database contents

```bash
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py create_admin
```

`create_admin` prompts for your first Administrator account. Use a real email
and a password you will not lose.

---

## 7. Configure the web app

**Web** tab → **Add a new web app** → **Manual configuration** (*not* the
"Django" option — that scaffolds a new project over yours) → same Python version
as your virtualenv.

Then fill in four fields on that page:

**Source code**
```
/home/yourusername/ayla-library-system
```

**Virtualenv**
```
/home/yourusername/.virtualenvs/ayla
```

**WSGI configuration file** — click it, delete everything, paste this:

```python
import os
import sys

path = '/home/yourusername/ayla-library-system'
if path not in sys.path:
    sys.path.insert(0, path)

os.environ['DJANGO_SETTINGS_MODULE'] = 'ayla_library_system.settings'

from django.core.wsgi import get_wsgi_application
application = get_wsgi_application()
```

Settings loads `.env` from the project directory itself, so nothing about the
environment needs repeating here.

Replace `yourusername` in both places.

**Static files** — add two mappings:

| URL | Directory |
|---|---|
| `/static/` | `/home/yourusername/ayla-library-system/staticfiles` |
| `/media/` | `/home/yourusername/ayla-library-system/media` |

> The `/media/` mapping serves book QR images. It will **not** expose patron ID
> scans: `media/credentials/` is refused by the URL configuration and those files
> are served only through `/patron-id/<file>`, behind a login. That is deliberate
> — do not "fix" it by adding a separate mapping for the credentials folder.

Finally: **Security** section on the same page → enable **Force HTTPS**.

Then hit the big green **Reload** button.

---

## 8. First sign-in

Visit `https://yourusername.pythonanywhere.com` — it should redirect to the
patron portal.

Sign in at `/admin-portal/login/` with the account you created.

**Then grant yourself modules.** Go to **User Management**, open your own
account, and tick the modules you need. Transactions, Log Management, Manage
Patrons, Manage Books, Donations and Inventory all check for a module grant
*even for an Administrator*, so a brand-new admin account sees them redirect to
the dashboard. This catches everyone once.

---

## 9. Scheduled tasks

**Tasks** tab → add **one** daily task:

```
cd ~/ayla-library-system && workon ayla && python manage.py daily_maintenance
```

That single command runs both daily jobs — flagging and emailing overdue loans,
and closing visits left open past closing time — so one task slot is enough. If
one job fails the other still runs, and the task exits non-zero so PythonAnywhere
shows it red rather than silently succeeding.

Schedule it for after closing time. Preview what it would do without changing
anything:

```bash
python manage.py daily_maintenance --dry-run
```

The individual commands (`send_overdue_notifications`, `close_open_visits`) still
exist if you want to run one alone.

---

## 10. Deploying an update

This is the loop you will run from now on. On your laptop:

```bash
python manage.py test library        # 19 tests, ~15 seconds
git add -A && git commit -m "What changed" && git push
```

Then in a PythonAnywhere Bash console:

```bash
cd ~/ayla-library-system
workon ayla
git pull
python manage.py migrate
python manage.py collectstatic --noinput
```

Then **Web** tab → **Reload**. The reload is what actually puts new code live;
skipping it is the most common reason "nothing changed".

Four rules:

1. **Tests before every push.** A red test is a blocked deploy.
2. **Never edit files directly on the server.** The next `git pull` erases them.
3. **Back up before any migration** (§11). Code can be reverted; a dropped
   column cannot give data back.
4. **Only `migrate` when a migration is new.** Running it with nothing pending
   is harmless, so when in doubt, run it.

---

## 11. Backups

Two things hold state you cannot regenerate.

**Database:**

```bash
# MySQL
mysqldump -u yourusername -h yourusername.mysql.pythonanywhere-services.com \
    -p 'yourusername$ayla' > ~/ayla-$(date +%F).sql

# PostgreSQL
pg_dump -Fc ayla > ~/ayla-$(date +%F).dump
```

**Uploads** — patron ID scans and QR images:

```bash
tar czf ~/ayla-media-$(date +%F).tar.gz ~/ayla-library-system/media/
```

Download both from the **Files** tab. `media/credentials/` holds photographs of
government IDs — keep those backups off shared drives.

---

## When something goes wrong

**Error log** — Web tab → *Error log*. Read the **bottom** of the file; that is
the most recent failure.

**Application log** — `logs/ayla.log` in your project directory. Email failures
appear only here, because a broken SMTP password must never stop a librarian
processing a loan.

| Symptom | Almost always |
|---|---|
| `DisallowedHost` | `ALLOWED_HOSTS` missing your domain |
| CSRF failure on any form | `CSRF_TRUSTED_ORIGINS` missing `https://yourdomain` |
| Site loads with no styling | `collectstatic` not run, or the `/static/` mapping is wrong |
| `RuntimeError: DATABASE_NAME is not set` | `.env` not being read — check `load_dotenv` in the WSGI file |
| Camera will not open | Not on HTTPS. Enable **Force HTTPS**. |
| Changes not appearing | You did not press **Reload** |
| Cannot open Transactions/Books/etc. | Your admin account has no module grants (§8) |
| No emails arriving | `EMAIL_PROVIDER=smtp` on a free account — switch to an API provider (§5b) |
| Emails rejected by the provider | `DEFAULT_FROM_EMAIL` is not a sender you verified (§5b) |
