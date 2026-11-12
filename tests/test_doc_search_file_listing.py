"""Doc Search's file picker must show only its searchable corpus."""

from types import SimpleNamespace

from flask import Flask

from backend.database import db
from backend.models import FolderUpload, Project, UploadedFiles, User


def test_doc_search_file_list_excludes_null_source_in_folders_and_loose_files(
    monkeypatch,
):
    from backend import views

    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI="sqlite://",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(app)

    with app.app_context():
        db.create_all()
        user = User(email="doc-search-list@example.test")
        db.session.add(user)
        db.session.flush()
        project = Project(name="Search matter", userId=user.id)
        db.session.add(project)
        db.session.flush()
        mixed_folder = FolderUpload(folder_name="Mixed", project_id=project.id)
        null_only_folder = FolderUpload(folder_name="Legacy only", project_id=project.id)
        db.session.add_all([mixed_folder, null_only_folder])
        db.session.flush()

        def add_file(unique_id, source, folder=None):
            db.session.add(
                UploadedFiles(
                    unique_id=unique_id,
                    original_filename=f"{unique_id}.pdf",
                    file_size=100,
                    content="sample",
                    project_id=project.id,
                    folder_upload_id=folder.id if folder else None,
                    source=source,
                )
            )

        add_file("folder-search", "search", mixed_folder)
        add_file("folder-null", None, mixed_folder)
        add_file("folder-everything", "everything", mixed_folder)
        add_file("empty-folder-null", None, null_only_folder)
        add_file("loose-search", "search")
        add_file("loose-null", None)
        add_file("loose-everything", "everything")
        db.session.commit()

        monkeypatch.setattr(views, "current_user", SimpleNamespace(id=user.id))
        monkeypatch.setattr(views, "project_ownership_check", lambda **_kwargs: None)
        with app.test_request_context(
            "/api/get-uploaded-files-search",
            method="POST",
            json={"projectId": project.id},
        ):
            body, status = views.get_uploaded_files_search.__wrapped__()

        assert status == 200
        assert set(body) == {"folders", "loose_files"}
        assert len(body["folders"]) == 1
        folder = body["folders"][0]
        assert folder["folder_id"] == mixed_folder.id
        assert folder["folder_name"] == "Mixed"
        assert set(folder) == {
            "folder_id", "folder_name", "upload_date", "tags", "files"
        }
        assert [file["unique_id"] for file in folder["files"]] == ["folder-search"]
        assert set(folder["files"][0]) == {
            "unique_id", "filename", "file_size", "upload_date",
            "relative_path", "tags", "metadata",
        }
        assert [file["unique_id"] for file in body["loose_files"]] == ["loose-search"]
        assert set(body["loose_files"][0]) == {
            "unique_id", "filename", "file_size", "upload_date", "tags", "metadata",
        }

        db.session.remove()
        db.drop_all()
