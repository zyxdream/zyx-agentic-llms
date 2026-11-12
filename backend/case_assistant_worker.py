"""Start the legacy version-0 Case Assistant extraction pool.

New version-1 requests use agent_tasks.advance on the default worker. Saved
version-0 requests still use plan_prompt/answer_prompt there and process_section
on this extraction queue (four concurrent task slots). This is separate from
document_preparation_worker, which remains active for both versions.
Celery beat checks
failed/stalled requests on the main queue; only explicit Retry restarts them.
See DOCUMENT_PREPARATION_RUNBOOK.md for the required processes.
"""
import sys


def worker_main(argv=None):
    """Select the configured queue and pass worker arguments to Celery's CLI."""
    args = list(sys.argv[1:] if argv is None else argv)
    if '--recovery' in args:
        raise ValueError('The recovery worker was removed. Run the main worker and Beat for failure checks; use Retry to restart a failed prompt.')
    if any(arg in {'-Q', '--queues', '--queue'} or arg.startswith(('--queues=', '--queue=', '-Q')) for arg in args):
        raise ValueError('Extraction worker queue is managed by application configuration.')
    from backend.make_celery import celery_app
    from backend.case_assistant_tasks import queue_name
    with celery_app.flask_app.app_context():
        queue = queue_name()
    # Prefetch one limits reservations ahead of available slots so long-running
    # sections do not build up behind one busy worker. Concurrency is per worker
    # instance; starting more instances increases total model-call concurrency.
    celery_app.worker_main(['worker', '--queues', queue, '--concurrency=4', '--prefetch-multiplier=1', *args])


if __name__ == '__main__':
    worker_main()
