import ast
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


class MatterRenameTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / 'backend/views.py'
        tree = ast.parse(path.read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'update_project_name')
        node.decorator_list = []
        self.project = SimpleNamespace(id=12, userId=7, name='Old matter', drafts=['draft'], files=['file'])
        self.query = Mock()
        self.query.filter_by.return_value.first.return_value = self.project
        self.query.filter_by.return_value.filter.return_value.first.return_value = None
        self.session = Mock()
        self.request = Mock()
        self.request.get_json.return_value = {'projId': 12, 'projectName': ' New matter '}
        namespace = {'request': self.request, 'current_user': SimpleNamespace(id=7),
                     'Project': SimpleNamespace(query=self.query, id=0),
                     'db': SimpleNamespace(session=self.session), 'logger': logging.getLogger(__name__)}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
        self.rename = namespace['update_project_name']

    def test_rename_preserves_matter_identity_and_contents(self):
        body, status = self.rename()
        self.assertEqual(status, 200)
        self.assertEqual(body['projectName'], 'New matter')
        self.assertEqual(body['projId'], 12)
        self.query.filter_by.assert_any_call(id=12, userId=7)
        self.query.filter_by.assert_any_call(name='New matter', userId=7)
        self.assertEqual(self.project.drafts, ['draft'])
        self.assertEqual(self.project.files, ['file'])
        self.session.commit.assert_called_once()
        self.session.delete.assert_not_called()

    def test_nonowned_or_missing_matter_cannot_be_renamed(self):
        self.query.filter_by.return_value.first.return_value = None
        _, status = self.rename()
        self.assertEqual(status, 404)
        self.assertEqual(self.project.name, 'Old matter')
        self.session.commit.assert_not_called()

    def test_duplicate_name_is_rejected_without_mutation(self):
        self.query.filter_by.return_value.filter.return_value.first.return_value = SimpleNamespace(id=13)
        _, status = self.rename()
        self.assertEqual(status, 409)
        self.assertEqual(self.project.name, 'Old matter')
        self.session.commit.assert_not_called()

    def test_invalid_body_id_and_name_are_rejected_before_query(self):
        payloads = [None, [], 'bad', {}, {'projId': True, 'projectName': 'Valid'},
                    {'projId': 1.2, 'projectName': 'Valid'}, {'projId': -1, 'projectName': 'Valid'},
                    {'projId': 'bad', 'projectName': 'Valid'}, {'projId': 12, 'projectName': None},
                    {'projId': 12, 'projectName': '  '}, {'projId': 12, 'projectName': 'x' * 251}]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.request.get_json.return_value = payload
                _, status = self.rename()
                self.assertEqual(status, 400)
        self.query.filter_by.assert_not_called()

    def test_database_failure_rolls_back(self):
        self.session.commit.side_effect = RuntimeError('fixture failure')
        with self.assertLogs(__name__, level='ERROR'):
            _, status = self.rename()
        self.assertEqual(status, 500)
        self.session.rollback.assert_called_once()

    def test_unchanged_name_does_not_write(self):
        self.request.get_json.return_value = {'projId': '12', 'projectName': 'Old matter'}
        _, status = self.rename()
        self.assertEqual(status, 200)
        self.session.commit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
