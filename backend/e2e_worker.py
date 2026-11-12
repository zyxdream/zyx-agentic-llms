"""Start a Celery worker that is locked to the isolated E2E queue."""

from __future__ import annotations

import sys

from backend.e2e_config import build_e2e_config


def worker_main(argv: list[str] | None = None) -> None:
    """Start the E2E worker and reject attempts to override its queue."""
    config = build_e2e_config()
    queue = config["E2E_CELERY_QUEUE"]
    extra_args = list(sys.argv[1:] if argv is None else argv)

    if any(
        argument in {"-Q", "--queues", "--queue"}
        or argument.startswith("--queues=")
        or argument.startswith("--queue=")
        for argument in extra_args
    ):
        raise ValueError(
            "E2E worker queue is managed by E2E_CELERY_QUEUE; do not pass a queue override"
        )

    # Import only after validating E2E mode, so a missing opt-in cannot load
    # the normal application Celery app as a side effect.
    from backend.make_celery import celery_app

    celery_app.worker_main(["worker", "--queues", str(queue), *extra_args])


if __name__ == "__main__":
    raise SystemExit(worker_main())
