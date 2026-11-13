"""Short project write locks shared by deletion and work admission."""
from backend.database import db
from backend.models import Project


class ProjectMissing(LookupError):
    pass


def lock_project(project_id, user_id=None):
    # UPDATE is intentional: SQLite ignores SELECT FOR UPDATE. Never hold this
    # lock across file I/O, provider calls or broker operations.
    query = Project.query.filter_by(id=project_id)
    if user_id is not None:
        query = query.filter_by(userId=user_id)
    if query.update({Project.id: Project.id}, synchronize_session=False) != 1:
        raise ProjectMissing('Project not found')
    return Project.query.filter_by(id=project_id).populate_existing().one()
