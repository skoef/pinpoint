from django.apps import AppConfig


class GameConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "game"

    def ready(self):
        from . import dbsync

        if dbsync.enabled():
            dbsync.connect_signals()
