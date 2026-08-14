"""Object storage for the Water Licensed Works backup pipeline.

A thin wrapper over boto3 for the one bucket and the one prefix this project
is allowed to touch. Imported by backup.py, checks.py and status.py. The
Jenkins notify job deliberately does not import it - that job runs on the GIS
server with boto3 and standard library only.

Every function takes a key *relative to the project prefix* and joins the
prefix on internally, so no caller can construct a key elsewhere in the
bucket by accident. gssgeodrive is shared with other GSS projects and this
project must never read, list or delete outside its own prefix. list_keys
gives back relative keys as well, so a listing can be handed straight to
download_file, key_exists or delete_key.

Needs S3_NRS_ENDPOINT, S3_GSS_GEODRIVE_KEY_ID and S3_GSS_GEODRIVE_SECRET_KEY.
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger("storage")


@dataclass
class Storage:
    """A boto3 client bound to the bucket and prefix it is allowed to use.

    The three values travel together on purpose. Holding the prefix next to
    the client is what lets every function below join it on without the
    caller passing it, which is the whole of the protection against writing
    into another GSS project's space. Build it with connect_to_storage().
    """

    client: object
    bucket: str
    prefix: str


def connect_to_storage(config):
    """Build the client once, from the environment, bound to config.yml's
    bucket and prefix.

    endpoint_url is not optional. This is NRS S3-compatible storage, not AWS:
    left unset, boto3 resolves an AWS endpoint instead and fails somewhere
    that reads like a credentials problem rather than a missing setting.
    """
    endpoint = os.getenv("S3_NRS_ENDPOINT")
    key_id = os.getenv("S3_GSS_GEODRIVE_KEY_ID")
    secret_key = os.getenv("S3_GSS_GEODRIVE_SECRET_KEY")

    missing = [
        name
        for name, value in [
            ("S3_NRS_ENDPOINT", endpoint),
            ("S3_GSS_GEODRIVE_KEY_ID", key_id),
            ("S3_GSS_GEODRIVE_SECRET_KEY", secret_key),
        ]
        if not value
    ]
    if missing:
        raise ValueError(
            f"Object storage settings are missing from the environment: "
            f"{', '.join(missing)}. On a developer machine they mirror the "
            f"GitHub Actions secrets of the same name. Run preflight.py to "
            f"confirm the whole set."
        )

    prefix = config["storage"]["prefix"]
    # Without the trailing slash the prefix concatenates onto the first key
    # segment - 'water_licensed_worksrotating/2026-08-14/...' - which is a
    # perfectly valid key that nobody would ever go looking for.
    if not prefix.endswith("/"):
        prefix += "/"

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=key_id,
        aws_secret_access_key=secret_key,
    )
    return Storage(client=client, bucket=config["storage"]["bucket"], prefix=prefix)


def full_key(storage, key):
    """Join a project-relative key onto the configured prefix.

    Every other function here goes through this, so it is the single place a
    key is checked before it reaches the shared bucket.
    """
    if key is None or not str(key).strip():
        raise ValueError(
            "An empty key was passed to storage.py. Keys are relative to the "
            "project prefix, for example 'rotating/2026-08-14/lines.gdb.zip'."
        )

    # Backslashes are a Windows accident rather than part of a name:
    # os.path.join on a developer machine produces
    # 'rotating\\2026-08-14\\lines.gdb.zip', which S3 stores verbatim as one
    # long name the Linux runner would never produce or match.
    key = str(key).replace("\\", "/")

    if key.startswith("/"):
        raise ValueError(
            f"Key '{key}' starts with '/'. Keys are relative to the project "
            f"prefix '{storage.prefix}', so pass "
            f"'rotating/2026-08-14/lines.gdb.zip' rather than a leading slash."
        )
    if ".." in key.split("/"):
        raise ValueError(
            f"Key '{key}' contains '..', which would point outside the project "
            f"prefix '{storage.prefix}' and into the shared gssgeodrive bucket."
        )
    if key.startswith(storage.prefix):
        raise ValueError(
            f"Key '{key}' already begins with the project prefix. Every "
            f"function in storage.py joins the prefix itself, so this would "
            f"write to '{storage.prefix}{key}'. Pass the part after the "
            f"prefix, which is also the form list_keys returns."
        )

    return storage.prefix + key


def list_keys(storage, key_prefix=""):
    """List this project's keys, relative to the project prefix.

    key_prefix is itself relative, so list_keys(storage, 'rotating/') returns
    ['rotating/2026-08-14/lines.gdb.zip', ...]. Called with no prefix it
    returns everything the project owns and nothing else.

    Two things this has to get right.

    The project prefix contains a 0-byte placeholder object whose key *is* the
    prefix, left behind when the folder was created. It is not an artifact,
    and counting it makes every "how many rotating sets are there" answer one
    too many - which is how pruning ends up deleting a set it should have
    kept. Sub-folder placeholders of the same kind end in '/'.

    And list_objects_v2 returns at most 1,000 keys per call without saying so:
    no error, just a short answer. That is the exact shape of failure this
    project has already been bitten by, hence the paginator rather than a
    single call.
    """
    search_prefix = full_key(storage, key_prefix) if key_prefix else storage.prefix

    keys = []
    paginator = storage.client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=storage.bucket, Prefix=search_prefix):
        for entry in page.get("Contents", []):
            relative = entry["Key"][len(storage.prefix):]
            if not relative or relative.endswith("/"):
                continue
            keys.append(relative)

    # Sorted so that date-stamped keys come back oldest first, which is what
    # pruning and promotion both read them in.
    return sorted(keys)


def upload_file(storage, local_path, key):
    """Upload a local file and return the full key it was written to.

    "Never overwrite in place" (DESIGN.md 6.5) is a property of the keys the
    caller chooses, not something enforced here - S3 has no create-only put,
    so writing an existing key replaces it. That is why backup.py publishes
    each run under a new date-stamped prefix.

    upload_file rather than put_object: it streams from disk instead of
    reading the whole artifact into memory, and switches to a multipart
    upload above 8 MB, which the 14.5 MB lines artifact crosses. The bucket
    lifecycle rule aborts incomplete multipart uploads after 14 days, so a
    run that dies mid-upload leaves nothing behind.
    """
    destination = full_key(storage, key)
    storage.client.upload_file(
        Filename=str(local_path), Bucket=storage.bucket, Key=destination
    )
    logger.info(
        "Uploaded %s to s3://%s/%s (%s bytes)",
        local_path,
        storage.bucket,
        destination,
        f"{os.path.getsize(local_path):,}",
    )
    return destination


def download_file(storage, key, local_path):
    """Download one object to a local path and return that path.

    The parent directory is created if it is not there, because the caller is
    normally writing into a fresh temporary directory.
    """
    source = full_key(storage, key)
    Path(local_path).parent.mkdir(parents=True, exist_ok=True)
    storage.client.download_file(
        Bucket=storage.bucket, Key=source, Filename=str(local_path)
    )
    logger.info(
        "Downloaded s3://%s/%s to %s (%s bytes)",
        storage.bucket,
        source,
        local_path,
        f"{os.path.getsize(local_path):,}",
    )
    return local_path


def read_bytes(storage, key):
    """Fetch one object into memory and return its bytes.

    For the small JSON objects the pipeline reads back - status objects,
    manifests - where writing them to a temporary file first would be
    ceremony. Never use it for an artifact: download_file streams to disk
    instead of holding the whole geodatabase in memory.
    """
    source = full_key(storage, key)
    return storage.client.get_object(Bucket=storage.bucket, Key=source)["Body"].read()


def copy_key(storage, source_key, destination_key):
    """Copy an object within the project prefix, server side.

    This is how a rotating set becomes a monthly or yearly one (DESIGN.md
    6.4). Promotion copies an artifact that has already been validated and
    checked rather than exporting again: a second export would produce a
    different snapshot taken at a different moment, and would add two
    low-frequency scheduled jobs that could fail unnoticed.

    Both keys go through full_key, so a copy cannot reach outside the project
    prefix at either end.
    """
    source = full_key(storage, source_key)
    destination = full_key(storage, destination_key)
    storage.client.copy_object(
        Bucket=storage.bucket,
        Key=destination,
        CopySource={"Bucket": storage.bucket, "Key": source},
    )
    logger.info("Copied %s to %s", source_key, destination_key)
    return destination


def key_exists(storage, key):
    """Is there an object at this key? A head request, so nothing is downloaded."""
    try:
        storage.client.head_object(Bucket=storage.bucket, Key=full_key(storage, key))
        return True
    except ClientError as exc:
        # 404 is the answer to the question that was asked. Anything else -
        # bad credentials, wrong bucket, endpoint unreachable - is a real
        # failure and must not be reported as "the object is not there",
        # which would let a backup run treat a storage outage as an empty
        # bucket and start publishing over the top of it.
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return False
        raise


def key_size(storage, key):
    """Size in bytes of the object at this key.

    Upload confirmation in DESIGN.md 6.5 step 5 is a head request comparing
    the stored size against what was sent, done before anything old is
    pruned. Raises if the key is absent, because a missing object at this
    point in a run is a failure rather than a question.
    """
    head = storage.client.head_object(Bucket=storage.bucket, Key=full_key(storage, key))
    return head["ContentLength"]


def delete_key(storage, key):
    """Delete one object, and log it.

    This is the only function in the project that removes anything from the
    bucket. It deletes exactly the key it is given: no wildcards, no
    recursion, no tidying up of neighbouring keys while it is there.

    The deletion is logged unconditionally. The bucket issues a single
    full-access key pair (DESIGN.md 6.6), so there is no permission boundary
    preventing the pipeline from deleting a backup and the log is the only
    record of what it did. Versioning makes a delete recoverable, but only
    for the 60 days the bucket lifecycle rule keeps noncurrent versions.
    """
    target = full_key(storage, key)
    storage.client.delete_object(Bucket=storage.bucket, Key=target)
    logger.info("Deleted s3://%s/%s", storage.bucket, target)
