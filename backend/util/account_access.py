"""Fresh account-status checks shared by HTTP and background authorization."""
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import select

from backend.database import db
from backend.models import ChatThread, Project, User


class AccountUnavailable(ValueError):
    """The request's owner no longer has an enabled local account."""


_active_account = ContextVar('active_account', default=None)


def require_enabled_account(user_id):
    """Read committed account status without changing the caller's transaction.

    Scalar reads bypass cached User objects. Independent server connections also
    avoid a worker's older repeatable-read snapshot. SQLite uses the current
    session because its in-memory pool can share the caller's physical connection.
    """
    statement = select(User.is_disabled).where(User.id == user_id)
    if db.engine.dialect.name == 'sqlite':
        with db.session.no_autoflush:
            row = db.session.execute(statement).first()
    else:
        with db.engine.connect() as connection:
            row = connection.execute(statement).first()
    if row is None or row[0]:
        raise AccountUnavailable('This account cannot use zyx-agentic-llms.')


def chat_account_id(chat_id):
    """Resolve the owner of a chat; document IDs are not chat identities."""
    chat_id = ChatThread.resolve_id(chat_id)
    with db.session.no_autoflush:
        owner_id = db.session.execute(select(Project.userId).join(
            ChatThread, ChatThread.projectId == Project.id).where(ChatThread.id == chat_id)).scalar_one_or_none()
    if owner_id is None:
        raise AccountUnavailable('This account cannot use zyx-agentic-llms.')
    return owner_id


def project_account_id(project_id):
    """Resolve the account that owns a matter or search project."""
    with db.session.no_autoflush:
        owner_id = db.session.execute(
            select(Project.userId).where(Project.id == project_id)).scalar_one_or_none()
    if owner_id is None:
        raise AccountUnavailable('This account cannot use zyx-agentic-llms.')
    return owner_id


@contextmanager
def account_execution(user_id):
    """Carry a legacy worker's owner to its nested provider/write checkpoints."""
    require_enabled_account(user_id)
    token = _active_account.set(user_id)
    try:
        yield
        require_enabled_account(user_id)
    finally:
        _active_account.reset(token)


def check_current_account():
    user_id = _active_account.get()
    if user_id is not None:
        require_enabled_account(user_id)
