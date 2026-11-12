import json

from backend import answer_drafter
from backend.ai.answer_schemas import AnswerDefenseSuggestions, AnswerDraftedDefenses


class Memory(dict):
    def get(self, key, default=None):
        return super().get(key, default)


def test_suggest_ad_template_formats_typed_json_example_with_quoted_coa_title():
    prompt = answer_drafter._build_answer_ad_suggestion_prompt(
        ['FIRST CAUSE OF ACTION - "Contract"'],
        _structure(),
        _digest(),
        {"title": 'FIRST CAUSE OF ACTION - "Contract"', "start_line": 0, "end_line": 0},
        "1",
    )

    example = prompt.split("Output all the above requested fields in this JSON format, using exactly these keys:\n", 1)[1]
    example = example.split("\nIf no defenses apply", 1)[0]
    parsed = json.loads(example)
    assert parsed["coa_id"] == "1"
    assert parsed["coa_title"] == 'FIRST CAUSE OF ACTION - "Contract"'
    assert set(parsed) == {"coa_id", "coa_title", "affirmative_defenses"}
    assert set(parsed["affirmative_defenses"][0]) == {
        "name", "description", "supporting_facts", "citations",
    }
    assert [item["name"] for item in parsed["affirmative_defenses"]] == [
        "Failure to State a Claim", "Preemption by Federal Law",
    ]
    assert all(len(item["citations"]) == 1 for item in parsed["affirmative_defenses"])
    AnswerDefenseSuggestions.model_validate(parsed)
    assert "Provide a brief explanation (1–2 sentences)" in prompt
    assert "exactly one authoritative citation" in prompt
    assert "Keep every description under 2 sentences and include only **one** citation" in prompt
    assert "return an empty list rather than fabricate one" in prompt
    assert "draft several sentences" not in prompt


def test_draft_ad_output_example_is_valid_typed_json():
    with open("backend/ai/prompts/answer/DRAFT_AD") as prompt_file:
        prompt = prompt_file.read().format(ads="Selected defenses", total_num_ads_str="Two defenses")
    example = prompt.split("7. Output using the following JSON format:\n", 1)[1]
    parsed = AnswerDraftedDefenses.model_validate(json.loads(example))
    assert [defense.title for defense in parsed.defenses] == [
        "Failure to State a Claim", ".....",
    ]


def _structure():
    return {
        "plaintiffs": ["Acme Corp."],
        "defendants": ["Beta LLC"],
        "parties": [
            {"name": "Acme Corp.", "role": "plaintiff"},
            {"name": "Beta LLC", "role": "defendant"},
        ],
        "court": "Superior Court of California",
        "venue": "Los Angeles County",
        "jurisdiction_type": "state",
        "state": "california",
        "case_number": "24STCV12345",
        "pleading_title": "Complaint",
    }


def _digest():
    return {
        "summary": "Plaintiff alleges a contract, delayed performance, and late suit filing.",
        "chunk_summaries": [
            {
                "chunk_id": 1,
                "primary_line_start": 1,
                "primary_line_end": 3,
                "summary": "The dispute concerns a written agreement and alleged nonpayment.",
            }
        ],
        "key_allegations": [
            {
                "text": "The parties entered a written agreement in January 2020.",
                "paragraph_numbers": [12],
                "line_numbers": [2],
                "tags": ["contract", "date"],
            }
        ],
        "defense_relevant_facts": [
            {
                "text": "Plaintiff alleges discovery in March 2020 but filed in August 2025.",
                "possible_relevance": ["statute_of_limitations"],
                "paragraph_numbers": [40],
                "line_numbers": [4],
            }
        ],
        "source_ranges": [{"start_line": 1, "end_line": 4, "sources": ["FACTUAL ALLEGATIONS"]}],
    }


