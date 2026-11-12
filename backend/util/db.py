import json
import shutil

from pathlib import Path
from typing import MutableMapping, Union, Iterator, Optional, Any, Dict
from flask_sqlalchemy import SQLAlchemy
from backend.models import TextEntry, User, Project, ChatThread

BaseMemory = MutableMapping[Union[str, Path], str]

class DBMemory(BaseMemory):
    """
    Chat-scoped auxiliary storage and the assembled compatibility state view.

    ``doc_id`` remains a legacy constructor alias for the chat identity; a
    DraftDocument ID must never be used to select this memory.
    """
    def __init__(self, db: SQLAlchemy, doc_id: int = None, *, chat_thread_id: int = None) -> None:
        if doc_id is not None and chat_thread_id is not None and int(doc_id) != int(chat_thread_id):
            raise ValueError('The chat identifiers do not match.')
        self.chat_thread_id = ChatThread.resolve_id(chat_thread_id if chat_thread_id is not None else doc_id)
        self.db = db

        #
        user = User.query.join(
        Project, User.id == Project.userId  # Join User and Project on User.id and Project.userId
        ).join(
            ChatThread, Project.id == ChatThread.projectId  # Join Project and ChatThread on Project.id and ChatThread.projectId
        ).filter(
            ChatThread.id == self.chat_thread_id
        ).first()
        self.user = user

    @property
    def doc_id(self):
        """Legacy chat-ID alias used by older guided and worker entry points."""
        return self.chat_thread_id


    def __getitem__(self, key: str) -> str:
        """
        Get the content of a file in the database.

        Parameters
        ----------
        key : str
            The name of the file to get the content of.

        Returns
        -------
        str
            The content of the file.

        Raises
        ------
        KeyError
            If the file does not exist in the database.
        """
        if key == 'state':
            from backend.util.workspace_store import load
            return json.dumps(load(self.doc_id), ensure_ascii=False)
        # check if it exists first
        entry_check = TextEntry.query.filter_by(chat_thread_id=self.doc_id, textName=key).first()

        if entry_check is not None:
            return entry_check.content
        else:
            raise KeyError(f"File '{key}' could not be found in the database")

    def get(self, key: str, default: Optional[Any] = None) -> Any:
        """
        Get the content of a file in the database, or a default value if the file does not exist.

        Parameters
        ----------
        key : str
            The name of the file to get the content of.
        default : any, optional
            The default value to return if the file does not exist, by default None.

        Returns
        -------
        any
            The content of the file, or the default value if the file does not exist.
        """
        try:
            return self[key]
        except KeyError:
            return default

    def __setitem__(self, key: str, val: str) -> None:
        """
        Set the content of a file in the database.

        Parameters
        ----------
        key : Union[str, Path]
            The name of the file to set the content of.
        val : str
            The content to set.

        Raises
        ------
        TypeError
            If val is not string.
        """
        if not isinstance(val, str):
            raise TypeError("val must be str")
        if key == 'state':
            raise ValueError('Workspace state is a read projection. Use explicit draft, turn, or auxiliary writes.')
        from backend.util.account_access import check_current_account
        check_current_account()

        entry_check = TextEntry.query.filter_by(chat_thread_id=self.doc_id, textName=key).first()
        if entry_check is None:
            new_entry = TextEntry(chat_thread_id=self.doc_id, textName=key, content=val)
            self.db.session.add(new_entry)
        else:
            entry_check.content=val
        self.db.session.commit()
        snippet = val[:50].replace("\n", "\\n")
        print(f"[DBMemory] saved `{key}` (len={len(val)}) content starts with: {snippet!r}")

    def __delitem__(self, key: str) -> None:
        """
        Delete a file or directory in the database.

        Parameters
        ----------
        key : str
            The name of the file or directory to delete.

        Raises
        ------
        KeyError
            If the file or directory does not exist in the database.
        """
        entry_check = TextEntry.query.filter_by(chat_thread_id=self.doc_id, textName=key).first()
        if entry_check is None:
            raise KeyError(f"File '{key}' could not be found in the database")

        self.db.session.delete(entry_check)
        self.db.session.commit()

    def __len__(self):
        return TextEntry.query.filter_by(chat_thread_id=self.doc_id).count()

    def __iter__(self) -> Iterator[str]:
        return iter(TextEntry.query.filter_by(chat_thread_id=self.doc_id).all())


