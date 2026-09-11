"""Template context shared by every page."""

from . import __version__


def version(request):
    """Expose the app version, so templates need no view support for it.

    A context processor rather than view context because the login page is
    rendered by Django's own LoginView, which we would otherwise have to
    subclass just to pass one string.
    """
    return {"app_version": __version__}