def test_answer_ad_suggestions_one_coa_uses_new_inputs_and_citation_filtering(monkeypatch):
    lines = [
        "Caption",
        "FACTUAL ALLEGATIONS",
        "12. The parties entered a written agreement in January 2020.",
        "FIRST CAUSE OF ACTION - Breach of Contract",
        "Beta allegedly failed to pay under the contract.",
    ]
    coa = {
        "title": "FIRST CAUSE OF ACTION - Breach of Contract",
        "type": "cause_of_action",
        "claim_name": "Breach of Contract",
        "claim_number": 1,
        "against": ["Beta LLC"],
        "by": ["Acme Corp."],
        "start_line": 3,
        "end_line": 4,
    }
    prompts = []
    citation_calls = []

    def fake_chat_completion_parse(prompt, response_model, **kwargs):
        assert response_model is AnswerDefenseSuggestions
        assert kwargs == {"model": "gpt-6.1-sol", "reasoning_effort": "high"}
        prompts.append(prompt)
        return AnswerDefenseSuggestions.model_validate({
            "coa_id": "1",
            "coa_title": "FIRST CAUSE OF ACTION - Breach of Contract",
            "affirmative_defenses": [
                {
                    "name": "Limitations",
                    "description": "Limitations may apply because the digest references discovery in March 2020 and suit in August 2025.",
                    "supporting_facts": ["Compl. ¶40", "line 4"],
                    "citations": [
                        {
                            "case_name": "",
                            "citation": "Cal. Civ. Proc. Code § 337",
                            "parenthetical": "(four-year limitations period for written contract claims)",
                        }
                    ],
                }
            ],
        })

    def fake_filter_raw_citations(citation):
        citation_calls.append(citation)
        return [
            {
                "prefix": "/CaseViewer",
                "citationName": "Cal-Civ-Proc-Code-337",
                "id": 7,
                "clusterId": 7,
                "displayName": citation,
                "start_index": 0,
                "end_index": len(citation) - 1,
                "type": "state",
            }
        ]

    monkeypatch.setattr(answer_drafter, "chat_completion_parse", fake_chat_completion_parse)
    monkeypatch.setattr(answer_drafter, "num_tokens_from_string", lambda prompt: len(prompt))
    monkeypatch.setattr(answer_drafter, "filter_raw_citations", fake_filter_raw_citations)
    monkeypatch.setattr(answer_drafter, "filter_on_casename", lambda raw_cit, citation_info: citation_info)
    monkeypatch.setattr(
        answer_drafter,
        "insert_tags",
        lambda text, cite_d, offset: (
            f'<a href="/CaseViewer/mock" target="_blank" rel="noopener noreferrer">{text}</a>',
            72,
        ),
    )

    result = answer_drafter.get_answer_ad_suggestions_from_llm(
        lines,
        _structure(),
        _digest(),
        [coa],
    )

    assert len(prompts) == 1
    assert "Acme Corp." in prompts[0]
    assert "Plaintiff alleges a contract, delayed performance, and late suit filing." in prompts[0]
    assert "line 3: FIRST CAUSE OF ACTION - Breach of Contract" in prompts[0]
    assert prompts[0].startswith("We are writing an answer to a litigation complaint")
    assert "Be overinclusive and suggest all plausible affirmative defenses" in prompts[0]
    assert "Failure to State a Claim" in prompts[0]
    assert '"coa_id": "1", "coa_title": "FIRST CAUSE OF ACTION - Breach of Contract"' in prompts[0]
    assert '"affirmative_defenses"' in prompts[0]
    assert '"Affirmative Defenses"' not in prompts[0]
    assert "fewer, stronger, fact-tied defenses" not in prompts[0]
    assert citation_calls == ["Cal. Civ. Proc. Code § 337"]
    entry = result["Statute of Limitations"]["1"]
    assert entry["cause_of_action"] == "FIRST CAUSE OF ACTION - Breach of Contract"
    assert "March 2020" in entry["description"]
    assert entry["supporting_facts"] == ["Compl. ¶40", "line 4"]
    assert entry["citations"] == [
        {
            "case_name": "",
            "citation": "Cal. Civ. Proc. Code § 337",
            "parenthetical": "(four-year limitations period for written contract claims)",
        }
    ]
    assert entry["filtered_citation_info"]
    assert entry["filtered_citation"] == [
        "Cal. Civ. Proc. Code § 337 (four-year limitations period for written contract claims)"
    ]
    assert entry["filtered_citation_html"] == [
        '<a href="/CaseViewer/mock" target="_blank" rel="noopener noreferrer">Cal. Civ. Proc. Code § 337</a> (four-year limitations period for written contract claims)'
    ]


