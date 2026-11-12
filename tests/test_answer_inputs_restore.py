from backend import views


def test_answer_resume_step_uses_outline_when_ads_are_empty():
    mem = {
        "analysis/outline": '[["FACTUAL ALLEGATIONS", null], ["Paragraph 1", 1]]',
    }
    outline = [["FACTUAL ALLEGATIONS", None], ["Paragraph 1", 1]]

    resume_step = views._answer_resume_step(
        mem,
        ads_suggestions={},
        outline=outline,
        general_denial=None,
    )

    assert resume_step == 1


def test_answer_resume_step_keeps_generated_sections_precedence():
    mem = {
        "drafted_sections/responses": "Responses draft.",
    }

    resume_step = views._answer_resume_step(
        mem,
        ads_suggestions={},
        outline=[["FACTUAL ALLEGATIONS", None]],
        general_denial=None,
    )

    assert resume_step == 2


def test_answer_resume_step_uses_general_denial_when_ads_are_empty():
    resume_step = views._answer_resume_step(
        {},
        ads_suggestions={},
        outline=[],
        general_denial="Defendant generally denies the complaint.",
    )

    assert resume_step == 1
