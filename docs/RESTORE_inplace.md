# Restoring a layer in place

How to put a hosted feature layer back to the state a backup artifact holds, keeping the item ID, service URL, sharing, symbology and subtypes exactly as they are.

**This is a manual procedure. Nothing in the scheduled pipeline can start it, and nothing in the pipeline can change a feature layer at all.** The backup and check jobs export, measure and alert; the decision to restore, and the act of restoring, belong to the data owner.

---

## Before anything

**A restore is not a repair.** It replaces every feature in the layer with the features in the artifact, so every legitimate edit made since that artifact was taken is discarded along with whatever went wrong. Backups are taken Monday, Wednesday and Friday, so that can be several days of real work.

Frequently the right answer is not a restore at all. If the problem is a handful of records, correcting them is faster, and it keeps everything else.

So, in order:

1. **Read the alert and the metrics.** The check job's email says which rule fired and by how much. The metrics file for that day, under `metrics/` in object storage, has the numbers behind it.
2. **Decide whether a restore is the proportionate response**, weighing what would be lost against what is wrong.
3. **Get the data owner's explicit approval**, and note who gave it and when. The tool asks for a name and writes it into the log.
4. **Pick the artifact.** Usually the most recent set whose paired check did not fail. Sets live under `rotating/<date>/`, `monthly/<month>/` and `yearly/<year>/`.

---

## What you need

- Python 3.11 with the packages in `requirements.txt` (the `arcgis` package is the only one this uses).
- An ArcGIS Online account with edit rights on the target layer, its credentials in `AGO_USERNAME_RESTORE` and `AGO_PASSWORD_RESTORE`. The tool reads no other credentials — deliberately, so that a session set up to run the pipeline cannot run a restore.
- The artifact and its manifest, downloaded to the machine you are working on: `<layer>.gdb.zip` and `manifest.json` from the same backup set. **They must be from the same set** — the manifest is what proves the artifact is intact, and one from a different day cannot.

---

## The procedure

### 1. Describe the restore before doing it

```
python restore/restore_layer.py \
    --item-id <the item ID> \
    --layer-index 0 \
    --fgdb path/to/points.gdb.zip
```

This verifies the artifact's checksum against the manifest, reads the layer, and prints what it would do — how many features would be deleted, how many would be restored, when the artifact was taken. **It changes nothing.** Read it and check that the item is the one you mean.

If the checksum does not match, stop and download the artifact again. There is no way to override it, on purpose: an artifact that cannot be verified is not something to empty a layer for.

### 2. Do it

Add `--execute`:

```
python restore/restore_layer.py \
    --item-id <the item ID> \
    --layer-index 0 \
    --fgdb path/to/points.gdb.zip \
    --execute
```

It will ask you to type the item ID back before it does anything. If the target is one of the two live production layers it will refuse outright, and say what to add:

```
    --production --approved-by "<who approved it>"
```

With those, the confirmation becomes the layer's name rather than its item ID — a different phrase, because typing an item ID becomes automatic and this is the run where that matters.

Then it deletes every feature, uploads the artifact as a temporary item, appends it, and deletes the temporary item again.

**It runs long.** Both layers are tens of thousands of features. Leave it alone rather than interrupting it.

### 3. Read what it reports

At the end it compares the layer against the manifest and reports the feature count, the schema and the extent. Everything matching is the expected outcome. Anything it lists as a difference needs a look before you tell anyone the restore is finished.

### 4. Confirm the dependent systems

Outside what this tool can check, and worth a few minutes:

- Open the layer in the web map or application that uses it and confirm it draws and edits normally.
- Confirm the next scheduled downstream replication runs cleanly.

The restore keeps the item ID, service URL, sharing, symbology and capabilities untouched, so there is no mechanism by which a dependent application breaks — but confirming is cheap and the alternative is finding out later.

---

## If it fails part way

**The dangerous moment is between the delete and the append**, and the tool is deliberately loud about it: if the append fails, the layer is empty and it says so, along with where the artifact is.

To retry the append without deleting again:

```
python restore/restore_layer.py … --execute --append-only
```

Do not re-run the plain command in that state — there is nothing left to delete, and the delete step would only add time.

If the delete itself fails or times out, the tool falls back to deleting in chunks automatically. Should it stop part way through, the layer is partly emptied; re-running the plain command from the beginning is safe, because the delete matches everything that is left.

---

## What this does not cover

- **Rebuilding the hosted service itself**, if the item is lost rather than the data. That is a different and much rarer procedure; `servicedef.json` in every backup set holds the service definition it would be built from.
- **Restoring downstream copies.** The data warehouse copy is refreshed by its own scheduled replication, not by this.
- **Deciding what went wrong.** A restore removes the symptom. If the cause was a process rather than an accident, it will happen again.

---

## Notes

The layers have sync enabled, which means `truncate` is unavailable and the delete is a bulk `delete_features` call instead. That is why the delete step is the slow and least predictable part, and why the chunked fallback exists.

The tool asks the service to preserve the GlobalIDs from the artifact. Nothing in this project depends on them.

**Timings** are recorded in the drill log once the restore drill has been run at full scale, and belong here when they are.