def test_answer_ad_suggestions_alias_merge_across_coas_and_keep_coa_descriptions(monkeypatch):
    lines = [
        "FIRST CAUSE OF ACTION",
        "First claim text.",
        "SECOND CAUSE OF ACTION",
        "Second claim text.",
    ]
    coas = [
        {
            "title": "FIRST CAUSE OF ACTION",
            "claim_number": 1,
            "claim_name": "Contract",
            "start_line": 0,
            "end_line": 1,
        },
        {
            "title": "SECOND CAUSE OF ACTION",
            "claim_number": 2,
            "claim_name": "Fraud",
            "start_line": 2,
            "end_line": 3,
        },
    ]

    def fake_chat_completion_parse(prompt, response_model, **kwargs):
        assert response_model is AnswerDefenseSuggestions
        if "SECOND CAUSE OF ACTION" in prompt:
            return AnswerDefenseSuggestions.model_validate({
                "coa_id": "2",
                "coa_title": "SECOND CAUSE OF ACTION",
                "affirmative_defenses": [
                    {
                        "name": "Statute of Limitations",
                        "description": "Limitations may apply to the fraud claim based on delayed discovery allegations.",
                        "supporting_facts": [],
                        "citations": [],
                    }
                ],
            })
        return AnswerDefenseSuggestions.model_validate({
            "coa_id": "1",
            "coa_title": "FIRST CAUSE OF ACTION",
            "affirmative_defenses": [
                {
                    "name": "Limitations",
                    "description": "Limitations may apply to the contract claim based on the alleged agreement date.",
                    "supporting_facts": [],
                    "citations": [],
                }
            ],
        })

    monkeypatch.setattr(answer_drafter, "ANSWER_AD_SUGGEST_WORKERS", 1)
    monkeypatch.setattr(answer_drafter, "chat_completion_parse", fake_chat_completion_parse)
    monkeypatch.setattr(answer_drafter, "num_tokens_from_string", lambda prompt: len(prompt))

    result = answer_drafter.get_answer_ad_suggestions_from_llm(
        lines,
        _structure(),
        _digest(),
        coas,
    )

    assert list(result.keys()) == ["Statute of Limitations"]
    assert set(result["Statute of Limitations"].keys()) == {"1", "2"}
    assert "contract claim" in result["Statute of Limitations"]["1"]["description"]
    assert "fraud claim" in result["Statute of Limitations"]["2"]["description"]


def test_answer_ad_consolidation_drops_near_duplicate_description_within_same_coa():
    result = answer_drafter._consolidate_answer_ad_suggestions([
        {
            "coa_id": "1",
            "coa": {"title": "FIRST CAUSE OF ACTION"},
            "items": [
                {
                    "name": "Failure to Mitigate Damages",
                    "description": "Plaintiff failed to mitigate damages after notice.",
                    "supporting_facts": [],
                    "citations": [],
                },
                {
                    "name": "Failure to Mitigate Damages",
                    "description": "Plaintiff failed to mitigate damages after notice.",
                    "supporting_facts": ["line 10"],
                    "citations": [],
                },
            ],
        }
    ])

    entry = result["Failure to Mitigate Damages"]["1"]
    assert entry["description"] == "Plaintiff failed to mitigate damages after notice."
    assert entry["supporting_facts"] == ["line 10"]


def test_answer_ad_suggestions_no_coas_returns_empty_without_llm(monkeypatch):
    monkeypatch.setattr(
        answer_drafter,
        "chat_completion_parse",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("AD LLM should not be called without COAs")),
    )
    monkeypatch.setattr(
        answer_drafter,
        "filter_raw_citations",
        lambda citation: (_ for _ in ()).throw(AssertionError("citation lookup should not run without suggestions")),
    )

    assert answer_drafter.get_answer_ad_suggestions_from_llm([], _structure(), _digest(), []) == {}


def test_ensure_answer_factual_digest_and_ads_saves_empty_when_no_coas(monkeypatch):
    lines = ["FACTUAL ALLEGATIONS", "Plaintiff alleges facts."]
    factual_digest = _digest()
    memory = Memory({
        "complaint_text": json.dumps(lines),
        "complaint_structure": json.dumps({**_structure(), "first_body_line_number": 0}),
        "sections": json.dumps([
            {
                "title": "FACTUAL ALLEGATIONS",
                "type": "factual_allegations",
                "start_line": 0,
                "end_line": 1,
            }
        ]),
        "causes_of_action": json.dumps([]),
        "paragraphs": json.dumps({}),
    })

    monkeypatch.setattr(
        answer_drafter,
        "get_answer_factual_digest_from_llm",
        lambda text_by_lines, sections, para_2_ln=None, first_body_line_number=0: factual_digest
        if text_by_lines == lines and para_2_ln == {} and first_body_line_number == 0
        else (_ for _ in ()).throw(AssertionError("digest should use saved Answer memory")),
    )
    monkeypatch.setattr(
        answer_drafter,
        "chat_completion_parse",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("AD LLM should not be called without COAs")),
    )

    result = answer_drafter.ensure_answer_factual_digest_and_ads(memory)

    assert result == {}
    assert memory["summary"] == factual_digest["summary"]
    assert json.loads(memory["complaint_factual_digest"]) == factual_digest
    assert json.loads(memory["ads_suggestions"]) == {}


