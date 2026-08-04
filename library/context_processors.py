"""Template context shared across the portal templates."""

from .models import User


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
    return {'allowed_modules': user.module_keys}
