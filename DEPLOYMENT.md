# Deploying AYLA

Everything needed to take this from a clone to a running library system, in
order. Written to be followed by someone who did not build it.

---

## 1. Requirements

- Python 3.11 or newer
- **PostgreSQL** 13+ or **MySQL** 5.7+ — set with `DATABASE_ENGINE`.
  Not SQLite: it cannot lock rows, which would silently disable the protection
  against lending one copy to two patrons at once.
- A way to send mail — either an SMTP account (Gmail with an *app password*), or
  an API key from a transactional provider if your host blocks SMTP (§7)

```bash
pip install -r requirements.txt
```

---

## 2. Configuration

Every setting comes from the environment. Copy the template and fill it in:

```bash
cp .env.example .env
```

The application **refuses to start** if `SECRET_KEY` (in production) or
`DATABASE_NAME` is missing, rather than failing later with an unhelpful error.

| Variable | Required | Notes |
|---|---|---|
| `SECRET_KEY` | **yes** in production | Any long random string. Never commit it. |
| `DEBUG` | no | Defaults to `False`. Never `True` in production. |
| `ALLOWED_HOSTS` | **yes** in production | Comma-separated. Empty + `DEBUG=False` blocks every request. |
| `CSRF_TRUSTED_ORIGINS` | **yes** in production | Full origins with scheme, e.g. `https://ayla.example.com` |
| `DATABASE_ENGINE` | no | `postgresql` (default) or `mysql`. Never SQLite. |
| `DATABASE_NAME` / `_USER` / `_PASSWORD` / `_HOST` / `_PORT` | **yes** | |
| `EMAIL_PROVIDER` | no | `smtp` (default), `brevo`, `sendgrid`, `mailgun`, `resend`, `console` |
| `EMAIL_API_KEY` | if not smtp | Required by the API providers; the app refuses to start without it |
| `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` | if smtp | Without both, mail prints to the console instead of sending |
| `SECURE_SSL_REDIRECT` | no | Defaults `True` in production |

Generate a secret key:

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

---

## 3. First deploy

```bash
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py create_admin
```

`create_admin` prompts for the first Administrator account.

**Then grant that account its modules.** Sign in and open **User Management**.
Desk-facing screens — Transactions, Log Management, Manage Patrons, Manage
Books, Donations, Inventory — are gated on a module grant *even for an
Administrator*. A brand-new admin account holds none, so those pages redirect to
the dashboard until they are granted. This surprises everyone once.

---

## 4. Scheduled tasks

Two jobs must run on a schedule. Without them the system still works, but data
quietly drifts. One command covers both:

```bash
python manage.py daily_maintenance
```

It runs the overdue sweep and closes stale visits. A failure in one does not
stop the other, and it exits non-zero so a scheduler shows the task as failed.
Accepts `--dry-run`.

| Job inside it | What breaks without it |
|---|---|
| `send_overdue_notifications` | Nothing is ever flagged overdue; no reminder emails |
| `close_open_visits` | Visitors stay "inside" overnight; occupancy and visit-duration figures drift |

Both still exist as separate commands if you want to run one alone.

One command rather than two because some hosts grant only a single scheduled
task slot — a free PythonAnywhere account does.

```bash
0 21 * * * cd /path/to/ayla-library-system && python manage.py daily_maintenance
```

---

## 5. Before every deploy

```bash
python manage.py test library
python manage.py check --deploy
```

The test suite is a smoke layer: every page renders, every report builds and
exports, the auth guards hold, a borrow round-trips. It runs in about fifteen
seconds. **A red test is a blocked deploy.**

`check --deploy` should report no warnings when `DEBUG=False`. If you run it
with `DEBUG=True` you will see five HTTPS warnings — those are expected locally.

---

## 6. Backups

Two things hold state that cannot be regenerated:

**The database** — everything.

```bash
# PostgreSQL
pg_dump -Fc "$DATABASE_NAME" > ayla-$(date +%F).dump

# MySQL
mysqldump -u "$DATABASE_USER" -h "$DATABASE_HOST" -p "$DATABASE_NAME" > ayla-$(date +%F).sql
```

**`media/`** — patron ID scans (`media/credentials/`) and generated QR images.
This directory is gitignored, so it exists in exactly one place. It is not in
your repository and it will not come back on its own.

```bash
tar czf ayla-media-$(date +%F).tar.gz media/
```

Take both on the same schedule; a database restored against the wrong media
directory leaves approved patrons with missing ID documents.

`media/credentials/` holds photographs of government identity documents. Treat
those backups accordingly — encrypted at rest, and not on shared storage.

---

## 7. Logs

`logs/ayla.log` — rotating, 2 MB × 5 files. Contains application errors and
unhandled request exceptions.

Email failures are deliberately non-fatal: a broken mail configuration will not
stop a librarian processing a loan. It also means **the log is the only place a
mail failure appears.** Check it after changing mail settings, or overdue notices
will stop going out and nobody will notice.

Verify mail end to end after any change:

```bash
python manage.py send_test_email your-own-address@example.com
```

### If your host blocks SMTP

Some hosts (a free PythonAnywhere account among them) do not allow outbound
connections on SMTP ports, so port 587 is simply unreachable. Send over HTTPS
instead — set `EMAIL_PROVIDER` to `brevo`, `sendgrid`, `mailgun` or `resend`,
add `EMAIL_API_KEY`, and `pip install django-anymail`.

No application code changes; every send already goes through `django.core.mail`
and never learns which transport is in use. The `DEFAULT_FROM_EMAIL` address
must be one you have verified with that provider, or they will reject the send.

---

## 8. Security posture

Already in place, and worth knowing when something looks over-strict:

- Sign-in is **locked for 15 minutes after 5 failures**, per email address, on
  all four doors (admin, staff, patron, desk unlock).
- **Idle sessions close** — 15 minutes staff, 60 minutes patron.
- Patron ID scans are **not served from `/media/`**. They are behind a login and
  the `patrons` module, at `/patron-id/<file>`.
- A **Content-Security-Policy** header is sent on every response. If you add a
  new CDN, add it to `CSP_DIRECTIVES` in settings or the browser will block it.
- CDN scripts are **pinned with SRI hashes**. If you upgrade a library version,
  the hash must be recomputed or the browser will refuse to load it.

---

## 9. Known limitations

Honest notes for whoever maintains this next.

- **Tailwind is loaded from a CDN at runtime.** It has no SRI hash (its bytes
  change) and it compiles CSS in the browser. Building it to a static file would
  remove a third-party dependency and make pages render faster.
- **CSP includes `'unsafe-inline'` for scripts**, because the templates use
  inline `<script>` blocks and `onclick=` handlers. The policy therefore limits
  what injected script can *do*, but does not stop it running.
- **Indoor positioning is position-fixing, not continuous tracking.** BLE
  trilateration in a small room is accurate to roughly 1–3 m; the map answers
  "where am I standing" rather than following a walk.