def test_answer_ad_citation_lookup_failure_preserves_suggestion(monkeypatch):
    lines = [
        "FIRST CAUSE OF ACTION - Breach of Contract",
        "Beta allegedly failed to pay under the contract.",
    ]
    coa = {
        "title": "FIRST CAUSE OF ACTION - Breach of Contract",
        "claim_number": 1,
        "start_line": 0,
        "end_line": 1,
    }

    monkeypatch.setattr(
        answer_drafter,
        "chat_completion_parse",
        lambda prompt, response_model, **kwargs: AnswerDefenseSuggestions.model_validate({
            "coa_id": "1",
            "coa_title": "FIRST CAUSE OF ACTION - Breach of Contract",
            "affirmative_defenses": [
                {
                    "name": "Failure to State Claim",
                    "description": "The allegations may not plead all required contract elements.",
                    "supporting_facts": ["line 1"],
                    "citations": [{"case_name": "Mock Case", "citation": "1 Cal. 5th 1", "parenthetical": "(mock)"}],
                }
            ],
        }),
    )
    monkeypatch.setattr(answer_drafter, "num_tokens_from_string", lambda prompt: len(prompt))
    monkeypatch.setattr(
        answer_drafter,
        "filter_raw_citations",
        lambda citation: (_ for _ in ()).throw(RuntimeError("lookup unavailable")),
    )

    result = answer_drafter.get_answer_ad_suggestions_from_llm(
        lines,
        _structure(),
        _digest(),
        [coa],
    )

    entry = result["Failure to State a Claim"]["1"]
    assert entry["citations"] == [{"case_name": "Mock Case", "citation": "1 Cal. 5th 1", "parenthetical": "(mock)"}]
    assert entry["filtered_citation_info"] == []
    assert entry["filtered_citation"] == []
    assert entry["filtered_citation_html"] == []


def test_answer_california_state_general_denial_skips_paragraphs_but_saves_ad_suggestions(monkeypatch):
    lines = [
        "Caption",
        "FACTUAL ALLEGATIONS",
        "Plaintiff alleges facts.",
        "FIRST CAUSE OF ACTION",
        "Plaintiff alleges a state claim.",
    ]
    memory = Memory({
        "complaint_cover_text": "Cover text",
        "complaint_text": json.dumps(lines),
        "line_metadata": json.dumps([{"line_number": index} for index, _line in enumerate(lines)]),
        "inputs/uploaded_filename": "complaint.pdf",
    })
    section = {
        "start_line": 1,
        "end_line": 2,
        "title": "FACTUAL ALLEGATIONS",
        "type": "factual_allegations",
    }
    coa = {
        "start_line": 3,
        "end_line": 4,
        "title": "FIRST CAUSE OF ACTION",
        "type": "cause_of_action",
        "claim_number": 1,
        "claim_name": "State Claim",
        "against": ["Beta LLC"],
        "by": ["Acme Corp."],
        "summary": "",
    }
    factual_digest = _digest()
    ads_suggestions = {
        "Failure to State a Claim": {
            "1": {
                "cause_of_action": "FIRST CAUSE OF ACTION",
                "description": "Mock state AD suggestion.",
                "citations": [],
                "filtered_citation_info": [],
                "filtered_citation": [],
                "filtered_citation_html": [],
            }
        }
    }

    monkeypatch.setattr(answer_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(answer_drafter, "_answer_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        answer_drafter,
        "get_answer_complaint_structure_from_llm",
        lambda text_by_lines, line_metadata: {
            "plaintiffs": ["Acme Corp."],
            "defendants": ["Beta LLC"],
            "venue": "Los Angeles County",
            "court": "Superior Court of California",
            "jurisdiction_type": "state",
            "state": "california",
            "case_number": "24STCV12345",
            "first_body_line_number": 1,
        },
    )
    monkeypatch.setattr(
        answer_drafter,
        "get_answer_sections_and_coas_from_llm",
        lambda text_by_lines, line_metadata, first_body_line_number: {
            "sections": [section, coa],
            "causes_of_action": [coa],
        },
    )
    monkeypatch.setattr(answer_drafter, "chat_completion", lambda prompt, **kwargs: "General denial text.")
    monkeypatch.setattr(
        answer_drafter,
        "get_paralines_from_llm",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("state general-denial path should skip paragraph extraction")),
    )
    monkeypatch.setattr(
        answer_drafter,
        "get_answer_factual_digest_from_llm",
        lambda text_by_lines, sections, para_2_ln=None, first_body_line_number=0: factual_digest
        if para_2_ln == {} and first_body_line_number == 1
        else (_ for _ in ()).throw(AssertionError("state digest should run without paragraph anchors")),
    )
    monkeypatch.setattr(
        answer_drafter,
        "get_answer_ad_suggestions_from_llm",
        lambda text_by_lines, complaint_structure, digest, causes_of_action: ads_suggestions
        if digest == factual_digest and causes_of_action == [coa]
        else (_ for _ in ()).throw(AssertionError("state AD suggestions should use digest and COAs")),
    )

    result = answer_drafter.generate_answer_analysis_from_memory(memory, "draft-public-id")

    assert result["outline"] is None
    assert result["general_denial"] == "General denial text."
    assert result["ads_suggestions"] == ads_suggestions
    assert "paragraphs" not in memory
    assert json.loads(memory["ads_suggestions"]) == ads_suggestions
    assert json.loads(memory["complaint_factual_digest"]) == factual_digest


