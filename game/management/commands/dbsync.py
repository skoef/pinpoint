"""Move the SQLite database between the container and S3.

Called from ``entrypoint.sh``: once with --download on start, then with --upload
whenever the dirty marker shows the database changed.

Using boto3 (already a dependency) rather than the AWS CLI keeps ~60 MB of
tooling out of the image.
"""

from django.core.management.base import BaseCommand, CommandError

from game import dbsync


class Command(BaseCommand):
    help = "Download the SQLite database from S3, or upload it back."

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--download", action="store_true",
                           help="Restore the database from S3 if it exists there.")
        group.add_argument("--upload", action="store_true",
                           help="Upload a consistent snapshot to S3.")
        parser.add_argument(
            "--if-dirty", action="store_true",
            help="With --upload, do nothing unless the dirty marker is present.",
        )

    def handle(self, *args, **options):
        if not dbsync.enabled():
            # Not an error: this is the normal state locally and when Postgres
            # is configured, so the entrypoint can call it unconditionally.
            self.stdout.write("SQLite/S3 sync is not configured; nothing to do.")
            return

        if options["download"]:
            restored = dbsync.download()
            self.stdout.write(
                "Database restored from S3." if restored
                else "No database in S3 yet; a new one will be created."
            )
            return

        # --upload
        if options["if_dirty"] and not dbsync.is_dirty():
            #self.stdout.write("No writes since the last upload; skipping.")
            return

        # Clear the marker *before* snapshotting. A write landing during the
        # upload re-touches it, so the next pass picks that change up. Clearing
        # afterwards would drop it instead.
        dbsync.clear_marker()
        try:
            dbsync.upload()
        except Exception as exc:
            # Put the marker back so the next pass retries rather than losing
            # the change because of a transient S3 failure.
            dbsync.mark_dirty()
            raise CommandError(f"upload failed, will retry: {exc}") from exc
        self.stdout.write("Database uploaded to S3.")
