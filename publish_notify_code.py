"""Publish the notification job's code to object storage.

    python publish_notify_code.py

Runs in GitHub Actions on every push to main that touches either file, and
uploads exactly two of them plus a manifest:

    code/notify.py     the notification job itself
    code/config.yml    the routing table, schedule and grace periods
    code/manifest.json the commit they came from, and a SHA-256 of each

It exists because the Jenkins agents that run the notification job cannot
reach github.com, and can reach object storage. So the bucket carries the code
to them: Actions publishes on every push, the Jenkins job downloads at the
start of every build, and neither end has to remember to copy anything. The
alternative was a clone on a network share that somebody refreshes by hand,
which goes stale silently - and a notifier running last month's routing table
is exactly the kind of fault nothing else would report.

This writes only under the code/ prefix. It touches no backup, no metrics file
and no status object, and it never reads or writes ArcGIS Online.

Needs config.yml and the object storage credentials.
"""

import hashlib
import json
import logging
import sys

import yaml

import storage
from status import resolve_code_version, utc_now, utc_stamp

logger = logging.getLogger("publish_notify_code")

CONFIG_PATH = "config.yml"

MANIFEST_NAME = "manifest.json"

# Local path -> the name it is published under. The Jenkins job's bootstrap
# reverses this mapping, so the two are one contract written in two places and
# tests/test_notify.py asserts they still agree.
PUBLISHED_FILES = {
    "jenkins/notify.py": "notify.py",
    "config.yml": "config.yml",
}

BLOCK_BYTES = 1024 * 1024


def load_config(path):
    """Read config.yml. Everything the pipeline is told lives in there."""
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def sha256_of(path):
    """The checksum recorded in the manifest and verified on download.

    A truncated notify.py would fail on import and be obvious. A truncated
    config.yml is the dangerous one: it can still parse as valid YAML with
    whole sections missing, which would leave the notifier routing alerts by a
    table that is no longer complete. Worth the six lines.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(BLOCK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def publish(store, config):
    """Upload both files and the manifest describing them.

    The manifest is written last, so a reader that has it knows both files
    beside it are complete.
    """
    code_path = config["storage"]["paths"]["code"]
    code_version = resolve_code_version()

    checksums = {}
    for local_path, published_name in sorted(PUBLISHED_FILES.items()):
        checksums[published_name] = sha256_of(local_path)
        storage.upload_file(store, local_path, f"{code_path}{published_name}")

    manifest = {
        "code_version": code_version,
        "published_utc": utc_stamp(utc_now()),
        "files": checksums,
    }
    storage.write_bytes(
        store,
        f"{code_path}{MANIFEST_NAME}",
        json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
    )
    logger.info(
        "Published %s to %s at code version %s",
        ", ".join(sorted(checksums)), code_path, code_version,
    )
    return manifest


def main():
    config = load_config(CONFIG_PATH)
    logging.basicConfig(
        level=config.get("logging", {}).get("level", "INFO"),
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    publish(storage.connect_to_storage(config), config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
