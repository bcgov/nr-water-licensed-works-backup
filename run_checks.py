"""Entry point for the scheduled daily check job.

    python run_checks.py

Reads config.yml, runs one set of integrity checks, prints what happened and
exits 0 or 1.

The exit code is deliberately not a copy of the status. PASS, BASELINE and
WARN all exit 0: a WARN is below the action threshold by definition, and a
workflow that goes red on one trains everybody to ignore a red workflow,
which is the failure DESIGN.md 8.4 spends its length avoiding. DATA_FAIL and
SYSTEM_FAIL exit 1 so the run is visibly failed.

Nothing downstream depends on that exit code. Alerting is driven by the
status object the Jenkins notifier reads, which belongs to status.py and is
not wired up yet.

A run takes roughly six minutes, nearly all of it the 1,020 spatial grid
queries per layer.
"""

import logging
import sys

import yaml

import checks

CONFIG_PATH = "config.yml"

# DATA_FAIL is a data problem and SYSTEM_FAIL is an operational one. Both
# mean this run did not deliver a clean check, so both fail the step.
FAILING_STATUSES = ("DATA_FAIL", "SYSTEM_FAIL")


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
    # the account of the run under 1,020 grid queries.
    logging.getLogger("arcgis").setLevel(logging.WARNING)

    result = checks.run_checks(config)

    logger = logging.getLogger("run_checks")
    logger.info("%s: %s", result.status, result.summary)
    for line in result.details:
        logger.info("  %s", line)

    return 1 if result.status in FAILING_STATUSES else 0


if __name__ == "__main__":
    sys.exit(main())
