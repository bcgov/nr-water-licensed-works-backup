"""Known-answer tests for the restore tool and, mostly, for its fence.

    python -m pytest tests -q

This is the only file in the repository that can delete a feature, so most of
what is asserted here is not behaviour but *shape*: that nothing imports it,
that no workflow can reach it, that it reads no configuration, that it holds
no storage credentials, and that the production items are refused before
anything else in the run happens.

The behavioural half covers the refusals themselves and the artifact
verification, which are the steps standing between a mistyped item ID and an
emptied production layer. Every one of them is a pure function of its
arguments, so all of it runs with no network, no credentials and no layer.

Nothing here touches ArcGIS Online. Nothing here is capable of it.
"""

import ast
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "restore"))

import restore_layer
import status

LINES_ITEM = "a938dd3fcafa4b98baee3fcb5ab59fe8"
POINTS_ITEM = "848f3c3900da4b1b907a415154a53042"
TEST_COPY_ITEM = "0000aaaa1111bbbb2222cccc3333dddd"

# The pipeline. None of these may import the restore tool, in either
# direction of reading: that is what keeps "the pipeline cannot alter
# production data" a property of the code rather than a promise.
PIPELINE_MODULES = [
    "backup.py",
    "checks.py",
    "storage.py",
    "status.py",
    "preflight.py",
    "run_backup.py",
    "run_checks.py",
    "publish_notify_code.py",
    Path("jenkins") / "notify.py",
]


def restore_source():
    return (ROOT / "restore" / "restore_layer.py").read_text(encoding="utf-8")


def imported_names(source):
    """Top-level module names imported by a source file."""
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


# ---------------------------------------------------------------------------
# The fence
# ---------------------------------------------------------------------------


def test_no_pipeline_module_imports_the_restore_tool():
    """DESIGN.md 11 and 7.8.4. The scheduled pipeline is read-only with
    respect to the feature layers, and it stays that way by there being no
    path from any of it to this file. An import is that path."""
    for module in PIPELINE_MODULES:
        names = imported_names((ROOT / module).read_text(encoding="utf-8"))
        assert "restore_layer" not in names, f"{module} imports the restore tool"
        assert "restore" not in names, f"{module} imports from restore/"


def test_no_workflow_can_reach_the_restore_tool():
    """This repository is public and its workflows accept workflow_dispatch.
    A destructive path behind a button anybody with write access can press is
    exactly what DESIGN.md 11.3 refuses, which is also why the drill
    credentials are never GitHub secrets."""
    files = list((ROOT / ".github" / "workflows").glob("*.yml"))
    files.append(ROOT / "jenkins" / "Jenkinsfile")
    assert files, "no workflow files were found, so this test proves nothing"

    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "restore_layer" not in text, f"{path.name} names the restore tool"
        assert "restore/" not in text, f"{path.name} references the restore directory"


def test_it_imports_nothing_from_this_repository():
    """It has to be runnable from a directory holding this file and an
    artifact, by somebody who is not us, on a day when things have gone wrong.
    It also means no future edit can quietly give a pipeline module a reason
    to import it."""
    names = imported_names(restore_source())
    assert names - set(sys.stdlib_module_names) == {"arcgis"}
    for local in ("storage", "status", "backup", "checks", "boto3", "yaml"):
        assert local not in names


def test_it_holds_no_storage_credentials_and_reads_no_configuration():
    """Two properties, one reason. It cannot delete a backup, because it never
    builds a storage client; and it cannot be aimed by a file somebody edited
    last month, because it never reads one. The operator downloads the
    artifact as a separate visible step and passes the path."""
    source = restore_source()
    names = imported_names(source)
    assert "boto3" not in names
    assert "S3_NRS_ENDPOINT" not in source
    assert "S3_GSS_GEODRIVE_KEY_ID" not in source
    assert "yaml" not in names

    # No configuration file is named anywhere in it, checked through the
    # syntax tree for the same reason as the test below: the docstring says
    # 'config.yml' in the course of explaining that it never reads one.
    literals = [
        node.value for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert not [text for text in literals if text.endswith((".yml", ".yaml"))]


def test_it_reads_only_the_restore_credentials():
    """A shell set up to run a backup must not be able to run a restore.

    Asserted through the syntax tree rather than by searching the text,
    because the module docstring names the pipeline's variables in the course
    of explaining that it does not read them - which is worth saying, and
    would defeat a substring check.
    """
    fetched = set()
    for node in ast.walk(ast.parse(restore_source())):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "getenv"
        ):
            for argument in node.args:
                fetched.add(getattr(argument, "id", None) or getattr(argument, "value", None))

    assert fetched == {"USERNAME_VARIABLE", "PASSWORD_VARIABLE"}
    assert restore_layer.USERNAME_VARIABLE == "AGO_USERNAME_RESTORE"
    assert restore_layer.PASSWORD_VARIABLE == "AGO_PASSWORD_RESTORE"


