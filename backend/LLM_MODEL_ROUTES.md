# LLM model routing inventory

Verified against the backend call sites on 2026-10-20. This describes **effective routes in the current code**, not recommended models. Function names in the Source column are relative to `backend/`; calls are conditional on the user reaching that step. Parallel batches use the same route for each provider request.

`ai/base.py:DEFAULT_MODEL` is `gpt-6.1-sol`. Its Responses helpers send `medium` when effort is omitted for that model (and for legacy `gpt-6-sol`); an explicit effort wins. “Not explicitly set” means this code sends no reasoning-effort override, **not** that the provider uses `medium`. Audio models do not have a reasoning-effort setting in these calls.

## Case Assistant and Doc Search

Case Assistant Fast turns use `gpt-6-luna`/`medium`; Deep turns use `base.DEFAULT_MODEL`, currently `gpt-6.1-sol`, with `medium` for intent, extraction, and summaries and explicit `high` for answers and drafting/edits. Legacy model-valued requests are translated into a mode; they do not select the provider model. The background brief receives the originating turn's mode and remains `medium`; older brief jobs without a mode use the base model. Required Case Assistant/Case Background document preparation uses Luna/`medium` independently of chat mode. Doc Search metadata and Word export, retain Sol/`medium`. Separate Verify jobs use the shared Guard's Sol/`high` final audits. Chat-time Luna and Sol each receive a 1,050,000-token context budget with the existing 128,000-token completion reserve; preparation uses its own smaller per-call budgets.

| Product | LLM-bearing step | Effective model | Effort | Selection / inheritance | Source function |
| --- | --- | --- | --- | --- | --- |
| Case Assistant | Intent classification and workflow routing | Fast: `gpt-6-luna`; Deep: `gpt-6.1-sol` | `medium` | Mode-resolved; explicit | `everything.py:_everything_phase1_body` → `classify_intent_decision` |
| Case Assistant | Relevant excerpts from each selected file | Fast: `gpt-6-luna`; Deep: `gpt-6.1-sol` | `medium` | Exact-scope source blocks and validated quotes; exhaustive requests visit every range | `util/prepared_retrieval.py:extract_evidence` |
| Case Assistant | Older-chat keyword expansion | Fast: `gpt-6-luna`; Deep: `gpt-6.1-sol` | `medium` | Explicit model/effort from phase 2 | `everything.py:retrieve_relevant_chat_turns` |
| Case Assistant | Preserve-original target selection | Fast: `gpt-6-luna`; Deep: `gpt-6.1-sol` | `medium` | Mode-resolved | `everything.py:_everything_phase2_body` → `run_preserve_original_uploaded_revision` |
| Case Assistant | Uploaded/current-draft heading, section, and block structuring | Fast: `gpt-6-luna`; Deep: `gpt-6.1-sol` | `medium` | Only when structuring is needed | `everything.py:_everything_phase2_body` → `util/draft_structure.py` |
| Case Assistant | Final conversational answer | Fast: `gpt-6-luna`; Deep: `gpt-6.1-sol` | Fast: `medium`; Deep: `high` | Mode-resolved output | `everything.py:_everything_phase2_body` |
| Case Assistant | Create a new draft | Fast: `gpt-6-luna`; Deep: `gpt-6.1-sol` | Fast: `medium`; Deep: `high` | Mode-resolved output | `everything.py:_everything_phase2_body` |
| Case Assistant | Edit the current draft | Fast: `gpt-6-luna`; Deep: `gpt-6.1-sol` | Fast: `medium`; Deep: `high` | Mode-resolved output | `everything.py:_everything_phase2_body` |
| Case Assistant | Revise an uploaded draft, including preserve-original edits | Fast: `gpt-6-luna`; Deep: `gpt-6.1-sol` | Fast: `medium`; Deep: `high` | Target selection/structuring remain `medium`; edits use output effort | `everything.py:_everything_phase2_body` |
| Case Assistant | Background conversation brief | Fast: `gpt-6-luna`; Deep/legacy: `gpt-6.1-sol` | `medium` | Separate post-answer job; mode captured in task payload | `everything.py:_run_brief_update` → `update_conversation_brief` |
| Case Assistant | Word-export formatting specification | `gpt-6.1-sol` | `medium` | Explicit override of the resolver's own default | `everything.py:draft_full_everything_word` |
| Doc Search (Smart Search) | Intent classification | `gpt-6.1-sol` | `medium` | Base default; client model ignored | `smart_search.py:smart_search_task` / `smart_search_phase1` → `everything.py:classify_intent` |
| Doc Search | BM25 search-keyword expansion | `gpt-6.1-sol` | `high` | Explicit model/effort; BM25 ranking itself has no LLM | `smart_search.py:smart_search_task` / `smart_search_phase1` → `everything.py:get_relevant_content_bm25` |
| Doc Search | Older-chat keyword expansion | `gpt-6.1-sol` | `high` | Explicit model/effort | `smart_search.py:smart_search_task` / `smart_search_phase2` → `everything.py:retrieve_relevant_chat_turns` |
| Doc Search | Answer generation | `gpt-6.1-sol` | `high` | Explicit effort | `smart_search.py:smart_search_task` / `smart_search_phase2` |
| Doc Search | Conversation-brief update | `gpt-6.1-sol` | `medium` | Base helper supplies effort | `smart_search.py:smart_search_task` / `smart_search_phase2` → `everything.py:update_conversation_brief` |

