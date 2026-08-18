"""Known-answer tests for the key handling in storage.py.

    python -m pytest tests -q

Only the pure key logic is covered here - what list_keys filters out, and what
full_key refuses to build. Nothing in this file reaches the network. The
listing tests use a hand-written stand-in for the boto3 client rather than a
mocking framework, because the whole of what it has to do is hand back a page
of keys, and a reader can see exactly what the bucket is pretending to hold.

The filtering matters more than it looks. This bucket is shared with other GSS
projects and it writes bookkeeping objects of its own into the prefixes this
project uses, so "what is in the bucket" and "what this project put there" are
not the same list. Every count of rotating sets, every promotion decision and
every prune depends on telling them apart.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import storage

PREFIX = "authorizations/backups/water_licensed_works/"


def store_holding(*relative_keys):
    """A Storage whose bucket contains exactly these keys, prefix included.

    The stand-in answers get_paginator and nothing else, which is all
    list_keys asks of the client.
    """
    pages = [{"Contents": [{"Key": PREFIX + key} for key in relative_keys]}]
    client = SimpleNamespace(
        get_paginator=lambda name: SimpleNamespace(paginate=lambda **kwargs: pages)
    )
    return storage.Storage(client=client, bucket="gssgeodrive", prefix=PREFIX)


# ---------------------------------------------------------------------------
# What a listing must leave out
# ---------------------------------------------------------------------------

def test_the_backend_folder_markers_are_not_returned():
    """Observed live on 2026-08-17: '_$folder$' and 'metrics/_$folder$' both
    appeared on their own the first time the check job wrote a metrics file,
    having been absent from a listing taken minutes before. They are zero-byte
    bookkeeping objects, not artifacts."""
    store = store_holding(
        "_$folder$",
        "metrics/_$folder$",
        "metrics/2026-08-14.json",
        "metrics/2026-08-17.json",
    )
    assert storage.list_keys(store) == [
        "metrics/2026-08-14.json", "metrics/2026-08-17.json"
    ]


def test_a_marker_cannot_make_a_tier_look_promoted():
    """The sharp end of the previous test. backup.promote_monthly and
    promote_yearly both decide a tier is done by asking whether anything at all
    exists under its prefix, so a stray marker would skip that month's
    promotion permanently - and the yearly path returns without even a log
    line."""
    store = store_holding("monthly/2026-09/_$folder$", "yearly/2026/_$folder$")
    assert storage.list_keys(store, "monthly/2026-09/") == []
    assert storage.list_keys(store, "yearly/2026/") == []


def test_the_prefix_placeholder_is_not_returned():
    """A 0-byte object whose key *is* the prefix. Counting it makes every "how
    many rotating sets are there" answer one too many, which is how pruning
    deletes a set it should have kept."""
    store = store_holding("", "rotating/2026-08-14/lines.gdb.zip")
    assert storage.list_keys(store) == ["rotating/2026-08-14/lines.gdb.zip"]


def test_sub_folder_placeholders_are_not_returned():
    store = store_holding("rotating/", "rotating/2026-08-14/",
                          "rotating/2026-08-14/manifest.json")
    assert storage.list_keys(store) == ["rotating/2026-08-14/manifest.json"]


def test_real_keys_come_back_relative_and_oldest_first():
    """Relative so a listing can be handed straight to download_file or
    delete_key, and sorted because pruning and promotion both read date-stamped
    names in order."""
    store = store_holding(
        "rotating/2026-08-17/manifest.json",
        "rotating/2026-08-12/manifest.json",
        "rotating/2026-08-14/manifest.json",
    )
    assert storage.list_keys(store) == [
        "rotating/2026-08-12/manifest.json",
        "rotating/2026-08-14/manifest.json",
        "rotating/2026-08-17/manifest.json",
    ]


def test_an_empty_prefix_lists_everything_the_project_owns():
    store = store_holding("metrics/2026-08-17.json", "status/checks-x.json")
    assert len(storage.list_keys(store)) == 2


# ---------------------------------------------------------------------------
# What a key is not allowed to be
#
# The bucket is shared with other GSS projects and this one holds a full-access
# key pair, so full_key is the only thing standing between a bad argument and
# another project's data.
# ---------------------------------------------------------------------------

def test_a_relative_key_is_joined_onto_the_project_prefix():
    store = store_holding()
    assert storage.full_key(store, "metrics/2026-08-17.json") == (
        PREFIX + "metrics/2026-08-17.json"
    )


def test_windows_backslashes_are_normalised():
    """os.path.join on a developer machine produces
    'rotating\\2026-08-14\\lines.gdb.zip', which S3 stores verbatim as one long
    name the Linux runner would never produce or match."""
    store = store_holding()
    assert storage.full_key(store, r"rotating\2026-08-14\lines.gdb.zip") == (
        PREFIX + "rotating/2026-08-14/lines.gdb.zip"
    )


def test_a_key_that_would_escape_the_project_prefix_is_refused():
    store = store_holding()
    for bad_key in ("../other_project/secret.zip", "rotating/../../escape.zip"):
        with pytest.raises(ValueError, match=r"\.\."):
            storage.full_key(store, bad_key)


def test_a_leading_slash_is_refused():
    store = store_holding()
    with pytest.raises(ValueError, match="starts with"):
        storage.full_key(store, "/rotating/2026-08-14/lines.gdb.zip")


def test_a_key_that_already_carries_the_prefix_is_refused():
    """Otherwise it would silently write to prefix + prefix + key, which is a
    valid key nobody would ever go looking for."""
    store = store_holding()
    with pytest.raises(ValueError, match="already begins with"):
        storage.full_key(store, PREFIX + "metrics/2026-08-17.json")


def test_an_empty_key_is_refused():
    store = store_holding()
    for bad_key in ("", "   ", None):
        with pytest.raises(ValueError, match="empty key"):
            storage.full_key(store, bad_key)
