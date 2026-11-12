"""Provider routing and output-contract regressions for the three request drafters."""

import concurrent.futures
import importlib
import json
import re
from itertools import count
from types import SimpleNamespace

import pytest

from backend.ai.discovery_request_schemas import Definitions, RFARequests, RFPRequests, ROGRequests
from backend.ai.discovery_intro import load_discovery_intro_prompt
from backend.ai.base import split_into_chunks


PRODUCTS = [
    ("rfp", RFPRequests, "Requests for Production of Documents", "Request", "Justification", "requests"),
    ("rfa", RFARequests, "Requests for Admission", "Admission Request", "Strategic Rationale", "admissions"),
    ("rog", ROGRequests, "INTERROGATORIES", "Interrogatory", "Rationale", "interrogatories"),
]


class Memory(dict):
    user = SimpleNamespace(contact_info="Counsel")


def item_for(index, request_key, rationale_key):
    return {"Topic": "Topic", request_key: f"ITEM{index}", rationale_key: "Reason"}


def prepare(monkeypatch, product, memory):
    module = importlib.import_module(f"backend.{product}_drafter")
    monkeypatch.setattr(module.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(module, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(module, "sleep", lambda seconds: None)
    monkeypatch.setattr(module, "split_into_chunks", lambda lines, chunk_len, overlap_lines: [lines])
    return module


@pytest.mark.parametrize("product,schema,alias,request_key,rationale_key,response_key", PRODUCTS)
@pytest.mark.parametrize("count", [99, 100, 101, 201])
def test_generation_routes_and_consolidation_keeps_every_item(
    monkeypatch, product, schema, alias, request_key, rationale_key, response_key, count,
):
    memory = Memory({
        "complaint_cover_text": "Cover", "complaint_text": json.dumps(["Complaint body"]),
        "inputs/uploaded_filename": "complaint.pdf", "inputs/side": "defendant",
    })
    module = prepare(monkeypatch, product, memory)
    observed = []
    monkeypatch.setattr(module, "extract_parties_venue", lambda text, **kw: (
        observed.append(("parties", kw)) or ["Plaintiff"], ["Defendant"], "Superior Court"
    ))
    monkeypatch.setattr(module, "chat_completion", lambda prompt, **kw: (
        observed.append(("court", kw)) or "STATE"
    ))

    def parse(prompt, result_schema, **kwargs):
        assert result_schema is schema
        observed.append(("parse", kwargs))
        if len([entry for entry in observed if entry[0] == "parse"]) == 1:
            items = [item_for(i, request_key, rationale_key) for i in range(count)]
        else:
            indices = [int(value) for value in re.findall(r"ITEM(\d+)", prompt)]
            assert indices == list(range(count))
            assert "Output the complete ordered list" in prompt
            assert "Limit the output to 30" not in prompt
            assert "Limit the output to 25" not in prompt
            if product == "rfa":
                assert "Requests for Admissions" in prompt
                assert "Requests for Production of Admissions" not in prompt
            items = [item_for(i, request_key, rationale_key) for i in indices]
        return schema.model_validate({alias: items})

    monkeypatch.setattr(module, "chat_completion_parse", parse)
    generate = getattr(module, f"generate_{product}_requests_from_memory")
    result = generate(memory, "draft")

    expected = [item_for(i, request_key, rationale_key) for i in range(count)]
    assert result[response_key] == expected
    assert json.loads(memory[f"temp/consolidated_{product}s"]) == expected
    assert observed[0] == ("parties", {"model": "gpt-6-luna", "reasoning_effort": "medium"})
    assert observed[1] == ("court", {"model": "gpt-6.1-sol", "reasoning_effort": "medium"})
    assert all(kwargs == {"model": "gpt-6.1-sol", "reasoning_effort": "high"}
               for kind, kwargs in observed if kind == "parse")
    assert len([entry for entry in observed if entry[0] == "parse"]) == 2


@pytest.mark.parametrize("product,schema,alias,request_key,rationale_key,response_key", PRODUCTS)
def test_larger_chunks_reduce_proposal_calls_and_cap_parallelism(
    monkeypatch, product, schema, alias, request_key, rationale_key, response_key,
):
    lines = ["x" * 100 for _ in range(1000)]
    assert len(split_into_chunks(lines, chunk_len=10000, overlap_lines=10)) == 11
    assert len(split_into_chunks(lines, chunk_len=30000, overlap_lines=10)) == 4

    memory = Memory({
        "complaint_cover_text": "Cover", "complaint_text": json.dumps(lines),
        "inputs/uploaded_filename": "complaint.pdf", "inputs/side": "defendant",
    })
    module = prepare(monkeypatch, product, memory)
    monkeypatch.setattr(module, "split_into_chunks", split_into_chunks)
    monkeypatch.setattr(module, "extract_parties_venue", lambda text, **kw: (["P"], ["D"], "Court"))
    monkeypatch.setattr(module, "chat_completion", lambda prompt, **kw: "STATE")
    worker_counts = []
    real_executor = concurrent.futures.ThreadPoolExecutor

    def recording_executor(*args, **kwargs):
        worker_counts.append(kwargs["max_workers"])
        return real_executor(*args, **kwargs)

    monkeypatch.setattr(module.concurrent.futures, "ThreadPoolExecutor", recording_executor)
    provider_prompts = []
    request_ids = count(1)

    def parse(prompt, result_schema, **kwargs):
        provider_prompts.append(prompt)
        assert result_schema is schema
        return schema.model_validate({
            alias: [item_for(next(request_ids), request_key, rationale_key)]
        })

    monkeypatch.setattr(module, "chat_completion_parse", parse)
    result = getattr(module, f"generate_{product}_requests_from_memory")(memory, "draft")

    assert result["message"] == "Form processed successfully"
    assert worker_counts == [4]  # Consolidation runs directly, without a second pool.
    assert len(provider_prompts) == 5  # Four proposal chunks and one consolidation call.
    assert all(f"ITEM{index}" in provider_prompts[-1] for index in range(1, 5))


@pytest.mark.parametrize("product,schema,alias,request_key,rationale_key,response_key", PRODUCTS)
@pytest.mark.parametrize("failure", ["missing", "empty", "exception"])
def test_failed_single_consolidation_does_not_save_requests(
    monkeypatch, product, schema, alias, request_key, rationale_key, response_key, failure,
):
    memory = Memory({
        "complaint_cover_text": "Cover", "complaint_text": '["Complaint body"]',
        "inputs/side": "defendant",
    })
    module = prepare(monkeypatch, product, memory)
    monkeypatch.setattr(module, "extract_parties_venue", lambda text, **kw: (["P"], ["D"], "Court"))
    monkeypatch.setattr(module, "chat_completion", lambda prompt, **kw: "STATE")
    calls = []

    def parse(prompt, result_schema, **kwargs):
        assert result_schema is schema
        calls.append(prompt)
        if len(calls) == 1:
            return schema.model_validate({alias: [item_for(1, request_key, rationale_key)]})
        if failure == "exception":
            raise RuntimeError("Provider failed")
        if failure == "missing":
            return None
        return schema.model_validate({alias: []})

    monkeypatch.setattr(module, "chat_completion_parse", parse)
    result = getattr(module, f"generate_{product}_requests_from_memory")(memory, "draft")

    assert result["message"] == "error"
    assert "consolidating" in result["error_message"]
    assert len(calls) == 2
    assert f"temp/consolidated_{product}s" not in memory


@pytest.mark.parametrize("product,schema,alias,request_key,rationale_key,response_key", PRODUCTS)
@pytest.mark.parametrize("selected_target,expected_target", [(17, 17), (None, 25)])
def test_further_reduce_uses_selected_target_without_a_default_cap(
    monkeypatch, product, schema, alias, request_key, rationale_key, response_key,
    selected_target, expected_target,
):
    memory = Memory({
        f"temp/consolidated_{product}s": json.dumps([item_for(1, request_key, rationale_key)]),
        "inputs/venue": "Superior Court", "inputs/is_federal": "STATE",
    })
    module = prepare(monkeypatch, product, memory)
    prompts = []

    def parse(prompt, result_schema, **kwargs):
        assert result_schema is schema
        prompts.append(prompt)
        return schema.model_validate({alias: [item_for(1, request_key, rationale_key)]})

    monkeypatch.setattr(module, "chat_completion_parse", parse)
    reduction = getattr(module, f"further_reduce_{product}_task", None)
    if reduction is None:
        reduction = module.further_reduce_task
    data = {"draftId": "draft"}
    if selected_target is not None:
        data["targetN"] = selected_target
    result = reduction.run(data)

    assert result[f"reduced_{product}s"] == [item_for(1, request_key, rationale_key)]
    assert len(prompts) == 1
    assert "Output the complete ordered list unless a separate target number is specified" in prompts[0]
    assert f"specified target is {expected_target}" in prompts[0]
    assert f"no more than {expected_target}" in prompts[0]
    assert "Limit the output to 30" not in prompts[0]
    assert "Limit the output to 25" not in prompts[0]


@pytest.mark.parametrize("product,schema,alias,request_key,rationale_key,response_key", PRODUCTS)
@pytest.mark.parametrize("venue,court", [("", "STATE"), ("Superior Court", None),
                                          ("Superior Court", "UNKNOWN")])
def test_generation_rejects_missing_venue_or_invalid_court(
    monkeypatch, product, schema, alias, request_key, rationale_key, response_key, venue, court,
):
    memory = Memory({"complaint_cover_text": "Cover", "complaint_text": '["Body"]'})
    module = prepare(monkeypatch, product, memory)
    monkeypatch.setattr(module, "extract_parties_venue", lambda text, **kw: ([], [], venue))
    monkeypatch.setattr(module, "chat_completion", lambda prompt, **kw: court)
    monkeypatch.setattr(module, "chat_completion_parse", lambda *args, **kw: pytest.fail("No request should be drafted"))
    result = getattr(module, f"generate_{product}_requests_from_memory")(memory, "draft")
    assert result["message"] == "error"
    assert "inputs/is_federal" not in memory


@pytest.mark.parametrize("product,schema,alias,request_key,rationale_key,response_key", PRODUCTS)
def test_partial_proposal_success_and_missing_consolidation(monkeypatch, product, schema, alias,
                                                              request_key, rationale_key, response_key):
    memory = Memory({"complaint_cover_text": "Cover", "complaint_text": '["Body"]'})
    module = prepare(monkeypatch, product, memory)
    monkeypatch.setattr(module, "extract_parties_venue", lambda text, **kw: (["P"], ["D"], "Court"))
    monkeypatch.setattr(module, "chat_completion", lambda prompt, **kw: "FEDERAL")
    monkeypatch.setattr(module, "split_into_chunks", lambda *args, **kw: [["first"], ["second"]])
    calls = []

    def parse(prompt, result_schema, **kwargs):
        calls.append(prompt)
        if len(calls) == 1:
            return schema.model_validate({alias: [item_for(1, request_key, rationale_key)]})
        return None

    monkeypatch.setattr(module, "chat_completion_parse", parse)
    result = getattr(module, f"generate_{product}_requests_from_memory")(memory, "draft")
    assert result["message"] == "error"  # A successful proposal is not a successful consolidation.
    assert len(calls) == 3
    assert f"temp/consolidated_{product}s" not in memory


@pytest.mark.parametrize("product,schema,alias,request_key,rationale_key,response_key", PRODUCTS)
@pytest.mark.parametrize("court,venue", [
    ("STATE", "Court"),
    ("STATE", "Superior Court of California, County of Los Angeles"),
    ("FEDERAL", "Court"),
    ("FEDERAL", "U.S. District Court, Northern District of California"),
])
def test_reduction_and_definitions_intro_routes_and_failures(
    monkeypatch, product, schema, alias, request_key, rationale_key, response_key, court, venue,
):
    memory = Memory({
        f"temp/consolidated_{product}s": json.dumps([item_for(1, request_key, rationale_key)]),
        "inputs/venue": venue, "inputs/is_federal": court, "inputs/plaintiff": "P",
        "inputs/defendant": "D", "inputs/side": "defendant",
    })
    module = prepare(monkeypatch, product, memory)
    monkeypatch.setattr(module.Drafts, "pdf_filename", lambda draft_id: "draft.pdf")
    monkeypatch.setattr(module, f"create_{product}_pdf", lambda **kw: True)
    observed = []

    def parse(prompt, result_schema, **kwargs):
        observed.append((result_schema, kwargs))
        if result_schema is Definitions:
            return Definitions.model_validate({"definitions": [{"term": "Agreement", "definition": "A contract."}]})
        assert result_schema is schema
        return schema.model_validate({alias: [item_for(1, request_key, rationale_key)]})

    monkeypatch.setattr(module, "chat_completion_parse", parse)
    def intro(prompt, **kwargs):
        observed.append(("intro", kwargs))
        assert f"venue: {venue}" in prompt
        document_type = {
            "rog": "Interrogatories",
            "rfp": "Requests for Production of Documents",
            "rfa": "Requests for Admissions",
        }[product]
        assert f"discovery document type: {document_type}" in prompt
        federal_rule = {"rog": "33", "rfp": "34", "rfa": "36"}[product]
        california_section = {"rog": "2030.010", "rfp": "2031.010", "rfa": "2033.010"}[product]
        if court == "FEDERAL":
            assert f"Federal Rule of Civil Procedure {federal_rule}" in prompt
            assert all(f"Federal Rule of Civil Procedure {other}" not in prompt
                       for other in {"33", "34", "36"} - {federal_rule})
            assert "California Code of Civil Procedure" not in prompt
            if product != "rog":
                assert "under oath" not in prompt
        elif "California" in venue:
            assert f"California Code of Civil Procedure § {california_section}" in prompt
            assert "Federal Rule of Civil Procedure" not in prompt
            assert "not to exceed 35" not in prompt
            assert "within 30 days" not in prompt
        else:
            assert "[discovery document type]" in prompt
            assert "Federal Rule of Civil Procedure" not in prompt
            assert "California Code of Civil Procedure" not in prompt
        return "Introduction"

    monkeypatch.setattr(module, "chat_completion", intro)
    reduction = getattr(module, f"further_reduce_{product}_task", None)
    if reduction is None:
        reduction = module.further_reduce_task
    reduced = reduction.run({"draftId": "draft", "targetN": 25})
    assert reduced["reduced_" + product + "s"] == [item_for(1, request_key, rationale_key)]
    result = module.state1.run({"draftId": "draft", "coa0": "Request text", "rfa0": "Request text"})
    assert result["intro"] == "Introduction"
    assert "Agreement" in result["definition"]
    assert observed == [
        (schema, {"model": "gpt-6.1-sol", "reasoning_effort": "high"}),
        (Definitions, {"model": "gpt-6.1-sol", "reasoning_effort": "high"}),
        ("intro", {"model": "gpt-6.1-sol", "reasoning_effort": "high"}),
    ]

    monkeypatch.setattr(module, "chat_completion", lambda prompt, **kwargs: None)
    del memory["drafted_sections/intro"]
    failed = module.state1.run({"draftId": "draft", "coa0": "Request text", "rfa0": "Request text"})
    assert failed["message"] == "error"
    assert "drafted_sections/intro" not in memory


@pytest.mark.parametrize("product", ["rfp", "rfa", "rog"])
def test_intro_prompt_uses_california_only_for_explicit_state_venue(product):
    generic = load_discovery_intro_prompt(product, "STATE", "Superior Court")
    assert generic == load_discovery_intro_prompt(product, "state", "Court in CA")
    assert generic == load_discovery_intro_prompt(product, "STATE", "Californian court")
    california = load_discovery_intro_prompt(product, "STATE", "superior court of CALIFORNIA")
    assert california != generic
    assert "California Code of Civil Procedure" in california
    federal = load_discovery_intro_prompt(product, "FEDERAL", "Northern District of California")
    assert "Federal Rule of Civil Procedure" in federal
    assert "California Code of Civil Procedure" not in federal


@pytest.mark.parametrize("product,schema,alias,request_key,rationale_key,response_key", PRODUCTS)
def test_missing_reduction_or_definitions_fails_without_saving(
    monkeypatch, product, schema, alias, request_key, rationale_key, response_key,
):
    memory = Memory({
        f"temp/consolidated_{product}s": json.dumps([item_for(1, request_key, rationale_key)]),
        "inputs/venue": "Court", "inputs/is_federal": "FEDERAL", "inputs/plaintiff": "P",
        "inputs/defendant": "D", "inputs/side": "defendant",
    })
    module = prepare(monkeypatch, product, memory)
    monkeypatch.setattr(module, "chat_completion_parse", lambda *args, **kw: None)
    monkeypatch.setattr(module, "chat_completion", lambda *args, **kw: pytest.fail("Intro must not run"))
    reduction = getattr(module, f"further_reduce_{product}_task", None)
    if reduction is None:
        reduction = module.further_reduce_task
    reduced = reduction.run({"draftId": "draft"})
    assert reduced["message"] == "error"
    assert f"temp/reduced_{product}s" not in memory

    drafted = module.state1.run({"draftId": "draft", "coa0": "Request", "rfa0": "Request"})
    assert drafted["message"] == "error"
    assert not any(key.startswith("drafted_sections/") for key in memory)


@pytest.mark.parametrize("product", ["rfp", "rfa", "rog"])
def test_definitions_example_is_valid_json_after_formatting(product):
    path = f"backend/ai/prompts/{product}/DRAFT_DEFS"
    with open(path) as prompt_file:
        prompt = prompt_file.read().format(people="P and D", requests="Request")
    example = prompt.split("Output list of terms as JSON format:", 1)[1].strip()
    parsed = json.loads(example)
    assert len(parsed["definitions"]) == 2
    assert parsed["definitions"] == [
        {"term": "...", "definition": "..."},
        {"term": "...", "definition": "..."},
    ]


@pytest.mark.parametrize("schema,alias,request_key,rationale_key", [row[1:5] for row in PRODUCTS])
def test_typed_request_result_preserves_existing_keys(schema, alias, request_key, rationale_key):
    item = item_for(1, request_key, rationale_key)
    parsed = schema.model_validate({alias: [item]})
    assert parsed.model_dump(by_alias=True) == {alias: [item]}
    provider_schema = schema.model_json_schema(by_alias=True)
    assert alias in provider_schema["required"]


@pytest.mark.parametrize("product,schema,alias,request_key,rationale_key,response_key", PRODUCTS)
def test_all_seven_steps_send_expected_provider_payloads(
    monkeypatch, product, schema, alias, request_key, rationale_key, response_key,
):
    from backend.ai import base

    memory = Memory({
        "complaint_cover_text": "Cover", "complaint_text": '["Complaint body"]',
        "inputs/uploaded_filename": "complaint.pdf", "inputs/side": "defendant",
    })
    module = prepare(monkeypatch, product, memory)
    monkeypatch.setattr(module.Drafts, "pdf_filename", lambda draft_id: "draft.pdf")
    monkeypatch.setattr(module, f"create_{product}_pdf", lambda **kw: True)
    calls = []

    def response(**values):
        return SimpleNamespace(
            id="response-id", status="completed", usage=SimpleNamespace(
                input_tokens=1, output_tokens=1, total_tokens=2,
            ), **values,
        )

    class FakeResponses:
        def parse(self, **kwargs):
            calls.append(("parse", kwargs))
            result_schema = kwargs["text_format"]
            if result_schema.__name__ == "PartiesVenueResult":
                parsed = result_schema.model_validate({
                    "plaintiff": ["P"], "defendant": ["D"], "venue": "Superior Court",
                })
            elif result_schema is Definitions:
                parsed = Definitions.model_validate({
                    "definitions": [{"term": "Agreement", "definition": "A contract."}],
                })
            else:
                parsed = result_schema.model_validate({
                    alias: [item_for(1, request_key, rationale_key)],
                })
            return response(output_parsed=parsed)

        def create(self, **kwargs):
            calls.append(("create", kwargs))
            prompt = kwargs["input"][-1]["content"]
            return response(output_text="STATE" if "Output FEDERAL or STATE" in prompt else "Introduction")

    monkeypatch.setattr(base, "get_openai_client", lambda: SimpleNamespace(responses=FakeResponses()))

    generated = getattr(module, f"generate_{product}_requests_from_memory")(memory, "draft")
    assert generated[response_key] == [item_for(1, request_key, rationale_key)]
    reduction = getattr(module, f"further_reduce_{product}_task", None)
    if reduction is None:
        reduction = module.further_reduce_task
    assert reduction.run({"draftId": "draft"})["reduced_" + product + "s"]
    assert module.state1.run({"draftId": "draft", "coa0": "Request", "rfa0": "Request"})["intro"] == "Introduction"

    assert [(kind, payload["model"], payload["reasoning"]) for kind, payload in calls] == [
        ("parse", "gpt-6-luna", {"effort": "medium"}),
        ("create", "gpt-6.1-sol", {"effort": "medium"}),
        ("parse", "gpt-6.1-sol", {"effort": "high"}),
        ("parse", "gpt-6.1-sol", {"effort": "high"}),
        ("parse", "gpt-6.1-sol", {"effort": "high"}),
        ("parse", "gpt-6.1-sol", {"effort": "high"}),
        ("create", "gpt-6.1-sol", {"effort": "high"}),
    ]
    assert [payload["text_format"] for kind, payload in calls if kind == "parse"] == [
        base.PartiesVenueResult, schema, schema, schema, Definitions,
    ]