class DiskMemory(BaseMemory):
    def __init__(self, path: Union[str, Path]):
        """
        Initialize the DB class.

        Parameters
        ----------
        path : Union[str, Path]
            The path to the directory where the database files are stored.
        """
        self.path: Path = Path(path).absolute()

        self.path.mkdir(parents=True, exist_ok=True)

    def __contains__(self, key: str) -> bool:
        """
        Check if a file with the specified name exists in the database.

        Parameters
        ----------
        key : str
            The name of the file to check.

        Returns
        -------
        bool
            True if the file exists, False otherwise.
        """
        return (self.path / key).is_file()

    def __getitem__(self, key: str) -> str:
        """
        Get the content of a file in the database.

        Parameters
        ----------
        key : str
            The name of the file to get the content of.

        Returns
        -------
        str
            The content of the file.

        Raises
        ------
        KeyError
            If the file does not exist in the database.
        """
        full_path = self.path / key

        if not full_path.is_file():
            raise KeyError(f"File '{key}' could not be found in '{self.path}'")
        with full_path.open("r", encoding="utf-8") as f:
            return f.read()

    def get(self, key: str, default: Optional[Any] = None) -> Any:
        """
        Get the content of a file in the database, or a default value if the file does not exist.

        Parameters
        ----------
        key : str
            The name of the file to get the content of.
        default : any, optional
            The default value to return if the file does not exist, by default None.

        Returns
        -------
        any
            The content of the file, or the default value if the file does not exist.
        """
        try:
            return self[key]
        except KeyError:
            return default

    def __setitem__(self, key: Union[str, Path], val: str) -> None:
        """
        Set the content of a file in the database.

        Parameters
        ----------
        key : Union[str, Path]
            The name of the file to set the content of.
        val : str
            The content to set.

        Raises
        ------
        TypeError
            If val is not string.
        """
        if str(key).startswith("../"):
            raise ValueError(f"File name {key} attempted to access parent path.")

        if not isinstance(val, str):
            raise TypeError("val must be str")

        full_path = self.path / key
        full_path.parent.mkdir(parents=True, exist_ok=True)

        full_path.write_text(val, encoding="utf-8")

    def __delitem__(self, key: Union[str, Path]) -> None:
        """
        Delete a file or directory in the database.

        Parameters
        ----------
        key : Union[str, Path]
            The name of the file or directory to delete.

        Raises
        ------
        KeyError
            If the file or directory does not exist in the database.
        """
        item_path = self.path / key
        if not item_path.exists():
            raise KeyError(f"Item '{key}' could not be found in '{self.path}'")

        if item_path.is_file():
            item_path.unlink()
        elif item_path.is_dir():
            shutil.rmtree(item_path)

    def __iter__(self) -> Iterator[str]:
        return iter(
            sorted(
                str(item.relative_to(self.path))
                for item in sorted(self.path.rglob("*"))
                if item.is_file()
            )
        )

    def __len__(self):
        return len(list(self.__iter__()))

    def _all_files(self) -> str:
        file_paths = [str(item) for item in self if Path(item).is_file()]
        return "\n".join(file_paths)

    def to_path_list_string(self, supported_code_files_only: bool = False) -> str:
        """
        Returns directory as a list of file paths. Useful for passing to the LLM where it needs to understand the wider context of files available for reference.
        """
        if supported_code_files_only:
            return self._supported_files()
        else:
            return self._all_files()

    def to_dict(self) -> Dict[Union[str, Path], str]:
        return {file_path: self[file_path] for file_path in self}

    def to_json(self) -> str:
        return json.dumps(self.to_dict())