def test_the_production_check_runs_before_anything_else():
    """The order is the interlock. A refusal that depended on the credentials
    being right, or the artifact being present, would be a refusal that could
    be reached around by getting something else wrong first."""
    tree = ast.parse(restore_source())
    main = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    attempt = next(node for node in main.body if isinstance(node, ast.Try))
    first = attempt.body[0]
    assert isinstance(first, ast.Expr)
    assert first.value.func.id == "check_target_allowed"


# ---------------------------------------------------------------------------
# The production items
# ---------------------------------------------------------------------------


def test_both_production_items_are_known():
    """Hard-coded here rather than read from config.yml, so that knowing which
    layers are live is a property of this file and not of what it was handed.
    DESIGN.md 7.8.4."""
    assert set(restore_layer.PRODUCTION_ITEMS) == {LINES_ITEM, POINTS_ITEM}


@pytest.mark.parametrize("item_id", [LINES_ITEM, POINTS_ITEM])
def test_a_production_item_is_refused_by_default(item_id):
    with pytest.raises(ValueError) as refusal:
        restore_layer.check_target_allowed(item_id, production=False, approved_by=None)
    # The refusal has to say how to proceed legitimately, or the next thing
    # that happens is somebody editing the constant out.
    assert "--production" in str(refusal.value)
    assert "--approved-by" in str(refusal.value)


@pytest.mark.parametrize("approver", [None, "", "   "])
def test_production_needs_a_named_approver(approver):
    """A restore discards every edit since the artifact was taken. Who
    accepted that is part of the record, and the terminal window is not a
    record."""
    with pytest.raises(ValueError):
        restore_layer.check_target_allowed(LINES_ITEM, production=True, approved_by=approver)


def test_production_is_allowed_once_it_is_unlocked():
    restore_layer.check_target_allowed(LINES_ITEM, production=True, approved_by="Data owner")


def test_a_test_copy_needs_no_unlock():
    """The drill runs against copies and must not need the production flag for
    them - if it did, the flag would be typed a dozen times a day and would
    stop meaning anything."""
    restore_layer.check_target_allowed(TEST_COPY_ITEM, production=False, approved_by=None)


def test_the_refusal_needs_no_credentials(monkeypatch):
    monkeypatch.delenv(restore_layer.USERNAME_VARIABLE, raising=False)
    monkeypatch.delenv(restore_layer.PASSWORD_VARIABLE, raising=False)
    with pytest.raises(ValueError):
        restore_layer.check_target_allowed(POINTS_ITEM, production=False, approved_by=None)


def test_the_confirmation_phrase_differs_for_a_production_layer():
    """Somebody who has restored a test copy twenty times types its item ID
    without reading it. The one run where that matters asks for something
    else."""
    assert restore_layer.confirmation_phrase(TEST_COPY_ITEM, production=False) == TEST_COPY_ITEM
    assert restore_layer.confirmation_phrase(LINES_ITEM, production=True) == (
        "RESTORE WATER_LICENSED_WORKS_LINES"
    )


# ---------------------------------------------------------------------------
# The artifact, which is verified before anything is destroyed
# ---------------------------------------------------------------------------


def artifact(tmp_path, name="points.gdb.zip", content=b"a file geodatabase, near enough"):
    path = tmp_path / name
    path.write_bytes(content)
    return path, hashlib.sha256(content).hexdigest()


def manifest_for(name, checksum, count=53987):
    return {
        "manifest_version": 1,
        "date_stamp": "2026-08-24",
        "layers": {
            "points": {
                "artifact": name,
                "sha256": checksum,
                "feature_class": "c9ad3cfa_5f69_47f7_a38d_ece32a4f80a4",
                "exported_feature_count": count,
                "exported_utc": "2026-08-24T04:41:00Z",
            },
            "lines": {"artifact": "lines.gdb.zip", "sha256": "0" * 64},
        },
    }


def test_the_manifest_entry_is_found_by_the_file_name():
    """The operator passes a file, not a layer name. Which layer it is comes
    out of the manifest, so there is no second thing to get wrong."""
    layer_key, entry = manifest_entry_for_fixture()
    assert layer_key == "points"
    assert entry["feature_class"].startswith("c9ad3cfa")


def manifest_entry_for_fixture():
    return restore_layer.manifest_entry_for(
        manifest_for("points.gdb.zip", "a" * 64), "points.gdb.zip"
    )


def test_an_artifact_the_manifest_does_not_cover_is_refused():
    with pytest.raises(ValueError) as refusal:
        restore_layer.manifest_entry_for(
            manifest_for("points.gdb.zip", "a" * 64), "yesterdays.gdb.zip"
        )
    # It says what the manifest does cover, because the usual cause is an
    # artifact and a manifest from two different sets.
    assert "points.gdb.zip" in str(refusal.value)


def test_a_matching_artifact_verifies(tmp_path):
    path, checksum = artifact(tmp_path)
    _, entry = restore_layer.manifest_entry_for(
        manifest_for(path.name, checksum), path.name
    )
    restore_layer.verify_artifact(path, entry)