## Hallucination Guard, Case Viewer, and Voice Mentor

The shared Hallucination Guard runner serves standalone Guard, Case Assistant Verify, and Answer Drafter's defense check. Its case audit includes quote/proposition analysis; there is no separate quote-extraction model call. Free Law/CourtListener lookup and deterministic quote checks are not LLM calls.

| Product | LLM-bearing step | Effective model | Effort | Selection / inheritance | Source function |
| --- | --- | --- | --- | --- | --- |
| Hallucination Guard (shared) | Holding from a retrieved opinion | `gpt-6.1-sol` | `medium` | Base default | `hallucination_guard.py:_enrich_validated_cases_for_standalone` → `util/free_law_tools.py:extract_holding_from_opinion` |
| Hallucination Guard (shared) | Citation-resolution web search | `gpt-6.1-sol` | `medium` | Base default | `hallucination_guard.py:_resolve_citation_via_web_search` |
| Hallucination Guard (shared) | Final case/citation, quote, and proposition audit | `gpt-6.1-sol` | `high` | Base model; explicit effort; parallel with statute audit | `hallucination_guard.py:run_hallucination_guard_on_text` |
| Hallucination Guard (shared) | Final statute audit | `gpt-6.1-sol` | `high` | Base model; explicit effort; parallel with case audit | `hallucination_guard.py:run_hallucination_guard_on_text` |
| Case Viewer | One-call holding and opinion-grounded case overview | `gpt-6.1-sol` | `medium` | Base default; cached results need no call | `caseviewer_service.py:build_live_caseviewer_payload` → `util/free_law_tools.py:extract_holding_and_case_overview_from_opinion` |
| Voice Mentor | Audio transcription | `whisper-1` | N/A | Fixed audio model | `practice.py:practice_transcribe` |
| Voice Mentor | Interactive mentor/judge/opponent response | `gpt-6.1-sol` | `high` | Explicit output effort | `practice.py:practice_chat` |
| Voice Mentor | Post-session feedback summary | `gpt-6.1-sol` | `high` | Includes assessment, recommendations, and a suggested rewrite; explicit effort | `practice.py:practice_summary` |
| Voice Mentor | Spoken response | `tts-1` | N/A | Fixed audio model | `practice.py:practice_tts` |

## Guided drafting and review products

Writing and analysis constants migrated from GPT-5.5 below initialize from `base.DEFAULT_MODEL`; named constants remain available for isolated benchmark overrides. Explicit Luna extraction routes remain separate.

Generation, suggestions, substantive legal analysis, and final findings audits use explicit `high` effort. Intermediate extraction, summaries, classification, and formatting retain `medium`; the base helper default remains `medium`.

