"""Small Celery task used to verify the isolated live-E2E worker."""

from celery import shared_task


@shared_task(name="tests.e2e.celery_probe")
def e2e_celery_probe() -> dict[str, bool]:
    """Return a value only after the dedicated E2E worker has consumed the task."""

    return {"ok": True}
