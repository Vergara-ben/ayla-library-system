"""Template context shared across the portal templates."""

from .models import Conversation, User


def staff_modules(request):
    """Expose the signed-in Staff account's granted modules to every template.

    Lets templates/library_staff/_sidebar.html hide the modules an account was
    not granted without every staff view having to pass them in. Admins get all
    keys, so admin templates are unaffected.
    """
    admin_id = request.session.get('admin_id')
    if not admin_id:
        return {}
    user = User.objects.filter(admin_id=admin_id).first()
    if user is None:
        return {}
    # Surfaced on every portal page: an enquiry nobody has answered should be
    # visible from wherever the librarian happens to be working, not only from
    # the Messages page they have no reason to open.
    return {
        'allowed_modules': user.module_keys,
        'unanswered_messages': Conversation.objects.filter(status='Open').count(),
    }