| Product | LLM-bearing step | Effective model | Effort | Selection / inheritance | Source function |
| --- | --- | --- | --- | --- | --- |
| Answer Drafter | Complaint structure: caption, parties, jurisdiction, body start | `gpt-6-luna` | `medium` | Explicit extraction route | `answer_drafter.py:get_answer_complaint_structure_from_llm` |
| Answer Drafter | Section and cause-of-action boundaries | `gpt-6-luna` | `medium` | Parallel chunk extraction | `answer_drafter.py:_extract_answer_section_items_for_chunk` |
| Answer Drafter | Numbered-paragraph locations | `gpt-6-luna` | `medium` | Parallel chunk extraction | `answer_drafter.py:_extract_answer_para_rows_for_chunk` |
| Answer Drafter | Factual-digest chunks and consolidated digest | `gpt-6-luna` | `medium` | Chunk calls plus reducer | `answer_drafter.py:_extract_answer_fact_digest_for_chunk` / `_reduce_answer_fact_digest_with_llm` |
| Answer Drafter | Affirmative-defense suggestions per claim | `gpt-6.1-sol` | `high` | Parallel per-cause calls | `answer_drafter.py:_extract_answer_ad_suggestions_for_coa` |
| Answer Drafter | Batched paragraph responses | `gpt-6.1-sol` | `high` | One request per batch, not per paragraph | `answer_drafter.py:_extract_answer_response_chunk` |
| Answer Drafter | Lawyer-selected and user-added affirmative-defense drafting | `gpt-6.1-sol` | `high` | Selected defenses only | `answer_drafter.py:state2` |
| Answer Drafter | General-denial opening; optional counterclaim | `gpt-6.1-sol` | `high` | Conditional text calls | `answer_drafter.py:generate_answer_analysis_from_memory` / `state3` |
| Answer Drafter | Defense hallucination check | `gpt-6.1-sol` | `high` | Uses shared Guard final-audit route above | `answer_drafter.py:run_answer_hallucination_guard_on_ad_text` |
| Complaint Drafter | Step 0 factual allegations, relief suggestions, introduction, cause suggestions, and parties/jurisdiction/venue | `gpt-6.1-sol` | `high` | Five parallel typed writing/suggestion subprocess calls, not extraction | `complaint_drafter.py:state0` → `ai/llm_call_j.py:_completion` |
| Complaint Drafter | Draft causes of action | `gpt-6.1-sol` | `high` | Explicit | `complaint_drafter.py:state1` |
| Complaint Drafter | Draft prayer for relief | `gpt-6.1-sol` | `high` | Explicit | `complaint_drafter.py:state2` |
| Complaint Drafter | Citation findings | `gpt-6.1-sol` | `high` | Explicit final-audit effort | `complaint_drafter.py:state3` |
| Complaint Drafter | Additional citation suggestions | `gpt-6.1-sol` | `high` | Explicit | `complaint_drafter.py:regenerate_citations_complaint` |
| Element Checker | Upload parties/venue, pre-cause headings, factual-summary chunks, consolidated factual summary, and per-cause summaries | `gpt-6-luna` | `medium` | Explicit; chunk calls are parallel | `element_checker.py:state0` |
| Element Checker | Cause-of-action boundaries determining cause count | `gpt-6.1-sol` | `medium` | Explicit exception to Luna upload route | `element_checker.py:state0` → `ai/base.py:get_coa_loc` |
| Element Checker | Legal-element/authority proposals, weakness analysis, and written recommendations | `gpt-6.1-sol` | `high` | Explicit parallel analysis calls | `element_checker.py:state1` / `state2` |
| Element Checker | Markdown formatting of elements and recommendations | `gpt-6.1-sol` | `medium` | Base default; separate from substantive analysis | `element_checker.py:state2` / `state3` |
| Element Checker | Final findings check | `gpt-6.1-sol` | `high` | Explicit final-audit effort; separate legacy prompt, not shared Guard runner | `element_checker.py:state3` |
| Element Checker | Additional citation suggestions | `gpt-6.1-sol` | `high` | Explicit | `element_checker.py:regenerate_citations` |

The RFP, RFA, and ROG **request** drafters share extraction and court-classification routes, with all drafting stages using the base model at high effort. Consolidation and Further Reduce select/rewrite legal requests rather than merely extracting or summarizing. Their generation functions are `generate_rfp_requests_from_memory`, `generate_rfa_requests_from_memory`, and `generate_rog_requests_from_memory`. They are different products from the RFP/ROG **response** drafters below.

