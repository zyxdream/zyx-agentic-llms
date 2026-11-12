"""Application event logging; usage accounting lives in task_analytics."""
import logging
logger = logging.getLogger(__name__)
def analytics_record(*args, **kwargs):
    logger.debug('Workspace event: %s %s', args, kwargs)
