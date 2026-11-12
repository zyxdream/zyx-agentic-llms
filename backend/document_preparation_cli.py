"""Explicit operational commands; no schema changes or backfill at app startup."""
import click
from sqlalchemy import and_, or_

from backend.database import db
from backend.models import Drafts, UploadedFiles
from backend.util.document_backfill import register_existing_document
from backend.util.document_preparation import PreparationError, reconcile


def backfill_documents(project_id=None):
    query = db.session.query(UploadedFiles.id).outerjoin(Drafts, UploadedFiles.draft_id == Drafts.id).filter(
        or_(UploadedFiles.source != 'search', UploadedFiles.source.is_(None)), or_(
        and_(UploadedFiles.draft_id.is_(None), UploadedFiles.project_id.isnot(None)), Drafts.docCategory == 69))
    if project_id:
        query = query.filter(or_(UploadedFiles.project_id == project_id, Drafts.projectId == project_id))
    file_ids = [row[0] for row in query.all()]
    created = []
    for file_id in file_ids:
        try:
            doc, changed = register_existing_document(file_id)
        except PreparationError as exc:
            db.session.rollback()
            if exc.status == 404:
                # A user can delete a file after the initial ID scan.
                continue
            raise
        if changed:
            created.append(doc.id)
    return created


def register(app):
    @app.cli.command('case-assistant-cleanup-source-ranges')
    @click.option('--apply', is_flag=True, help='Delete verified duplicate records; otherwise only report.')
    @click.option('--batch-size', type=click.IntRange(1, 1000), default=100, show_default=True)
    def cleanup_source_ranges(apply, batch_size):
        """Remove duplicate range-only results after deploying compatible readers."""
        from backend.util.source_range_cleanup import cleanup_source_ranges as cleanup
        result = cleanup(apply=apply, batch_size=batch_size)
        verb = 'Deleted' if apply else 'Dry-run: would delete'
        click.echo(f'{verb} {result["eligible_records"]} range-only records; '
                   f'{"cleared" if apply else "would clear"} {result["eligible_references"]} references. '
                   f'Skipped: {result["skipped"]}')

    @app.cli.command('prepare-documents-backfill')
    @click.option('--project-id', type=int)
    def backfill(project_id):
        """Queue preparation for existing Case Assistant/Case Background documents."""
        click.echo(f'Admitted {len(backfill_documents(project_id))} existing documents for preparation.')

    @app.cli.command('prepare-documents-reconcile')
    def repair():
        """Recover pending dispatch, stale workers and expired transfers/sources."""
        click.echo(f'Dispatched {reconcile()} pending preparations.')
