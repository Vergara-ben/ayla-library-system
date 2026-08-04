# AYLA LMS — Panel Revision Plan
*Generated 2026-07-04 against `CAPSTONE_REVISED_June21-4 (1).docx`*

## Context

The panel's revision pass on the manuscript (this June 21 revised copy) reinstated and expanded several requirements beyond what's currently built. The build so far (see prior session memory — Location Hierarchy, Reports, Borrowing Rules, Role Access Matrix work) already tracks an *earlier* version of the manuscript closely. Reading the current revised text end-to-end and diffing it against `models.py`, `views.py`, `urls.py`, and the template set turned up five real gaps and one report-lineup mismatch — not a pile of dead code. So this plan is short on deletions and long on additions; that's the honest result of the comparison, not a reframing to make the "cleanup first" ask look bigger than it is.

Two decisions were confirmed with you and are locked into this plan:
- **Inventory Management is Administrator-only** — no Library Staff access, despite the manuscript's Ch.1 prose suggesting Staff gets stock-receiving rights. Prior role-access notes win.
- **Reports become 7 types**: Transactions, Patron Logs, Books, Patrons, (Book) Donations, **Stock Levels**, **Stock Movement** — System Log Report is dropped from the Reports page; Inventory is represented as two distinct reports (matching Fig. 77/78 in the manuscript's activity diagrams), not one combined "Inventory" report.

> **Superseded 2026-08-04 — the transaction PIN was dropped.** Every PIN item below (the
> `Patron.pin_hash` field and `patron_change_pin` view in Phase 1, the PIN half of Phase 2,
> and the QR+PIN click-through in Phase 6) no longer applies. Patron identity at the desk is
> the QR scan alone; `pin_hash` was removed by migration `0017_remove_patron_pin_hash`. The
> QR requirements in those phases stand unchanged. Ch.1 ¶260 and Fig. 5 still describe the
> PIN and need manuscript-side edits.

---

## Phase 0 — Cleanup (do this first)

Only one concrete removal came out of the diff:

1. **Drop "System Log Report" from the Reports page.** `library/reports.py:28-35` (`REPORT_TYPES`) currently ends in `('system_log', 'System Log Report')` — remove that tuple entry (and adjust `SNAPSHOT_REPORTS` if needed once Stock Levels is added, see Phase 3). Remove the matching `<option>` from `templates/admin/reports.html`. Leave the `SystemLog` model (`models.py:442-470`) and `library/audit.py`'s `log_admin_action()` calls exactly as-is — they're cheap, still collecting real data, and there's no manuscript reason to rip out working audit instrumentation just because it's no longer a *report*. If it's truly unwanted, that's a one-line follow-up later; don't couple it to this pass.
2. No other dead code, orphaned templates, or duplicate logic surfaced. The Section-table removal and the Manage-Books/Shelf-Manager duplicate cleanup from prior sessions are already done and verified — nothing further to strip there.

---

## Phase 1 — Account Management overhaul (foundational)

Everything below (QR entry logging, QR+PIN transactions) depends on the Patron record actually having a QR code and a PIN, so this goes first.

**Current state:** `patron_register` (`views.py:125`) is a single-step form → immediate `account_status='Active'` → auto-login. No OTP, no approval queue, no `Patron.qr_code` (it existed once — see `migrations/0005_remove_patron_qr_code.py` — and was deliberately removed), no PIN field anywhere in the codebase.

**Manuscript requirement:** both online and on-site registration require Administrator approval before activation; online registration additionally requires an OTP email step; on approval, the system generates a unique Patron QR code; transactions later require that QR **plus a PIN** (a separate secret from the login password) for identity verification, staff/admin-operated only — patrons never scan anything themselves.

**Model changes** (`library/models.py`, new migration):
- `Patron.account_status`: add `'Pending'` to `STATUS_CHOICES`; new registrations default to `Pending`, not `Active`.
- `Patron.qr_code` — re-add (`CharField`, unique, nullable until approved).
- `Patron.pin_hash` — new field, hashed 4-6 digit PIN, set at registration or on first login.
- `Patron.registration_channel` — `Online` / `On-site`.
- `Patron.credential_document` — nullable file field (ID / proof-of-residency upload, online path only).
- Small `PatronOTP` model (or two fields `otp_code` + `otp_expires_at` directly on Patron) for the online email-verification step.

**View changes** (`library/views.py`):
- `patron_register` (line 125): split into the online self-service flow — submit form + upload credential → generate OTP → email it (new `library/emails.py` function) → verify OTP endpoint → land as `Pending`, **no session/login yet** (this is the biggest behavior change from today, where registration auto-logs the patron in).
- `entry_log_register` (line 3292, the on-site front-desk flow): the front-desk staff *is* the on-the-spot identity check the manuscript describes, so this path can set `Active` immediately (reasonable reading — flag this assumption to the user before/while building it, it's not 100% explicit in the text).
- New **Patron Approval Queue**: extend `admin_manage_patron` / `templates/admin/managepatron.html` with a Pending filter + Approve/Reject actions. Approve → generate `qr_code` (reuse the exact pattern already used for Books at `views.py:1351-1364`: `qrcode.QRCode(...)`, save to `media/qrcodes/patrons/patron_qr_<id>.png`) → set `Active` → email the patron their QR (new `send_registration_approved_email`).
- New `patron_change_pin` view + `patronaccount.html` UI (the manuscript names "Change PIN" as its own use case, distinct from "Change Password").
- Login/eligibility gates: `patron_login`, `check_patron_eligibility` (`library/eligibility.py`) must treat `Pending` as blocked, with a clear "awaiting approval" message distinct from "suspended."

**Templates:** `patronregister.html` (OTP step + upload input + "pending approval" messaging), `admin/managepatron.html` (Pending tab, Approve/Reject, QR display/print — mirror the QR block already in `admin/bookdetail.html`), `patron/patronaccount.html` (Change PIN + view own QR).

---

## Phase 2 — QR + PIN identity verification (builds on Phase 1)

1. **Entry/exit logging** (`entry_log_start` / `entry_log_exit`, `views.py:3250` & `3344`): currently name+email only. Add a QR-lookup path alongside it — new `search_patron_by_qr` endpoint mirroring the existing `search_book_by_qr` (`views.py:1260`) — so Library Staff/Admin can scan the patron's QR instead of typing. Keep name+email as the documented fallback; don't remove it.
2. **Transaction processing** (`process_transaction`, `views.py:1607`): today `patron_id` just comes from a search box, no PIN check anywhere. Add a "Scan Patron QR" tab next to the existing book Search/Scan-QR tabs (added 2026-06-19 per the transaction modal's `switchBookTab`/`addScannedBook` pattern — reuse that JS structure, don't rewrite it per the patron-design-system no-break rule) that resolves `qr_code → patron_id`, then require a PIN field server-side-validated against `pin_hash` before the transaction commits.
3. Templates: `admin/transaction.html` (+ its `library_staff/transaction.html` copy, since Staff does process transactions per the role matrix) get the new Scan-Patron-QR tab + PIN input.

---

## Phase 3 — Inventory Management Module (new, standalone)

This is the single largest addition — a full module with no existing code today (confirmed: no `Inventory`, `StockMovement`, or `stock` anywhere in `library/`). **Administrator-only**, per your decision.

**New models** (`library/models.py`):
- `InventoryRecord` — `book` (FK, nullable — a copy can be received before it's catalogued, per the manuscript: *"a received copy may therefore exist in inventory prior to being catalogued or shelved"*), `source` (Purchase/Donation), `condition` (Good/Damaged/Lost/Withdrawn), `qr_label` (own QR distinct from the catalog Book QR, generated the same `qrcode` way), `received_date`, `received_by` (FK User), `status`.
- `StockMovement` — `inventory_record` (FK), `action` (Received / ConditionChange / AuditAdjustment / Correction / Deaccession), `actor` (FK User), `reason`, `source`, `timestamp`. Every action that changes a copy's status writes one of these (this is what feeds the Stock Movement report).

**New views** (Administrator-only, `@admin_only_required`):
- Receive new stock (purchase or donation; condition inspection; print/apply QR label — reuse the Book QR-generation pattern at `views.py:1351`).
- Mark copy condition (damaged/lost/withdrawn) — also auto-triggered when a Transaction marks a book `Lost` (hook into the existing "Mark Lost" action in `admin_transaction_action`).
- Conduct stock audit — scan-based: compare expected holdings per shelf (via `ShelfLevel`/`Shelf` from the existing hierarchy) against copies actually scanned, produce a discrepancy report, require Admin confirmation before any adjustment is applied.
- Deaccession (data-entry-mistake correction only, not routine stock reduction — keep this distinct from the audit-adjustment path per the manuscript's wording).
- Update / view inventory record.

**Templates/nav:** new `templates/admin/inventoryadmin.html` + sidebar link in `templates/admin/_sidebar.html` only (no Staff sidebar entry, no `library_staff/` copy — per your Admin-only decision).

---

## Phase 4 — Reports: land the 7-type lineup

Once Phase 3's models exist:
- `library/reports.py` `REPORT_TYPES` (line 28): remove `system_log` (Phase 0), add `('stock_levels', 'Stock Levels Report')` and `('stock_movement', 'Stock Movement Report')`.
- `SNAPSHOT_REPORTS` (line 38): add `'stock_levels'` (point-in-time, like Books/Patrons — no date filter). Leave `stock_movement` date-filterable like Transactions/Patron Logs/Donations, since it's a movement log over time.
- Two new builder functions alongside the existing five in `reports.py`, consumed by the same PDF/Excel export pipeline (no changes needed there — it's already generic over report dicts).
- `templates/admin/reports.html`: update the type dropdown.

---

## Phase 5 — Communications: real-time chat

Not built at all today (only Announcements exist; no `ChatMessage`/`Conversation` model, no chat template). The manuscript calls for real-time Admin↔Patron chat, but the stack is plain Django on PythonAnywhere's free tier (`INSTALLED_APPS` has no Channels/ASGI, and PythonAnywhere free tier doesn't support WebSockets) — so this should be **short-interval AJAX polling**, the same pattern already proven for BLE positioning (polls every 1.5s). This is an infra-driven choice, not a shortcut.

- New `ChatMessage` model: `patron` (FK), `admin` (FK, nullable until a staff/admin replies), `sender_type`, `message`, `is_read`, `created_at`.
- Views: patron-side send/poll; admin-side conversation list (one thread per patron, unread badges) + thread send/poll.
- Templates: a patron chat widget (likely surfaced from the existing Announcements/Notifications area, per the design-system's no-break rule — don't touch unrelated IDs/JS hooks on that page) and an admin `Communications` or extended `Announcements` nav entry with a Chat tab.

---

## Phase 6 — Regression pass & verification

- Re-walk all three login portals (patron/staff/admin) once `Pending` status exists — make sure Suspended/Inactive/Pending all produce distinct, correct messaging and none of them can reach `process_transaction` or log in.
- End-to-end registration test: online path (form → OTP email → Pending → Admin approves → QR + welcome email arrive) and on-site path (front-desk registers → immediately Active).
- Re-verify existing eligibility (`library/eligibility.py`) still passes its 3-point check with the new `Pending` state added to the enum.
- Confirm Reports page renders all 7 types (PDF + Excel) including the two new Inventory-derived reports.
- Confirm the Inventory nav item appears only in the Administrator sidebar, never in Library Staff's.
- Manual click-through of QR+PIN transaction flow and QR-based entry/exit logging with a real test patron.

---

## Critical files (for implementation reference)

| Area | File |
|---|---|
| Models | `library/models.py` |
| Views (everything) | `library/views.py` (3576 lines — Patron reg. ~125, entry/exit logging ~3250-3400, transactions ~1607, book QR pattern ~1351) |
| URLs | `library/urls.py` |
| Reports | `library/reports.py` |
| Eligibility | `library/eligibility.py` |
| Email | `library/emails.py` |
| Audit | `library/audit.py` |
| Auth decorators | `library/auth_utils.py` |
| Admin templates | `templates/admin/*.html` + `_sidebar.html` |
| Staff templates | `templates/library_staff/*.html` + `_sidebar.html` |
| Patron templates | `templates/patron/*.html` |

## Verification approach

Run the existing dev server (`python manage.py runserver`) against the live Postgres/Supabase dev DB already configured in `.env`. For each phase: exercise the new flow through the actual browser UI (register → OTP → approve → QR; scan-based entry log; QR+PIN transaction; Inventory receive/audit; Reports PDF/Excel for the new types; chat send/poll both sides) rather than relying on migrations succeeding alone. Django's `manage.py check --deploy` should stay clean under `DEBUG=False` as already established.