def test_answer_new_york_state_uses_paragraph_extraction_and_defers_ad_suggestions(monkeypatch):
    lines = [
        "Caption",
        "FACTUAL ALLEGATIONS",
        "1. Plaintiff alleges facts.",
        "FIRST CAUSE OF ACTION",
        "2. Plaintiff alleges a state claim.",
    ]
    memory = Memory({
        "complaint_cover_text": "Cover text",
        "complaint_text": json.dumps(lines),
        "line_metadata": json.dumps([{"line_number": index} for index, _line in enumerate(lines)]),
        "inputs/uploaded_filename": "complaint.pdf",
    })
    section = {
        "start_line": 1,
        "end_line": 2,
        "title": "FACTUAL ALLEGATIONS",
        "type": "factual_allegations",
    }
    coa = {
        "start_line": 3,
        "end_line": 4,
        "title": "FIRST CAUSE OF ACTION",
        "type": "cause_of_action",
        "claim_number": 1,
        "claim_name": "State Claim",
        "against": ["Beta LLC"],
        "by": ["Acme Corp."],
        "summary": "",
    }
    monkeypatch.setattr(answer_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(answer_drafter, "_answer_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        answer_drafter,
        "get_answer_complaint_structure_from_llm",
        lambda text_by_lines, line_metadata: {
            "plaintiffs": ["Acme Corp."],
            "defendants": ["Beta LLC"],
            "venue": "County of New York",
            "court": "Supreme Court of the State of New York",
            "jurisdiction_type": "state",
            "state": "new york",
            "case_number": "24STCV12345",
            "first_body_line_number": 1,
        },
    )
    monkeypatch.setattr(
        answer_drafter,
        "get_answer_sections_and_coas_from_llm",
        lambda text_by_lines, line_metadata, first_body_line_number: {
            "sections": [section, coa],
            "causes_of_action": [coa],
        },
    )
    monkeypatch.setattr(
        answer_drafter,
        "chat_completion",
        lambda prompt: (_ for _ in ()).throw(AssertionError("New York state path should not use general denial")),
    )
    monkeypatch.setattr(
        answer_drafter,
        "get_paralines_from_llm",
        lambda text_by_lines, primary_start_line=0, return_warnings=False: (
            ({1: 2, 2: 4}, answer_drafter._empty_answer_para_warnings())
            if return_warnings
            else {1: 2, 2: 4}
        )
        if text_by_lines == lines and primary_start_line == 1
        else (_ for _ in ()).throw(AssertionError("New York state path should use paragraph extraction")),
    )
    monkeypatch.setattr(
        answer_drafter,
        "build_answer_outline_from_locations",
        lambda sections, para_2_ln, first_body_line_number: [["Paragraph 1", 1], ["Paragraph 2", 2]]
        if sections == [section, coa] and para_2_ln == {1: 2, 2: 4} and first_body_line_number == 1
        else (_ for _ in ()).throw(AssertionError("outline should use extracted paragraphs")),
    )
    monkeypatch.setattr(
        answer_drafter,
        "get_answer_factual_digest_from_llm",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("New York setup should defer factual digest")),
    )
    monkeypatch.setattr(
        answer_drafter,
        "get_answer_ad_suggestions_from_llm",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("New York setup should defer AD suggestions")),
    )

    result = answer_drafter.generate_answer_analysis_from_memory(memory, "draft-public-id")

    assert result["outline"] == [["Paragraph 1", 1], ["Paragraph 2", 2]]
    assert result["general_denial"] is None
    assert result["ads_suggestions"] == {}
    assert json.loads(memory["paragraphs"]) == {"1": 2, "2": 4}
    assert json.loads(memory["outline"]) == [["Paragraph 1", 1], ["Paragraph 2", 2]]
    assert json.loads(memory["ads_suggestions"]) == {}
    assert "complaint_factual_digest" not in memory