def test_a_truncated_artifact_is_refused(tmp_path):
    """The whole ordering of this tool rests on this test. If a wrong artifact
    can get past here, the layer is emptied for something that cannot fill
    it."""
    path, checksum = artifact(tmp_path)
    path.write_bytes(b"truncated")
    _, entry = restore_layer.manifest_entry_for(
        manifest_for(path.name, checksum), path.name
    )
    with pytest.raises(ValueError) as refusal:
        restore_layer.verify_artifact(path, entry)
    assert "Nothing has been touched" in str(refusal.value)


def test_a_manifest_with_no_checksum_is_refused(tmp_path):
    """An unverifiable artifact is not a safer case than a mismatched one."""
    path, _ = artifact(tmp_path)
    with pytest.raises(ValueError):
        restore_layer.verify_artifact(path, {"artifact": path.name})


# ---------------------------------------------------------------------------
# The duplicated fingerprint, which is load-bearing
# ---------------------------------------------------------------------------


PROPERTIES = {
    "fields": [
        {"name": "OBJECTID", "type": "esriFieldTypeOID"},
        {"name": "GlobalID", "type": "esriFieldTypeGlobalID"},
        {
            "name": "FEATURE_CODE",
            "type": "esriFieldTypeString",
            "domain": {
                "type": "codedValue",
                "name": "LWL_FCODES",
                "codedValues": [{"code": "GA11500000"}, {"code": "EA06100200"}],
            },
        },
        {"name": "TWRK_TAG", "type": "esriFieldTypeString"},
    ],
    "types": [{"id": 1, "name": "Dam"}, {"id": 2, "name": "Ditch"}],
}


def test_the_fingerprint_agrees_with_the_one_that_wrote_the_manifest():
    """restore_layer carries its own copy of status.schema_fingerprint so that
    it imports nothing from this repository. That copy is compared against a
    manifest the original wrote, so the two agreeing is a contract rather than
    a coincidence - and this is the test that holds it. Changing either one
    alone fails here."""
    assert restore_layer.schema_fingerprint(PROPERTIES) == status.schema_fingerprint(PROPERTIES)


def test_an_unchanged_schema_reports_nothing():
    fingerprint = restore_layer.schema_fingerprint(PROPERTIES)
    assert restore_layer.compare_schema(fingerprint, fingerprint) == []


def test_a_missing_field_is_named():
    expected = restore_layer.schema_fingerprint(PROPERTIES)
    reduced = json.loads(json.dumps(expected))
    reduced["fields"] = [f for f in reduced["fields"] if f["name"] != "TWRK_TAG"]
    differences = restore_layer.compare_schema(reduced, expected)
    assert any("TWRK_TAG" in line for line in differences)


def test_a_retyped_field_is_named():
    expected = restore_layer.schema_fingerprint(PROPERTIES)
    retyped = json.loads(json.dumps(expected))
    for field in retyped["fields"]:
        if field["name"] == "TWRK_TAG":
            field["type"] = "esriFieldTypeInteger"
    differences = restore_layer.compare_schema(retyped, expected)
    assert any("TWRK_TAG" in line for line in differences)


def test_changed_domains_and_subtypes_are_reported():
    """Both survive an export and an append or they do not, and which it is
    the restore drill answers. Either way a person has to be told."""
    expected = restore_layer.schema_fingerprint(PROPERTIES)
    stripped = json.loads(json.dumps(expected))
    stripped["domains"] = {}
    stripped["subtypes"] = []
    differences = restore_layer.compare_schema(stripped, expected)
    assert any("domain" in line for line in differences)
    assert any("subtype" in line for line in differences)


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


BASE_ARGUMENTS = [
    "--item-id", TEST_COPY_ITEM,
    "--layer-index", "0",
    "--fgdb", "points.gdb.zip",
]


def test_nothing_happens_without_execute():
    """The default is to describe. A tool of this kind should take an extra
    word to do anything, and the word should be the one nobody types by
    habit."""
    arguments = restore_layer.parse_arguments(BASE_ARGUMENTS)
    assert arguments.execute is False
    assert arguments.production is False
    assert arguments.append_only is False


def test_there_is_no_default_target():
    """DESIGN.md 7.8.4: explicit targets, no defaults. A tool with a default
    target acts on the wrong thing the first time somebody forgets an
    argument."""
    for missing in ("--item-id", "--layer-index", "--fgdb"):
        arguments = [a for a in BASE_ARGUMENTS]
        index = arguments.index(missing)
        del arguments[index:index + 2]
        with pytest.raises(SystemExit):
            restore_layer.parse_arguments(arguments)


def test_globalids_are_preserved_unless_asked_otherwise():
    """DESIGN.md 11.1 makes preserving them the documented path. Whether the
    service actually honours it is what the restore drill records."""
    assert restore_layer.parse_arguments(BASE_ARGUMENTS).no_preserve_globalids is False
