from flask import Flask
import pytest

from backend.database import db
from backend.models import Drafts, Project, User


@pytest.fixture
def e2e_database(tmp_path):
    """Provide an isolated Flask-SQLAlchemy database for replayed workflows."""
    app = Flask("lexintel-e2e")
    app.config.update(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'e2e.sqlite'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(app)

    with app.app_context():
        db.create_all()
        user = User(
            email="e2e-user@example.test",
            cognito_username="e2e-user",
            contact_info="E2E Counsel\n1 Test Street\nTest City, CA 90000\n555-0100",
            is_disabled=False,
        )
        db.session.add(user)
        db.session.flush()

        project = Project(name="E2E Project", userId=user.id)
        db.session.add(project)
        db.session.flush()

        draft = Drafts(
            display_name="E2E Complaint",
            docCategory=1,
            projectId=project.id,
            location=None,
        )
        db.session.add(draft)
        db.session.commit()

        yield app, draft

        db.session.remove()
        db.drop_all()