| Product | LLM-bearing step | Effective model | Effort | Selection / inheritance | Source function |
| --- | --- | --- | --- | --- | --- |
| RFP / RFA / ROG request drafters | Extract parties and venue | `gpt-6-luna` | `medium` | Explicit in each product | `rfp_drafter.py` / `rfa_drafter.py` / `rog_drafter.py:generate_*_requests_from_memory` |
| RFP / RFA / ROG request drafters | Classify federal vs. state court | `gpt-6.1-sol` | `medium` | Explicit in each product | Same generation functions |
| RFP request drafter | Propose requests from complaint chunks | `gpt-6.1-sol` | `high` | Explicit; at most four parallel calls | `rfp_drafter.py:generate_rfp_requests_from_memory` |
| RFP request drafter | Consolidate all proposed requests | `gpt-6.1-sol` | `high` | Explicit; one call | Same generation function |
| RFP request drafter | Optional further reduction | `gpt-6.1-sol` | `high` | Explicit | `rfp_drafter.py:further_reduce_rfp_task` |
| RFP request drafter | Draft definitions and introduction | `gpt-6.1-sol` | `high` | Explicit | `rfp_drafter.py:state1` |
| RFA / ROG request drafters | Propose requests from complaint chunks | `gpt-6.1-sol` | `high` | Explicit; at most four parallel calls each | Their generation functions |
| RFA / ROG request drafters | Consolidate all proposed requests | `gpt-6.1-sol` | `high` | Explicit; one call each | Their generation functions |
| RFA / ROG request drafters | Optional further reduction | `gpt-6.1-sol` | `high` | Explicit | `rfa_drafter.py:further_reduce_rfa_task`; `rog_drafter.py:further_reduce_task` |
| RFA / ROG request drafters | Draft definitions and introduction | `gpt-6.1-sol` | `high` | Explicit | Each product's `state1` |
| RFP Response | Extract parties and venue | `gpt-6-luna` | `medium` | Explicit | `rfp_response_drafter.py:generate_rfp_response_setup_from_memory` |
| RFP Response | Locate requests section in uploaded document | `gpt-6.1-sol` | `medium` | Base default; conditional on heading count | Same setup function → `ai/base.py:get_rfp_section_text` |
| RFP Response | Suggest definition and instruction objections | `gpt-6.1-sol` | `high` | Explicit writing effort; two calls | Same setup function |
| RFP Response | Extract numbered request list | `gpt-6-luna` | `medium` | Explicit typed extraction | `rfp_response_drafter.py:state1` |
| RFP Response | Batched objection suggestions | `gpt-6.1-sol` | `high` | Explicit parallel batches of 30 (max 4 concurrent) | `rfp_response_drafter.py:state1` |
| RFP Response | Batched final rewrites | `gpt-6.1-sol` | `high` | Explicit parallel batches of 30 (max 4 concurrent) | `rfp_response_drafter.py:state2` |
| ROG Response | Extract parties, title, and venue | `gpt-6-luna` | `medium` | Explicit | `rog_response_drafter.py:generate_rog_response_setup_from_memory` |
| ROG Response | Classify federal vs. state court | `gpt-6-luna` | `medium` | Explicit | Same setup function |
| ROG Response | Draft introduction | `gpt-6.1-sol` | `high` | Explicit | Same setup function |
| ROG Response | Extract interrogatory list | `gpt-6-luna` | `medium` | Explicit typed extraction | Same setup function |
| ROG Response | Batched objection suggestions | `gpt-6.1-sol` | `high` | Explicit parallel batches of 30 (max 4 concurrent) | `rog_response_drafter.py:state1` |
| ROG Response | Batched final rewrites | `gpt-6.1-sol` | `high` | Explicit parallel batches of up to 30 custom notes (max 4 concurrent) | `rog_response_drafter.py:state2` |

## Shared utilities and non-LLM steps

