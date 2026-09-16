"""URL configuration for ayla_library_system project."""
from django.urls import path, include, re_path
from django.conf import settings
from django.conf.urls.static import static
from django.views.generic import RedirectView
from django.views.static import serve as serve_media

urlpatterns = [
    # Somebody typing the bare domain got a 404.
    path('', RedirectView.as_view(url='/patron/dashboard/', permanent=False)),
    path('', include('library.urls')),
]

# Always serve uploaded media (floor plans, QR images, book covers).
urlpatterns += [
    re_path(r'^media/(?!credentials/)(?P<path>.*)$', serve_media,
            {'document_root': settings.MEDIA_ROOT}),
]

# Static files: only needed via runserver in development.
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
