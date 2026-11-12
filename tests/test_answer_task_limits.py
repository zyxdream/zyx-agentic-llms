class Memory(dict):
    def get(self, key, default=None):
        return super().get(key, default)


def _task_object(task):
    return task._get_current_object() if hasattr(task, "_get_current_object") else task


def test_answer_drafter_tasks_use_configured_time_limits():
    from backend import answer_drafter

    tasks = [
        answer_drafter.answer_parse_complaint_task,
        answer_drafter.state0,
        answer_drafter.answer_continue_task,
        answer_drafter.state1,
        answer_drafter.state2,
        answer_drafter.state3,
        answer_drafter.state4,
    ]

    for task in tasks:
        task_obj = _task_object(task)
        assert task_obj.time_limit == answer_drafter.ANSWER_TASK_TIME_LIMIT_SECONDS
        assert task_obj.soft_time_limit == answer_drafter.ANSWER_TASK_SOFT_TIME_LIMIT_SECONDS


def test_answer_parse_task_returns_clean_error_on_soft_timeout(monkeypatch):
    from backend import answer_drafter
    from backend.util import account_access

    # This timeout test uses dictionary memory rather than database ownership.
    monkeypatch.setattr(account_access, 'chat_account_id', lambda chat_id: 1)
    monkeypatch.setattr(account_access, 'require_enabled_account', lambda user_id: None)

    memory = Memory({
        answer_drafter.ANSWER_ACTIVE_PARSE_TASK_KEY: "parse-task",
    })

    monkeypatch.setattr(answer_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(answer_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(answer_drafter, "_answer_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        answer_drafter,
        "get_complaint_text_with_line_metadata_from_file",
        lambda path: (_ for _ in ()).throw(answer_drafter.SoftTimeLimitExceeded()),
    )

    result = answer_drafter.answer_parse_complaint_task.run(
        filename="complaint.pdf",
        draftId="draft-public-id",
    )

    assert result["message"] == "error"
    assert "timed out" in result["error_message"].lower()
    assert answer_drafter.ANSWER_ACTIVE_PARSE_TASK_KEY not in memory
