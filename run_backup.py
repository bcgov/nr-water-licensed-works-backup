"""Entry point for the scheduled backup job.

    python run_backup.py

Reads config.yml, runs one backup, prints what happened and exits 0 on
success or 1 on failure so that a workflow step fails visibly rather than
going green on a failed run.

The run is long: two exports, measured at 22.6 and 54.5 minutes, so an hour
and a half of mostly waiting is a normal run rather than a hung one.

Whatever the outcome, the run ends by writing a status object under status/
for the Jenkins notifier to find. That is what makes silence meaningful: the
staleness rule in DESIGN.md 8.2 treats an expected slot with nothing new
under status/ as a stopped pipeline.
"""

import logging
import sys

import yaml

import backup
import status
import storage

CONFIG_PATH = "config.yml"


def load_config(path):
    """Read config.yml. Everything the pipeline is told lives in there."""
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def main():
    config = load_config(CONFIG_PATH)
    logging.basicConfig(
        level=config.get("logging", {}).get("level", "INFO"),
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    # The arcgis package narrates its own portal calls at INFO, which buries
    # the account of the run.
    logging.getLogger("arcgis").setLevel(logging.WARNING)

    result = backup.run_backup(config)

    logger = logging.getLogger("run_backup")
    logger.info("%s: %s", result.status, result.summary)
    for line in result.details:
        logger.info("  %s", line)

    # The result is logged first, so a storage failure here cannot bury the
    # verdict it was reporting.
    status_written = True
    try:
        store = storage.connect_to_storage(config)
        status.write_status(store, config, status.BACKUP_JOB, result)
    except Exception as exc:
        # If the run failed because object storage was unreachable then this
        # fails too and no status object appears. That is the designed
        # behaviour rather than a gap - the notifier's staleness rule turns
        # the silence into an alert of its own - but it is not swallowed: the
        # step goes red so that a run nobody will hear about is visibly
        # failed rather than green.
        status_written = False
        logger.error(
            "Could not write the status object, so the notification job will "
            "see nothing for this run: %s", status.safe_reason(exc)
        )

    return 0 if result.status == "PASS" and status_written else 1


if __name__ == "__main__":
    sys.exit(main())
