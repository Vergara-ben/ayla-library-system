from django.apps import AppConfig


class LibraryConfig(AppConfig):
    name = 'library'

    def ready(self):
        # A donated book is released when it is put on a shelf.
        from . import accession  # noqa: F401
