"""Activity logging for every portal.

The manuscript's ERD (Figure 87) relates Activity Logs to Patrons *and* Staff.
This module started admin-only, which is why the original entry point is named
``log_admin_action`` and resolves its actor from ``session['admin_id']`` -- that
name is kept because 58 call sites use it, but it is now one of three.

Every function here is best-effort: an audit write must never be the reason a
borrow fails or a patron cannot log in, so the whole body is guarded. The
trade-off is deliberate and worth naming -- a lost log line is preferable to a
broken transaction, so this is not a security-grade tamper-proof trail.
"""

from .models import SystemLog, User, Patron


def _write(actor_role, admin=None, patron=None, name=None,
           action='', entity_type='', entity_id=None, detail=''):
    try:
        SystemLog.objects.create(
            admin=admin,
            patron=patron,
            actor_role=actor_role,
            admin_name=(name or 'Unknown')[:255],
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id is not None else None,
            detail=(detail or '')[:500],
        )
    except Exception:
        # Auditing must never interfere with the primary operation.
        pass


def log_admin_action(request, action, entity_type, entity_id=None, detail='', patron=None):
    """Record an action taken in the Administrator or Library Staff portal.

    The two share a session key and a table; ``session['admin_role']`` is what
    separates them, so it decides the role stored rather than assuming 'Admin'.

    ``patron`` names the person an action was performed *on* -- a borrow is
    carried out by staff but belongs to a patron's history too. Attaching them
    to the one row keeps a single truthful record of who did it, while still
    letting the log be searched from the patron's side. Writing a second
    patron-attributed row instead would say the patron did it, which is not
    what happened at the desk.
    """
    try:
        admin_id = request.session.get('admin_id')
        admin = User.objects.filter(admin_id=admin_id).first() if admin_id else None
        role = request.session.get('admin_role') or 'Admin'
        if role not in ('Admin', 'Staff'):
            role = 'Admin'
        _write(
            role,
            admin=admin,
            patron=patron,
            name=admin.fullname if admin else request.session.get('admin_fullname'),
            action=action, entity_type=entity_type, entity_id=entity_id, detail=detail,
        )
    except Exception:
        pass


def log_patron_action(request, action, entity_type, entity_id=None, detail='', patron=None):
    """Record an action taken by a patron in the Patron portal.

    ``patron`` may be passed directly for the cases where there is no session
    to read it from yet -- a login being recorded before the session is
    established, or a password reset performed while signed out.
    """
    try:
        if patron is None:
            patron_id = request.session.get('patron_id') if request else None
            patron = Patron.objects.filter(patron_id=patron_id).first() if patron_id else None
        if patron is None:
            # The catalogue and map are open to visitors who have not
            # registered. Their searches are real but unattributable, and a
            # trail of "Unknown patron" rows would read as failures to identify
            # someone rather than as people who were never identified at all.
            return
        _write(
            'Patron',
            patron=patron,
            name=patron.fullname if patron else 'Unknown patron',
            action=action, entity_type=entity_type, entity_id=entity_id, detail=detail,
        )
    except Exception:
        pass


def log_system_action(action, entity_type, entity_id=None, detail=''):
    """Record something the system did on its own.

    Overdue sweeps and auto-closed visits change patron-visible records with no
    human behind them. Attributing those to whichever admin happened to be
    signed in would be worse than useless in an audit trail, so they get their
    own role and no actor.
    """
    _write('System', name='System', action=action,
           entity_type=entity_type, entity_id=entity_id, detail=detail)
