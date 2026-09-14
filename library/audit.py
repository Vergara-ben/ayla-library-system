"""Activity logging for every portal."""

import logging

from .models import SystemLog, User, Patron

logger = logging.getLogger(__name__)


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
        # Never let logging break the main action.
        logger.exception('Could not write the audit record')


def log_admin_action(request, action, entity_type, entity_id=None, detail='', patron=None):
    """Record an action taken in the Administrator or Library Staff portal."""
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
        logger.exception('Could not write the audit record')


def log_patron_action(request, action, entity_type, entity_id=None, detail='', patron=None):
    """Record an action taken by a patron in the Patron portal."""
    try:
        if patron is None:
            patron_id = request.session.get('patron_id') if request else None
            patron = Patron.objects.filter(patron_id=patron_id).first() if patron_id else None
        if patron is None:
            # The catalogue and map are open to visitors who have not registered.
            return
        _write(
            'Patron',
            patron=patron,
            name=patron.fullname if patron else 'Unknown patron',
            action=action, entity_type=entity_type, entity_id=entity_id, detail=detail,
        )
    except Exception:
        logger.exception('Could not write the audit record')


def log_system_action(action, entity_type, entity_id=None, detail=''):
    """Record something the system did on its own."""
    _write('System', name='System', action=action,
           entity_type=entity_type, entity_id=entity_id, detail=detail)