| Utility / consumer | Effective model | Effort | Routing detail | Source function |
| --- | --- | --- | --- | --- |
| Required Case Assistant/Case Background preparation: orientation, section records, digest and compact card | `gpt-6-luna` | `medium` | Independent of chat mode; full readable-source coverage; 40K section / 48K reduction input; 32,768 generated-token allowance including reasoning; 6K section records, 1–2K detailed digest and 200–400-token preview | `document_preparation_tasks.py:prepare_document` → `util/preparation_summary.py:summarize_document` |
| Doc Search and other legacy-product metadata | `gpt-6.1-sol` | `medium` | Live task overrides the standalone helper default; managed preparation records prevent late legacy tasks overwriting prepared metadata | `document_metadata_tasks.py:document_metadata_task` → `util/document_metadata.py:extract_document_metadata` |
| Search-keyword expansion | `gpt-6.1-sol` | Doc Search: `high`; default/Case Assistant Deep: `medium` | Defaults to `ai/base.py:DEFAULT_MODEL`; callers override model/effort; Case Assistant Fast passes Luna/`medium` | `ai/keyword_expander.py:expand_search_keywords` |
| BM25 retrieval wrapper's keyword expansion | Doc Search: `gpt-6.1-sol`; bare wrapper: `gpt-5.5` | Doc Search: `high`; bare wrapper: not explicitly set | Doc Search passes the base model and explicit effort; the wrapper's optional override leaves other callers unchanged | `everything.py:get_relevant_content_bm25` |
| Opinion holding / Case Viewer synopsis | `gpt-6.1-sol` | `medium` | Base default when caller does not override | `util/free_law_tools.py:extract_holding_from_opinion` / `extract_holding_and_case_overview_from_opinion` |
| Draft heading/block structuring | Case Assistant Fast: `gpt-6-luna`; Deep: `gpt-6.1-sol`; bare helper: `gpt-5.5` | Case Assistant: `medium`; bare helper: not explicitly set | Case Assistant passes mode-resolved overrides; `util/draft_structure.py:DEFAULT_MODEL` is only the bare-helper fallback | `util/draft_structure.py:structure_draft_text` / `normalize_draft_structure` |
| Pleading/Word format resolution | Case Assistant: `gpt-6.1-sol`; bare helper: `gpt-5.5` | Case Assistant: `medium`; bare helper: not explicitly set | Case Assistant passes overrides; the resolver retains its own default | `util/pleading_format_resolver.py:resolve_format_spec_for_export` |

BM25 ranking, Free Law/CourtListener lookups, citation scanning, ordinary upload parsing, and PDF/Word rendering do not themselves choose an LLM model. Case Assistant's Word-format resolution is the exception listed above. The PDF/Word generators in the guided drafters consume already generated text; they do not make an additional model call. `ai/base.py` also has embedding and provider-demo helpers with no active product call site, so they are not treated as product routes here.

## Still-routable sunset products

These paths remain in code but are not part of the active product inventory.

| Product | LLM-bearing step | Effective model | Effort | Selection / inheritance | Source function |
| --- | --- | --- | --- | --- | --- |
| Research | Deep/default research response | `gpt-5.5` | Not explicitly set | Frontend Deep/default | `research.py:research_task` |
| Research | Fast research response | `gpt-6.1-sol` | `high` | Explicit output effort; legacy `gpt-6-sol` requests are remapped | `research.py:research_task` |
| Motion to Dismiss | Setup extraction/classification and cause/fact chunk summaries | `gpt-6.1-sol` | `medium` | Inherits base default | `dismiss_drafter.py:state0` |
| Motion to Dismiss | Factual-background motion-section drafting | `gpt-6.1-sol` | `high` | Drafts pleading text from summaries, rather than summarizing inputs | `dismiss_drafter.py:_synthesize_factual_background` |
| Motion to Dismiss | Legal standard and factual-background update | `gpt-6.1-sol` | `high` | Base model; explicit effort | `dismiss_drafter.py:state0` / `route_motion_dismiss_update_background` |
| Motion to Dismiss | Element and legal-ground analysis | `gpt-6.1-sol` | `high` | Base model; explicit effort | `dismiss_drafter.py:state1` |
| Motion to Dismiss | Relevant-fact selection for argument expansion | `gpt-6.1-sol` | `medium` | Source-grounded extraction before argument revision | `dismiss_drafter.py:motion_dismiss_expand_argument` (`SUGGEST_BATCH_FA`) |
| Motion to Dismiss | Argument drafting and revisions | `gpt-6.1-sol` | `high` | Base model; explicit effort | `dismiss_drafter.py:state2` / `motion_dismiss_expand_argument` |
| Motion to Dismiss | Introduction/conclusion | `gpt-6.1-sol` | `high` | Explicit writing effort | `dismiss_drafter.py:state3` |
| Motion to Dismiss | Citation extraction for table of authorities | `gpt-6.1-sol` | `medium` | Inherits base default | `dismiss_drafter.py:extract_citations_with_openai` |
| Motion to Dismiss | Final findings check | `gpt-6.1-sol` | `high` | Base model; explicit effort | `dismiss_drafter.py:state4` |

Research's backend also accepts other legacy client-supplied model IDs; except for remapping `gpt-6-sol` to `gpt-6.1-sol`, it forwards those IDs without an explicit effort override.

When changing a route, update the corresponding row after checking the product call site **and** the shared helper's effective-effort rule; a model string in a helper signature alone may not be the production route.
