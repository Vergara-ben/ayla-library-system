"""
URL configuration for ayla_library_system project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include, re_path
from django.conf import settings
from django.conf.urls.static import static
from django.views.generic import RedirectView
from django.views.static import serve as serve_media

urlpatterns = [
    path('admin/', admin.site.urls),
    # Somebody typing the bare domain got a 404. The public face of a library
    # system is the catalogue, so that is where the front door leads.
    path('', RedirectView.as_view(url='/patron/dashboard/', permanent=False)),
    path('', include('library.urls')),
]

# Always serve uploaded media (floor plans, QR images, book covers).
# The app relies on user-uploaded images at runtime, so this must work
# regardless of DEBUG — django.conf.urls.static.static() is a no-op when
# DEBUG is False, which silently 404s every /media/ URL.
#
# Everything except credentials/. Those are photographs of government IDs and
# are served instead by library.views.serve_patron_credential, behind a login
# and a module check. This pattern refuses them outright rather than relying on
# the credential route being the one people happen to use: the whole point is
# that no unauthenticated path to those files exists.
urlpatterns += [
    re_path(r'^media/(?!credentials/)(?P<path>.*)$', serve_media,
            {'document_root': settings.MEDIA_ROOT}),
]

# Static files: only needed via runserver in development.
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
