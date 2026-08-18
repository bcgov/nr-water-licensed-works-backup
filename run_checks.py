"""Entry point for the scheduled daily check job.

    python run_checks.py

Reads config.yml, runs one set of integrity checks, prints what happened and
exits 0 or 1.

The exit code is deliberately not a copy of the status. PASS, BASELINE and
WARN all exit 0: a WARN is below the action threshold by definition, and a
workflow that goes red on one trains everybody to ignore a red workflow,
which is the failure DESIGN.md 8.4 spends its length avoiding. DATA_FAIL and
SYSTEM_FAIL exit 1 so the run is visibly failed, and so does a failed status
write whatever the verdict was: a run the notifier will never hear about must
not go green.

Nothing downstream depends on that exit code. Alerting is driven by the
status object written under status/ at the end of every run, whatever the
outcome, which is what makes silence meaningful: the staleness rule in
DESIGN.md 8.2 treats an expected slot with nothing new there as a stopped
pipeline.

Writing it is this script's job rather than run_checks()'. Phase 2 calls
run_checks() from the staging script on the NRIDS server as a gate, and a
gate call should not publish a status object that the notifier would read as
the day's scheduled check.

A run takes roughly six minutes, nearly all of it the 1,020 spatial grid
queries per layer.
"""

import logging
import sys

import yaml

import checks
import status
import storage

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

    # The verdict is logged first, so a storage failure here cannot bury it.
    status_written = True
    try:
        store = storage.connect_to_storage(config)
        status.write_status(store, config, status.CHECKS_JOB, result)
    except Exception as exc:
        # If the run failed because object storage was unreachable then this
        # fails too and no status object appears. That is the designed
        # behaviour rather than a gap - the notifier's staleness rule turns
        # the silence into an alert of its own - but it is not swallowed. A
        # failed write is an operational failure and not a WARN, so the step
        # goes red even on a run that found nothing wrong in the data.
        status_written = False
        logger.error(
            "Could not write the status object, so the notification job will "
            "see nothing for this run: %s", status.safe_reason(exc)
        )

    if result.status in FAILING_STATUSES or not status_written:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
