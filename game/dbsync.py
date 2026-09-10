"""Persist the SQLite database to S3.

The container has no durable disk, so the database is downloaded on start and
uploaded again after writes. Reads vastly outnumber writes, so instead of
uploading on every change a signal touches a marker file and a loop in
``entrypoint.sh`` uploads whenever it finds one.

This is safe for a **single** instance only. Two containers would each download
their own copy and the last upload would silently discard the other's writes.
"""

import logging
import os
import sqlite3
import tempfile

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)


DISPATCH_UID = "pinpoint.dbsync.on_write"


def enabled():
    return bool(getattr(settings, "SQLITE_S3_SYNC", False))


def on_write(sender, **kwargs):
    """Any model save/delete means the database needs uploading again."""
    mark_dirty()


def _write_signals():
    from django.db.models.signals import m2m_changed, post_delete, post_save

    return (post_save, post_delete, m2m_changed)


def connect_signals():
    """Watch every model for writes, not just this app's.

    sender=None covers django.contrib.sessions (player progress) and auth (GM
    accounts) too. weak=False matters: a receiver connected from AppConfig.ready()
    is otherwise held only weakly and gets garbage collected the moment ready()
    returns, silently unhooking it.
    """
    for signal in _write_signals():
        signal.connect(on_write, weak=False, dispatch_uid=DISPATCH_UID)


def disconnect_signals():
    for signal in _write_signals():
        signal.disconnect(dispatch_uid=DISPATCH_UID)


def database_path():
    return str(settings.DATABASES["default"]["NAME"])


def marker_path():
    return settings.SQLITE_DIRTY_MARKER


def mark_dirty():
    """Record that the database changed. Cheap, and called on every write."""
    try:
        with open(marker_path(), "w") as handle:
            handle.write("1")
    except OSError:
        # Never fail a request because the marker could not be written; the
        # worst case is that this change waits for the next write to be synced.
        logger.exception("could not touch the dirty marker")


def clear_marker():
    try:
        os.unlink(marker_path())
    except FileNotFoundError:
        pass


def is_dirty():
    return os.path.exists(marker_path())


def _client():
    import boto3

    # endpoint_url is only needed for S3-compatible stores (MinIO and friends);
    # leaving it unset uses real S3. Credentials come from the environment or,
    # preferably in production, the instance's IAM role.
    return boto3.client(
        "s3",
        region_name=os.environ.get("AWS_S3_REGION_NAME") or None,
        endpoint_url=os.environ.get("AWS_S3_ENDPOINT_URL") or None,
    )


def snapshot(destination):
    """Copy the live database out with SQLite's online-backup API.

    A plain file copy is not safe here: it can catch a write mid-transaction,
    and under WAL the newest commits live outside the main file. The backup API
    handles both, while readers and writers keep working.
    """
    source = sqlite3.connect(f"file:{database_path()}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def _error_bits(exc):
    """Pull the HTTP status and error code out of a botocore ClientError."""
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    code = str(exc.response.get("Error", {}).get("Code", ""))
    return status, code


def download():
    """Fetch the database from S3. Returns True if a copy was restored.

    A missing object is not an error: on the very first deploy there is nothing
    to restore and ``migrate`` will create the file. Anything else -- in
    particular a 403 -- has to stop the container, because starting with an
    empty database would destroy the real one at the next upload.
    """
    from botocore.exceptions import ClientError

    bucket, key, path = settings.SQLITE_S3_BUCKET, settings.SQLITE_S3_KEY, database_path()
    client = _client()

    # Ask about the object explicitly rather than letting download_file's own
    # HeadObject fail, so the two cases can be told apart and reported usefully.
    try:
        client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        status, code = _error_bits(exc)
        if status == 404 or code in ("404", "NoSuchKey", "NoSuchBucket"):
            logger.warning("no database at s3://%s/%s yet; starting empty", bucket, key)
            return False
        if status == 403 or code in ("403", "AccessDenied", "Forbidden"):
            raise ImproperlyConfigured(
                f"S3 refused to say whether s3://{bucket}/{key} exists (403 "
                f"Forbidden; region={os.environ.get('AWS_S3_REGION_NAME') or 'default'}).\n"
                "\n"
                "Refusing to start with an empty database: if that object does "
                "exist, the first write would upload an empty database over it.\n"
                "\n"
                "S3 answers 403 instead of 404 for a missing object when the "
                "caller lacks s3:ListBucket, so on a first deploy this is most "
                "likely that missing permission rather than a real denial. "
                "Adding s3:HeadObject does not help -- HeadObject is authorised "
                "by s3:GetObject, and the 403 is about disclosing existence.\n"
                "\n"
                "The identity needs, on the bucket itself:\n"
                "    s3:ListBucket   on arn:aws:s3:::%(bucket)s\n"
                "and on the object:\n"
                "    s3:GetObject, s3:PutObject "
                "on arn:aws:s3:::%(bucket)s/%(key)s\n"
                "\n"
                "Also check the bucket really is in the region above; a "
                "mismatch can surface as a 403 too."
                % {"bucket": bucket, "key": key}
            ) from exc
        raise

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    client.download_file(bucket, key, path)
    logger.info("restored database from s3://%s/%s", bucket, key)
    return True


def upload():
    """Upload a consistent snapshot of the database to S3."""
    bucket, key = settings.SQLITE_S3_BUCKET, settings.SQLITE_S3_KEY
    handle, tmp = tempfile.mkstemp(suffix=".sqlite3")
    os.close(handle)
    try:
        snapshot(tmp)
        _client().upload_file(tmp, bucket, key)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    logger.info("uploaded database to s3://%s/%s", bucket, key)
