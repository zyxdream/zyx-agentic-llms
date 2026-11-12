"""Start the document worker on its configured queue, separate from chat jobs."""
import sys


def worker_main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if any(arg in {'-Q', '--queues', '--queue'} or arg.startswith(('--queues=', '--queue=', '-Q')) for arg in args):
        raise ValueError('Document worker queue is managed by application configuration.')
    from backend.make_celery import celery_app
    from backend.util.document_preparation import queue_name
    with celery_app.flask_app.app_context():
        queue = queue_name()
    celery_app.worker_main(['worker', '--queues', queue, '--concurrency=4', '--prefetch-multiplier=1', *args])


if __name__ == '__main__':
    worker_main()
