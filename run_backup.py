"""Entry point for the scheduled backup job.

    python run_backup.py

Reads config.yml, runs one backup, prints what happened and exits 0 on
success or 1 on failure so that a workflow step fails visibly rather than
going green on a failed run.

The run is long: two exports, measured at 22.6 and 54.5 minutes, so an hour
and a half of mostly waiting is a normal run rather than a hung one.

Writing the status object that the Jenkins notifier reads belongs to
status.py and is not wired up yet.
"""

import logging
import sys

import yaml

import backup

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

    return 0 if result.status == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
