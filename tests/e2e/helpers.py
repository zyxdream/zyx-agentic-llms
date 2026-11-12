"""Reusable helpers for customer-facing end-to-end tests.

The helpers deliberately keep transport concerns separate from workflow
assertions so the same tests can use Flask's test client locally or a deployed
HTTP endpoint in CI.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen


JSON = dict[str, Any]
Transport = Callable[..., "E2EResponse"]


@dataclass(frozen=True)
class E2EResponse:
    """Small response object shared by Flask and HTTP-backed clients."""

    status_code: int
    headers: Mapping[str, str]
    content: bytes
    url: str = ""

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        if not self.content:
            return None
        try:
            return json.loads(self.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Response from {self.url or 'request'} was not JSON") from exc

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise E2ERequestError(
                f"HTTP {self.status_code} returned from {self.url or 'request'}",
                response=self,
            )


class E2ERequestError(RuntimeError):
    """Raised when an E2E request or response contract is not satisfied."""

    def __init__(self, message: str, *, response: E2EResponse | None = None) -> None:
        super().__init__(message)
        self.response = response


@dataclass(frozen=True)
class ProjectRef:
    public_id: int | str
    name: str


@dataclass(frozen=True)
class DraftRef:
    public_id: str
    name: str
    project_id: int | str


class E2EClient:
    """Authenticated client for local Flask or deployed HTTP E2E tests."""

    def __init__(
        self,
        *,
        flask_client: Any | None = None,
        base_url: str | None = None,
        access_token: str | None = None,
        transport: Transport | None = None,
    ) -> None:
        configured_transports = sum(
            value is not None for value in (flask_client, base_url, transport)
        )
        if configured_transports != 1:
            raise ValueError("Provide exactly one of flask_client, base_url, or transport")

        self.flask_client = flask_client
        self.base_url = base_url.rstrip("/") if base_url else None
        self.transport = transport
        self.access_token = access_token or os.getenv("E2E_ACCESS_TOKEN")

    def _headers(
        self,
        headers: Mapping[str, str] | None,
        *,
        authenticated: bool,
        has_json: bool,
    ) -> dict[str, str]:
        request_headers = dict(headers or {})
        if authenticated:
            if not self.access_token:
                raise ValueError(
                    "An access token is required for authenticated E2E requests; "
                    "pass access_token or set E2E_ACCESS_TOKEN"
                )
            request_headers.setdefault("Authorization", f"Bearer {self.access_token}")
        if has_json:
            request_headers.setdefault("Content-Type", "application/json")
        return request_headers

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        data: bytes | str | None = None,
        headers: Mapping[str, str] | None = None,
        authenticated: bool = True,
        expected_status: int | set[int] | None = None,
    ) -> E2EResponse:
        request_headers = self._headers(
            headers,
            authenticated=authenticated,
            has_json=json_body is not None,
        )
        body: bytes | None
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
        elif isinstance(data, str):
            body = data.encode("utf-8")
        else:
            body = data

        method = method.upper()
        if self.flask_client is not None:
            response = self.flask_client.open(
                path,
                method=method,
                headers=request_headers,
                data=body,
            )
            result = E2EResponse(
                status_code=response.status_code,
                headers=dict(response.headers),
                content=response.get_data(),
                url=path,
            )
        elif self.transport is not None:
            result = self.transport(
                method,
                path,
                headers=request_headers,
                body=body,
            )
        else:
            assert self.base_url is not None
            url = urljoin(f"{self.base_url}/", path.lstrip("/"))
            request = Request(url, data=body, headers=request_headers, method=method)
            try:
                with urlopen(request) as response:  # noqa: S310 - URL is test configuration.
                    result = E2EResponse(
                        status_code=response.status,
                        headers=dict(response.headers.items()),
                        content=response.read(),
                        url=url,
                    )
            except HTTPError as exc:
                result = E2EResponse(
                    status_code=exc.code,
                    headers=dict(exc.headers.items()),
                    content=exc.read(),
                    url=url,
                )
            except URLError as exc:
                raise E2ERequestError(f"Could not reach {url}: {exc.reason}") from exc

        if expected_status is not None:
            accepted = {expected_status} if isinstance(expected_status, int) else expected_status
            if result.status_code not in accepted:
                raise E2ERequestError(
                    f"Expected HTTP {sorted(accepted)}, got {result.status_code} from {path}",
                    response=result,
                )
        return result

    def get(self, path: str, **kwargs: Any) -> E2EResponse:
        return self.request("GET", path, **kwargs)

    def post_json(
        self,
        path: str,
        payload: Any,
        **kwargs: Any,
    ) -> E2EResponse:
        return self.request("POST", path, json_body=payload, **kwargs)

    def create_project(self, name: str) -> ProjectRef:
        """Create a project and resolve its public ID from the list endpoint."""

        response = self.post_json("/create-project", {"proj-name": name})
        response.raise_for_status()

        projects_response = self.post_json("/list-projects", {})
        projects_response.raise_for_status()
        payload = projects_response.json()
        projects = payload.get("projects") if isinstance(payload, dict) else None
        if not isinstance(projects, list):
            raise E2ERequestError("list-projects did not return a projects list", response=projects_response)

        matches = [
            project
            for project in projects
            if isinstance(project, list) and project and project[0] == name
        ]
        if len(matches) != 1 or len(matches[0]) < 2:
            raise E2ERequestError(
                f"Could not resolve exactly one project named {name!r} after creation",
                response=projects_response,
            )
        return ProjectRef(public_id=matches[0][1], name=name)

    def create_draft(
        self,
        project_id: int | str,
        name: str,
        draft_type: int | str,
    ) -> DraftRef:
        response = self.post_json(
            "/create-draft",
            {
                "proj-id": project_id,
                "draft-name": name,
                "draft-type": draft_type,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("draftId"):
            raise E2ERequestError("create-draft did not return a draftId", response=response)
        return DraftRef(
            public_id=str(payload["draftId"]),
            name=str(payload.get("draftName") or name),
            project_id=project_id,
        )

    def poll_task(
        self,
        task_id: str,
        *,
        timeout: float = 300.0,
        interval: float = 2.0,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> "TaskResult":
        """Poll the application result endpoint while retaining progress history."""

        deadline = clock() + timeout
        history: list[JSON] = []
        last_payload: JSON | None = None

        while True:
            response = self.get(f"/result/{quote(str(task_id), safe='')}")
            if response.status_code >= 400:
                raise E2ERequestError(
                    f"Task result request failed for {task_id}: HTTP {response.status_code}",
                    response=response,
                )
            payload = response.json()
            if not isinstance(payload, dict):
                raise E2ERequestError(
                    f"Task result for {task_id} was not a JSON object",
                    response=response,
                )

            last_payload = payload
            history.append(
                {
                    "state": payload.get("state"),
                    "ready": payload.get("ready"),
                    "successful": payload.get("successful"),
                    "info": payload.get("info"),
                }
            )
            if payload.get("ready") is True:
                return TaskResult(
                    task_id=str(task_id),
                    ready=True,
                    successful=payload.get("successful"),
                    state=payload.get("state"),
                    value=payload.get("value"),
                    info=payload.get("info"),
                    history=tuple(history),
                    payload=payload,
                )

            now = clock()
            if now >= deadline:
                raise E2ETaskTimeout(
                    str(task_id),
                    last_payload=last_payload,
                    history=tuple(history),
                )
            sleep_fn(min(interval, max(0.0, deadline - now)))


@dataclass(frozen=True)
class TaskFailureDiagnostic:
    task_id: str
    state: Any
    info: Any
    error_message: Any
    error_code: Any
    retryable: Any
    value: Any

    def as_dict(self) -> JSON:
        return {
            "task_id": self.task_id,
            "state": self.state,
            "info": self.info,
            "error_message": self.error_message,
            "error_code": self.error_code,
            "retryable": self.retryable,
            "value": self.value,
        }

    def summary(self) -> str:
        reason = self.error_message or self.info or "task returned an unsuccessful result"
        return f"Task {self.task_id} failed ({self.state}): {reason}"


def build_task_failure_diagnostic(task_id: str, payload: Mapping[str, Any]) -> TaskFailureDiagnostic:
    value = payload.get("value")
    value_dict = value if isinstance(value, dict) else {}
    return TaskFailureDiagnostic(
        task_id=str(task_id),
        state=payload.get("state"),
        info=payload.get("info"),
        error_message=value_dict.get("error_message"),
        error_code=value_dict.get("error_code"),
        retryable=value_dict.get("retryable"),
        value=value,
    )


class E2ETaskFailure(RuntimeError):
    def __init__(self, diagnostic: TaskFailureDiagnostic) -> None:
        super().__init__(diagnostic.summary())
        self.diagnostic = diagnostic


class E2ETaskTimeout(TimeoutError):
    def __init__(
        self,
        task_id: str,
        *,
        last_payload: JSON | None,
        history: tuple[JSON, ...],
    ) -> None:
        super().__init__(f"Task {task_id} did not finish before the E2E timeout")
        self.task_id = task_id
        self.last_payload = last_payload
        self.history = history


@dataclass(frozen=True)
class TaskResult:
    task_id: str
    ready: bool
    successful: Any
    state: Any
    value: Any
    info: Any
    history: tuple[JSON, ...]
    payload: JSON

    @property
    def failure(self) -> TaskFailureDiagnostic | None:
        if self.successful is True:
            return None
        return build_task_failure_diagnostic(self.task_id, self.payload)

    def require_success(self) -> Any:
        if self.successful is not True:
            diagnostic = self.failure or build_task_failure_diagnostic(self.task_id, self.payload)
            raise E2ETaskFailure(diagnostic)
        return self.value


class ArtifactCollector:
    """Collect generated artifacts and diagnostics under one test directory."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        safe_name = Path(name).name
        if not safe_name or safe_name in {".", ".."}:
            raise ValueError("Artifact name must contain a filename")
        return self.root / safe_name

    def collect_response(self, name: str, response: E2EResponse) -> Path:
        response.raise_for_status()
        destination = self._path(name)
        destination.write_bytes(response.content)
        return destination

    def collect_file(self, source: str | Path, name: str | None = None) -> Path:
        source_path = Path(source)
        destination = self._path(name or source_path.name)
        destination.write_bytes(source_path.read_bytes())
        return destination

    def collect_json(self, name: str, payload: Any) -> Path:
        destination = self._path(name)
        destination.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
        return destination

    def collect_task_failure(
        self,
        diagnostic: TaskFailureDiagnostic,
        name: str = "task-failure.json",
    ) -> Path:
        return self.collect_json(name, diagnostic.as_dict())

    def collect_draft_artifacts(
        self,
        client: E2EClient,
        draft_id: str,
        *,
        prefix: str = "draft",
        include_word: bool = True,
        pdf_path: str | None = None,
        word_path: str | None = None,
    ) -> dict[str, Path]:
        encoded_draft_id = quote(str(draft_id), safe="")
        artifacts = {
            "pdf": self.collect_response(
                f"{prefix}.pdf",
                client.get(pdf_path or f"/draft-pdf/{encoded_draft_id}"),
            )
        }
        if include_word:
            artifacts["word"] = self.collect_response(
                f"{prefix}.docx",
                client.get(word_path or f"/download-word-complaint/{encoded_draft_id}"),
            )
        return artifacts
