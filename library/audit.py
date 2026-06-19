"""Audit logging for administrative actions.

``log_admin_action`` records who did what to which entity. It is
best-effort: any failure here must never break the underlying admin
operation, so the whole body is guarded.
"""

from .models import SystemLog, User


def log_admin_action(request, action, entity_type, entity_id=None, detail=''):
    """Record an admin action in the SystemLog audit trail.

    Args:
        request: the current request (used to resolve the acting admin).
        action: short verb, e.g. 'Create', 'Update', 'Delete', 'Login', 'Process'.
        entity_type: the object type acted on, e.g. 'Book', 'Patron'.
        entity_id: optional identifier of the affected record.
        detail: optional human-readable description.
    """
    try:
        admin_id = request.session.get('admin_id')
        admin = User.objects.filter(admin_id=admin_id).first() if admin_id else None
        SystemLog.objects.create(
            admin=admin,
            admin_name=admin.fullname if admin else (request.session.get('admin_fullname') or 'Unknown'),
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id is not None else None,
            detail=(detail or '')[:500],
        )
    except Exception:
        # Auditing must never interfere with the primary operation.
        pass
