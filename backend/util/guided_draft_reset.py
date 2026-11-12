"""Reset stale guided-drafter memory when a draft is restarted at step 0."""

from collections.abc import Iterable, MutableMapping


ANSWER_RESET_KEYS = (
    'complaint_cover_text',
    'complaint_text',
    'line_metadata',
    'ads_suggestions',
    'outline',
    'analysis/outline',
    'paragraph_warnings',
    'analysis/general_denial',
    'drafted_sections/responses',
    'drafted_sections/affirmative_defenses',
    'drafted_sections/counter_claims',
    'drafted_sections/intro',
    'inputs/uploaded_filename',
    'inputs/case_number',
    'inputs/generated_pdf_filename',
    'inputs/chosen_defendants',
    'inputs/paragraph_selections',
    'inputs/ad',
    'inputs/plaintiff',
    'inputs/defendant',
    'inputs/venue',
    'inputs/is_federal',
    'inputs/state',
    'complaint_structure',
    'sections',
    'causes_of_action',
    'summary',
    'complaint_factual_digest',
    'section_headers',
    'paragraphs',
    'hallucination_check',
    'answer_hg/case_results',
    'answer_hg/statute_results',
    'answer_hg/lookup_diagnostics',
    'answer_hg/filtered_citations',
    'answer_hg/results_html',
    'answer_hg/chunk_metadata',
    'answer/active_parse_task_id',
    'answer/active_continue_task_id',
)

RFP_RESET_KEYS = (
    'complaint_cover_text',
    'complaint_text',
    'line_metadata',
    'temp/consolidated_rfps',
    'temp/reduced_rfps',
    'drafted_sections/rfps',
    'drafted_sections/defs',
    'drafted_sections/instructions',
    'drafted_sections/intro',
    'inputs/uploaded_filename',
    'inputs/generated_pdf_filename',
    'inputs/side',
    'inputs/plaintiff',
    'inputs/defendant',
    'inputs/venue',
    'inputs/is_federal',
    'rfp/active_parse_task_id',
    'rfp/active_generate_requests_task_id',
)

ROG_RESET_KEYS = (
    'complaint_cover_text',
    'complaint_text',
    'line_metadata',
    'temp/consolidated_rogs',
    'temp/reduced_rogs',
    'drafted_sections/rogs',
    'drafted_sections/defs',
    'drafted_sections/instructions',
    'drafted_sections/intro',
    'inputs/uploaded_filename',
    'inputs/generated_pdf_filename',
    'inputs/side',
    'inputs/plaintiff',
    'inputs/defendant',
    'inputs/venue',
    'inputs/is_federal',
    'rog/active_parse_task_id',
    'rog/active_continue_task_id',
)

RFA_RESET_KEYS = (
    'complaint_cover_text',
    'complaint_text',
    'line_metadata',
    'temp/consolidated_rfas',
    'temp/reduced_rfas',
    'drafted_sections/rfas',
    'drafted_sections/defs',
    'drafted_sections/instructions',
    'drafted_sections/intro',
    'inputs/uploaded_filename',
    'inputs/generated_pdf_filename',
    'inputs/side',
    'inputs/plaintiff',
    'inputs/defendant',
    'inputs/venue',
    'inputs/is_federal',
    'rfa/active_parse_task_id',
    'rfa/active_continue_task_id',
)

RFP_RESPONSE_RESET_KEYS = (
    'cover_text',
    'body_text',
    'analysis/intro',
    'analysis/general',
    'analysis/definition_objections',
    'analysis/instruction_objections',
    'drafted_sections/definitions',
    'drafted_sections/instructions',
    'drafted_sections/intro',
    'drafted_sections/gen_obj',
    'drafted_sections/requests',
    'drafted_sections/rfp_response_text',
    'inputs/uploaded_filename',
    'inputs/case_number',
    'inputs/side',
    'inputs/requests_start_end_i',
    'inputs/generated_pdf_filename',
    'inputs/plaintiff',
    'inputs/defendant',
    'inputs/venue',
    'rfp_response/active_parse_task_id',
    'rfp_response/active_continue_task_id',
)

ROG_RESPONSE_RESET_KEYS = (
    'cover_text',
    'body_text',
    'drafted_sections/intro',
    'drafted_sections/gen_obj',
    'drafted_sections/interrogatories',
    'drafted_sections/rog_response_text',
    'inputs/uploaded_filename',
    'inputs/case_number',
    'inputs/side_selection',
    'inputs/side',
    'inputs/plaintiff',
    'inputs/defendant',
    'inputs/title',
    'inputs/venue',
    'inputs/is_federal',
    'inputs/generated_pdf_filename',
    'rog_response/active_parse_task_id',
    'rog_response/active_continue_task_id',
)

ELEMENT_CHECKER_RESET_KEYS = (
    'analysis/coa_sum',
    'analysis/element_analysis',
    'drafted_sections/fa_sum',
    'drafted_sections/fa_sum_final',
    'drafted_sections/legal_elements',
    'drafted_sections/recommendation',
    'drafted_sections/recommendation_markdown',
    'inputs/generated_pdf_filename',
    'hallucination_check',
)

DISMISS_RESET_KEYS = (
    'inputs/fa_sums',
    'coa_dict',
    'drafted_sections/fa_background',
    'drafted_sections/legal_standard',
    'drafted_sections/standard_citations',
    'drafted_sections/batches',
    'drafted_sections/current_batch_index',
    'drafted_sections/arguments',
    'drafted_sections/intro',
    'drafted_sections/conclusion',
    'inputs/generated_pdf_filename',
    'hallucination_check',
)

COMPLAINT_RESET_KEYS = (
    'analysis/coa_suggestions',
    'analysis/pfr_suggestions',
    'analysis/intro_suggestion',
    'drafted_sections/causes',
    'drafted_sections/prayer',
    'drafted_sections/intro',
    'inputs/generated_pdf_filename',
    'hallucination_check',
)


def clear_memory_keys(mem: MutableMapping[str, str], keys: Iterable[str]) -> None:
    """Delete memory keys, ignoring keys that do not exist."""
    for key in keys:
        try:
            del mem[key]
        except KeyError:
            pass
