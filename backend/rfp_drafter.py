from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from celery.result import AsyncResult
from werkzeug.utils import secure_filename
import logging
import os
from backend.constants import API_URL, STATIC_FOLDER, SOURCE_CODE_ROOT, PYTHON_EXECUTABLE
from backend.util.db import DBMemory, BaseMemory
from backend.util.data_tools import (
    create_ignore_set,
    handle_file_upload,
    invert_side,
    get_complaint_text_from_file,
    get_complaint_text_with_line_metadata_from_file,
    log_complaint_text_metadata_preview,
    prepare_complaint_text_for_memory,
)
from backend.util.guided_draft_reset import RFP_RESET_KEYS, clear_memory_keys
from backend.util.guided_prerequisites import guided_prerequisite_error
from backend.database import db
from backend.models import Drafts
from backend.ai.base import chat_completion, chat_completion_parse, extract_parties_venue, split_into_chunks
from backend.ai import base as ai_base
from backend.ai.discovery_request_schemas import Definitions, RFPRequests
from backend.ai.discovery_intro import load_discovery_intro_prompt
from backend.pdf_creator import create_rfp_pdf
from backend.word_creator import create_rfp_word
import subprocess
from backend.util.analytics import attach_step_usage, guided_step_operation, submit_with_context
from time import sleep
import json
import concurrent.futures

logger = logging.getLogger(__name__)

EXTRACTION_MODEL = 'gpt-6-luna'
CLASSIFICATION_MODEL = 'gpt-6.1-sol'
DRAFTING_MODEL = ai_base.DEFAULT_MODEL
DRAFTING_REASONING_EFFORT = 'high'
GENERATION_SOFT_TIME_LIMIT = 1200
GENERATION_HARD_TIME_LIMIT = 1260
LATER_STEP_SOFT_TIME_LIMIT = 600
LATER_STEP_HARD_TIME_LIMIT = 630
SUGGEST_RFP_CHUNK_CHARS = 30000
SUGGEST_RFP_MAX_WORKERS = 4


def _generation_timeout_error() -> dict:
    return {
        'message': 'error',
        'error_message': 'Request generation timed out after 20 minutes. Please try again.',
    }



DOC_TYPE = "RFP"
DOWNLOAD_STEP = 3

LINES_PER_CHUNK_PDF = 40
LINES_PER_CHUNK_DOCX = 60
RFP_ACTIVE_PARSE_TASK_KEY = 'rfp/active_parse_task_id'
RFP_ACTIVE_GENERATE_REQUESTS_TASK_KEY = 'rfp/active_generate_requests_task_id'
RFP_GENERATED_RESET_KEYS = (
    'temp/consolidated_rfps',
    'temp/reduced_rfps',
    'drafted_sections/rfps',
    'drafted_sections/defs',
    'drafted_sections/instructions',
    'drafted_sections/intro',
    'inputs/generated_pdf_filename',
    'inputs/plaintiff',
    'inputs/defendant',
    'inputs/venue',
    'inputs/is_federal',
    RFP_ACTIVE_GENERATE_REQUESTS_TASK_KEY,
)
RFP_STATE1_REQUIRED_KEYS = (
    'temp/consolidated_rfps',
    'inputs/plaintiff',
    'inputs/defendant',
    'inputs/venue',
    'inputs/side',
)

def draft_full_rfp(mem: BaseMemory, fn: str) -> str:
    """Drafts an RFP from available elements in memory.
    """
    # create formatted pdf
    gen_pdf_status = create_rfp_pdf(fn=fn,
                                        plaintiff=mem.get('inputs/plaintiff', "specify plaintiff"),
                                        defendant=mem.get('inputs/defendant', "specify defendant"),
                                        attorneys_contact_info=mem.user.contact_info or "No Attorneys_contact_info",
                                        venue=mem.get('inputs/venue', "Venue not yet specified"),
                                        introduction=mem.get('drafted_sections/intro', ''),
                                        definitions=mem.get('drafted_sections/defs', ''),
                                        instructions=mem.get('drafted_sections/instructions', ''),
                                        rfps=mem.get('drafted_sections/rfps', ''),
                                        is_defendant=mem['inputs/side'] == "defendant",
                                        )
    return gen_pdf_status


def draft_full_rfp_word(mem: BaseMemory, fn: str) -> str:
    """Drafts an RFP from available elements in memory.
    """
    # create formatted pdf
    gen_word_status = create_rfp_word(fn=fn,
                                        plaintiff=mem.get('inputs/plaintiff', "specify plaintiff"),
                                        defendant=mem.get('inputs/defendant', "specify defendant"),
                                        attorneys_contact_info=mem.user.contact_info or "No Attorneys_contact_info",
                                        venue=mem.get('inputs/venue', "Venue not yet specified"),
                                        introduction=mem.get('drafted_sections/intro', ''),
                                        definitions=mem.get('drafted_sections/defs', ''),
                                        instructions=mem.get('drafted_sections/instructions', ''),
                                        rfps=mem.get('drafted_sections/rfps', ''),
                                        is_defendant=mem['inputs/side'] == "defendant",
                                        )
    return gen_word_status


def launch_llm_call_process_j(prompt:str) -> subprocess.Popen:
    # launch subprocess llm_call.py
    os.environ['PYTHONPATH'] = SOURCE_CODE_ROOT + os.pathsep + os.environ.get('PYTHONPATH', '')
    process = subprocess.Popen([PYTHON_EXECUTABLE,
                             'backend/ai/llm_call_j.py',
                             prompt],
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
    return process


def _delete_memory_key(mem: BaseMemory, key: str) -> None:
    try:
        del mem[key]
    except Exception:
        pass


def clear_rfp_active_task(mem: BaseMemory, key: str, expected_task_id: str | None = None) -> None:
    current_task_id = mem.get(key, '') or ''
    if expected_task_id and current_task_id and current_task_id != expected_task_id:
        return
    _delete_memory_key(mem, key)


def _current_task_id(task) -> str | None:
    return getattr(getattr(task, 'request', None), 'id', None)


def _rfp_progress(task, message: str, progress: int) -> None:
    if task is None:
        return
    try:
        task.update_state(state='PROGRESS', meta={'message': message, 'progress': progress})
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        pass


def get_active_rfp_task_id(doc_id: int, key: str) -> str | None:
    mem = DBMemory(db=db, doc_id=doc_id)
    task_id = (mem.get(key, '') or '').strip()
    if not task_id:
        return None

    try:
        result = AsyncResult(task_id)
        if result.ready():
            clear_rfp_active_task(mem, key)
            return None
    except Exception:
        return task_id

    return task_id


def save_parsed_rfp_complaint(
    memory: BaseMemory,
    filename: str,
    side: str,
    complaint_text: str,
    line_metadata: list[dict] | None = None,
) -> list[str]:
    cover_text, text_by_lines, normalized_metadata = prepare_complaint_text_for_memory(
        filename,
        complaint_text,
        line_metadata,
        lines_per_chunk_pdf=LINES_PER_CHUNK_PDF,
        lines_per_chunk_docx=LINES_PER_CHUNK_DOCX,
    )
    logger.info("save_parsed_rfp_complaint lines=%s", len(text_by_lines))
    log_complaint_text_metadata_preview(filename, text_by_lines, normalized_metadata)
    memory['complaint_cover_text'] = cover_text
    memory['complaint_text'] = json.dumps(text_by_lines)
    memory['line_metadata'] = json.dumps(normalized_metadata)

    memory['inputs/uploaded_filename'] = filename
    memory['inputs/side'] = side.lower()

    return text_by_lines


def _load_saved_complaint_lines(memory: BaseMemory) -> list[str]:
    raw_text = memory.get('complaint_text', '')
    if not raw_text:
        return []

    try:
        text_by_lines = json.loads(raw_text)
        if isinstance(text_by_lines, list):
            return text_by_lines
    except Exception:
        pass

    return str(raw_text).splitlines()


@shared_task(bind=True, ignore_result=False)
def rfp_parse_complaint_task(self, filename: str, draftId: str, side: str) -> dict:
    doc_id = Drafts.resolve_id(draftId)
    task_id = _current_task_id(self)
    memory = DBMemory(db=db, doc_id=doc_id)

    try:
        from backend.util.account_access import chat_account_id, require_enabled_account
        require_enabled_account(chat_account_id(doc_id))
        _rfp_progress(self, 'Parsing complaint document...', 15)
        complaint_text, _, _, line_metadata = get_complaint_text_with_line_metadata_from_file(os.path.join(STATIC_FOLDER, filename))
        _rfp_progress(self, 'Saving parsed complaint text...', 80)
        require_enabled_account(chat_account_id(doc_id))
        save_parsed_rfp_complaint(memory, filename, side, complaint_text, line_metadata)
        _rfp_progress(self, 'Complaint parsed successfully.', 100)
        return {
            'message': 'Complaint parsed successfully',
            'uploaded_filename': filename,
            'has_parsed_complaint': True,
            'file_name': f'{API_URL}static/{filename}',
        }
    except Exception as exc:
        return {
            'message': 'error',
            'error_message': f'Error parsing complaint: {str(exc)}',
        }
    finally:
        clear_rfp_active_task(memory, RFP_ACTIVE_PARSE_TASK_KEY, expected_task_id=task_id)


@guided_step_operation(DOC_TYPE, 0, lambda memory, draftId, task=None: draftId)
def generate_rfp_requests_from_memory(memory: BaseMemory, draftId: str, task=None) -> dict:

    text_by_lines = _load_saved_complaint_lines(memory)
    complaint_cover_text = memory.get('complaint_cover_text', '')
    if not text_by_lines or not complaint_cover_text:
        return {
            'message': 'error',
            'error_message': 'Please upload and parse a complaint before generating requests.',
        }

    side = memory.get('inputs/side', 'defendant').lower()
    filename = memory.get('inputs/uploaded_filename', '')

    _rfp_progress(task, 'Identifying parties and venue...', 15)
    plaintiff_l, defendant_l, venue = extract_parties_venue(
        complaint_cover_text, model=EXTRACTION_MODEL, reasoning_effort='medium'
    )
    logger.info("generate_rfp_requests_from_memory plaintiff=%s", plaintiff_l)
    logger.info("generate_rfp_requests_from_memory plaintiff_count=%s", len(plaintiff_l))
    logger.info("generate_rfp_requests_from_memory defendant=%s", defendant_l)
    logger.info("generate_rfp_requests_from_memory defendant_count=%s", len(defendant_l))
    logger.info("generate_rfp_requests_from_memory venue=%s", venue)

    if not venue or not venue.strip():
        return {'message': 'error', 'error_message': 'Could not identify the court or venue. Please try again.'}

    memory['inputs/plaintiff'] = ";".join([each for each in plaintiff_l])
    logger.info("generate_rfp_requests_from_memory saved_plaintiff=%s", memory['inputs/plaintiff'])
    memory['inputs/defendant'] = ";".join([each for each in defendant_l])    # might be over-written in state1
    logger.info("generate_rfp_requests_from_memory saved_defendant=%s", memory['inputs/defendant'])
    memory['inputs/venue'] = venue
    # add step for llm federal
        # determine if this is a federal or state case
    comand_prompt = open('backend/ai/prompts/answer/DETERMINE_FEDERAL').read()
    filled_in_prompt = comand_prompt + venue
    llm_output = chat_completion(filled_in_prompt, model=CLASSIFICATION_MODEL,
                                 reasoning_effort='medium')
    is_federal = llm_output.strip().upper() if llm_output else ''
    if is_federal not in {'FEDERAL', 'STATE'}:
        return {'message': 'error', 'error_message': 'Could not determine whether the court is federal or state. Please try again.'}
    logger.info("generate_rfp_requests_from_memory is_federal=%s", is_federal)
    memory['inputs/is_federal'] = is_federal

    _rfp_progress(task, 'Generating proposed requests...', 35)
    # prepare side, names, instruction accordingly for side chosen
    if side == "defendant":
        side_dep_inst = "Analyze the plaintiff’s allegations to identify weaknesses or areas for further " \
                        "investigation. Focus on requesting documents that could undermine the plaintiff's claims or " \
                        "support potential defenses."
    else:
        side_dep_inst = "Ensure you request documents necessary to prove each element of the cause of action, " \
                        "to establish a strong foundation for the case and prevent possible dismissal."
    side_b = invert_side(side)
    side_name = memory['inputs/plaintiff'] if side == "plaintiff" else memory['inputs/defendant']
    side_b_name = memory['inputs/defendant'] if side == "plaintiff" else memory['inputs/plaintiff']

    comand_prompt = open('backend/ai/prompts/rfp/SUGGEST_RFPS').read()
    text_chunks = split_into_chunks(
        text_by_lines, chunk_len=SUGGEST_RFP_CHUNK_CHARS, overlap_lines=10
    )
    tasks = []
    for i, chunk_lines in enumerate(text_chunks):
        filled_in_prompt = comand_prompt.format(
            plaintiff_name=memory['inputs/plaintiff'],
            defendant_name=memory['inputs/defendant'],
            side=side,
            side_b=side_b,
            name=side_name,
            name_b=side_b_name,
            side_dep_inst=side_dep_inst,
            venue = venue,
            complaint=" ".join(chunk_lines)
            )
        if i == 0:
            logger.info("SUGGEST_RFPS first_prompt=\n%s", filled_in_prompt)
        tasks.append((i, filled_in_prompt))   # task : (0, prompt)

    tasks_rfps = [[] for _ in range(len(tasks))]   # initialize placeholder to hold result for each tasks
    with concurrent.futures.ThreadPoolExecutor(max_workers=SUGGEST_RFP_MAX_WORKERS) as pool:
        future_map = {
            submit_with_context(pool, chat_completion_parse, task[1], RFPRequests,
                                model=DRAFTING_MODEL, reasoning_effort=DRAFTING_REASONING_EFFORT): task for task in tasks}
        sleep(1)
        logger.info("SUGGEST_RFPS created_threads=%s", len(pool._threads))
        for fut in concurrent.futures.as_completed(future_map):
            task = future_map[fut]
            task_id = task[0]  # index
            try:
                output = fut.result()
                rfps = output.model_dump(by_alias=True)['Requests for Production of Documents'] if output else []
                tasks_rfps[task_id] = rfps
            except SoftTimeLimitExceeded:
                raise
            except Exception as exc:
                logger.warning("SUGGEST_RFPS task failed with exception: %s", exc)

    # flatten
    total_rfps = [each_rfp for rfps in tasks_rfps for each_rfp in rfps]
    logger.info("generate_rfp_requests_from_memory total_rfps=%s", len(total_rfps))
    if len(total_rfps) == 0:
        return {
            'message': 'error',
            'error_message': 'Something went wrong when generating requests, please try again.',
        }

    _rfp_progress(task, 'Consolidating proposed requests...', 75)
    CONS_COMMAND = open('backend/ai/prompts/rfp/CONSOLIDATE_RFPS').read()
    jurisdiction_context = (
        f"Jurisdiction / Court: {venue} "
        f"({'federal' if str(is_federal).lower() == 'federal' else 'state'}). "
        "When citing any legal basis in a rationale, use authority controlling in this jurisdiction.\n"
    )
    request_text = '\n'.join(rfp['Request'] for rfp in total_rfps)
    logger.info("CONSOLIDATE_RFPS candidates=%s prompt_chars=%s", len(total_rfps), len(request_text))
    try:
        output = chat_completion_parse(
            '\n'.join([jurisdiction_context, request_text, CONS_COMMAND]),
            RFPRequests,
            model=DRAFTING_MODEL,
            reasoning_effort=DRAFTING_REASONING_EFFORT,
        )
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        logger.warning("CONSOLIDATE_RFPS task failed with exception: %s", exc)
        output = None

    consolidated_rfps = (
        output.model_dump(by_alias=True)['Requests for Production of Documents']
        if output else []
    )
    logger.info("generate_rfp_requests_from_memory consolidated_rfps=%s", len(consolidated_rfps))
    if len(consolidated_rfps) == 0:
        return {
            'message': 'error',
            'error_message': 'Something went wrong when consolidating requests, please try again.',
        }

    # Persist consolidated RFPs for later further-reduce calls
    memory['temp/consolidated_rfps'] = json.dumps(consolidated_rfps)
    _rfp_progress(task, 'Requests generated.', 100)

    return {"message": "Form processed successfully",
            "requests": consolidated_rfps,  # unified key
            "coa_suggestions": consolidated_rfps,  # backward compatibility
            'file_name': f'{API_URL}static/{filename}'}


@shared_task(
    ignore_result=False,
    soft_time_limit=GENERATION_SOFT_TIME_LIMIT,
    time_limit=GENERATION_HARD_TIME_LIMIT,
)
def state0(filename: str, draftId: str, side: str) -> dict:
    """
    Legacy combined step-0 task: parse the uploaded complaint and generate RFP suggestions.
    New RFP UI uses rfp_parse_complaint_task followed by rfp_generate_requests_task.
    """
    try:
        memory = DBMemory(db=db, doc_id=Drafts.resolve_id(draftId))
        from backend.util.account_access import chat_account_id, require_enabled_account
        require_enabled_account(chat_account_id(draftId))
        complaint_text, _, _, line_metadata = get_complaint_text_with_line_metadata_from_file(os.path.join(STATIC_FOLDER, filename))
        require_enabled_account(chat_account_id(draftId))
        save_parsed_rfp_complaint(memory, filename, side, complaint_text, line_metadata)
        return generate_rfp_requests_from_memory(memory, draftId)
    except SoftTimeLimitExceeded:
        logger.warning("RFP request generation timed out in legacy state0")
        return _generation_timeout_error()


@shared_task(
    bind=True,
    ignore_result=False,
    soft_time_limit=GENERATION_SOFT_TIME_LIMIT,
    time_limit=GENERATION_HARD_TIME_LIMIT,
)
def rfp_generate_requests_task(self, data: dict) -> dict:
    draftId = data['draftId']
    doc_id = Drafts.resolve_id(draftId)
    task_id = _current_task_id(self)
    memory = DBMemory(db=db, doc_id=doc_id)

    try:
        return generate_rfp_requests_from_memory(memory, draftId, task=self)
    except SoftTimeLimitExceeded:
        logger.warning("RFP request generation timed out")
        return _generation_timeout_error()
    except Exception as exc:
        return {
            'message': 'error',
            'error_message': f'Error generating requests: {str(exc)}',
        }
    finally:
        clear_rfp_active_task(memory, RFP_ACTIVE_GENERATE_REQUESTS_TASK_KEY, expected_task_id=task_id)


def handle_state0(request):
    """State 0 is to simply upload a file, and generate fa summary and coa summary."""
    filename, error_message = handle_file_upload(request, STATIC_FOLDER)
    if error_message:
        logger.warning("handle_state0 upload_error=%s", error_message)
        return {'message': error_message}, 401

    doc_id = Drafts.resolve_id(request.form['draftId'])
    memory = DBMemory(db=db, doc_id=doc_id)
    clear_memory_keys(memory, RFP_RESET_KEYS)
    memory['inputs/uploaded_filename'] = filename

    # Call state0 as a Celery task to process the file
    result = state0.delay(filename=filename, draftId=request.form['draftId'], side=request.form['side'])
    return {"result_id": result.id}, 200  # Return the result ID of the task


def handle_upload_complaint(request):
    """Upload and parse an RFP complaint without generating requests."""
    doc_id = Drafts.resolve_id(request.form['draftId'])
    active_parse_task_id = get_active_rfp_task_id(doc_id, RFP_ACTIVE_PARSE_TASK_KEY)
    if active_parse_task_id:
        return {'message': 'Complaint parsing is already running.', 'result_id': active_parse_task_id}, 409

    active_generate_task_id = get_active_rfp_task_id(doc_id, RFP_ACTIVE_GENERATE_REQUESTS_TASK_KEY)
    if active_generate_task_id:
        return {'message': 'Request generation is already running.', 'result_id': active_generate_task_id}, 409

    filename, error_message = handle_file_upload(request, STATIC_FOLDER)
    if error_message:
        logger.warning("handle_upload_complaint upload_error=%s", error_message)
        return {'message': error_message}, 401

    memory = DBMemory(db=db, doc_id=doc_id)
    clear_memory_keys(memory, RFP_RESET_KEYS)
    memory['inputs/uploaded_filename'] = filename

    result = rfp_parse_complaint_task.delay(
        filename=filename,
        draftId=request.form['draftId'],
        side=request.form.get('side', 'defendant'),
    )
    memory[RFP_ACTIVE_PARSE_TASK_KEY] = result.id
    return {'result_id': result.id, 'uploaded_filename': filename}, 200


def handle_generate_requests(request):
    """Generate RFP requests from previously parsed complaint text."""
    data = request.get_json(silent=True) or request.form
    draft_id = data.get('draftId')
    doc_id = Drafts.resolve_id(draft_id)
    memory = DBMemory(db=db, doc_id=doc_id)

    active_parse_task_id = get_active_rfp_task_id(doc_id, RFP_ACTIVE_PARSE_TASK_KEY)
    if active_parse_task_id:
        return {'message': 'Complaint parsing is still running.', 'result_id': active_parse_task_id}, 400

    if not memory.get('complaint_text', '') or not memory.get('complaint_cover_text', ''):
        return {'message': 'Please upload and parse a complaint before generating requests.'}, 400

    active_generate_task_id = get_active_rfp_task_id(doc_id, RFP_ACTIVE_GENERATE_REQUESTS_TASK_KEY)
    if active_generate_task_id:
        return {'result_id': active_generate_task_id, 'already_running': True}, 200

    clear_memory_keys(memory, RFP_GENERATED_RESET_KEYS)
    result = rfp_generate_requests_task.delay({'draftId': draft_id})
    memory[RFP_ACTIVE_GENERATE_REQUESTS_TASK_KEY] = result.id
    return {'result_id': result.id}, 200


handle_rfp_upload_complaint = handle_upload_complaint
handle_rfp_generate_requests = handle_generate_requests


def create_RFP_section_text(rfps: list[str]) -> str:
    """Given a list of RRPs create the formatted text block for the PDF"""
    ret_str = "REQUESTS FOR PRODUCTION OF DOCUMENTS\n"

    # title will be handelded in PDF tools
    for i, rfp in enumerate(rfps):
        ret_str += f"REQUEST FOR PRODUCTION NO. {i+1}:\n"
        ret_str += f"{rfp}\n"

    return ret_str


@shared_task(ignore_result=False, soft_time_limit=LATER_STEP_SOFT_TIME_LIMIT, time_limit=LATER_STEP_HARD_TIME_LIMIT)
@guided_step_operation(DOC_TYPE, 1, lambda data: data['draftId'])
def state1(data: dict) -> str:
    draftId = Drafts.resolve_id(data['draftId'])
    # remove ignored RFPs
    ignores = create_ignore_set(data)
    rfps = [v for k,v in data.items() if k.startswith('coa') and k not in ignores]

    # draft RFP section (no LLM needed)
    rfp_section_text = create_RFP_section_text(rfps=rfps)
    logger.info("state1 rfp_section_text=\n%s", rfp_section_text)

    # save to memory/drafted_sections causes
    mem = DBMemory(db=db, doc_id=draftId)

    people = f"\ndefendant: {mem['inputs/defendant']}\n" + f"plaintiff: {mem['inputs/plaintiff']}\n"

    # Generate definitions based on requests.
    defs_terms = [] # a list of dict [{'term': '...', 'definition': '...'}, ...]
    people = f"\ndefendant: {mem['inputs/defendant']}\n" + f"plaintiff: {mem['inputs/plaintiff']}\n"
    filled_in_prompt = open('backend/ai/prompts/rfp/DRAFT_DEFS').read().format(
        people=people,
        requests = rfp_section_text)
    defs_output = chat_completion_parse(filled_in_prompt, Definitions,
                                        model=DRAFTING_MODEL, reasoning_effort=DRAFTING_REASONING_EFFORT)
    defs_terms = defs_output.model_dump()['definitions'] if defs_output else []
    logger.info("state1 defs_terms_len=%s", len(defs_terms))
    # add a check to see if defs_terms is empty, if it's empty, we first try to regenerate the llm defs_output, and if it is still empty, we send message to the user and ask them to retry later. 
    # the meesage is not the same as the one with contant support, it is the one says 'something went wrong when generating responses, please try again.'
    
    # defs_terms = []  # for testing error message, we set it to empty list
    if len(defs_terms) == 0:
        return {"message": "error", "error_message": "Something went wrong when generating definitions, please try again."}

    logger.info("state1 defs_terms_confirmed_len=%s", len(defs_terms))
    defs_list = [f"{i+1}. \"{definition['term'].capitalize()}\": {definition['definition']}\n" for i, definition in enumerate(defs_terms)]
    defs_section = open('backend/ai/prompts/rfp/DEFS_INTRO_TEMPLATE').read() + '\n'.join(defs_list)

    # draft intro (needs defendant and plaintiff)
    instruction_section = open('backend/ai/prompts/rfp/INSTRUCTIONS_TEMPLATE').read()
    formatted_instruction_section = instruction_section.format(side = mem['inputs/side'])

    # LLM call for intro
    side = mem['inputs/side']
    is_federal = mem['inputs/is_federal']
    logger.info("state1 side=%s", side)
    logger.info("state1 is_federal=%s", is_federal)

    intro_command = load_discovery_intro_prompt("rfp", is_federal, mem['inputs/venue'])
    
    filled_in_prompt = intro_command.format(
    jurisdiction = mem['inputs/venue'],
    side = side.capitalize(),
    plaintiff = mem['inputs/plaintiff'],
    defendant = mem['inputs/defendant'],
    venue = mem['inputs/venue'],
    is_federal = is_federal,
    discovery_type = "Requests for Production of Documents"
    )

    intro_section = chat_completion(filled_in_prompt, model=DRAFTING_MODEL,
                                    reasoning_effort=DRAFTING_REASONING_EFFORT)
    if not intro_section or not intro_section.strip():
        return {'message': 'error', 'error_message': 'Something went wrong when generating the introduction, please try again.'}
    mem['drafted_sections/rfps'] = rfp_section_text
    mem['drafted_sections/defs'] = defs_section
    mem['drafted_sections/instructions'] = formatted_instruction_section
    mem['drafted_sections/intro'] = intro_section

    # update full complaint
    filename = Drafts.pdf_filename(data['draftId'])
    mem['inputs/generated_pdf_filename'] = filename
    # draft_full_rfp(mem, f"backend/static/{filename}")
    gen_pdf_status = create_rfp_pdf(fn=f"backend/static/{filename}",
                                    plaintiff=mem.get('inputs/plaintiff', "specify plaintiff"),
                                    defendant=mem.get('inputs/defendant', "specify defendant"),
                                    attorneys_contact_info=mem.user.contact_info or "No Attorneys_contact_info",
                                    venue=mem.get('inputs/venue', "Venue not yet specified"),
                                    introduction='',
                                    definitions='',
                                    instructions='',
                                    rfps=mem.get('drafted_sections/rfps', ''),
                                    is_defendant=mem['inputs/side'] == "defendant",
                                    )

    return {"file_name": f'{API_URL}static/{filename}',
            "intro": intro_section,
            "definition": defs_section,
            "instructions": instruction_section}


def handle_state1(request):
    result = state1.delay(request.form)
    return {"result_id": result.id}, 200


def _rfp_state1_prerequisite_error(data: dict) -> dict | None:
    draft_id = Drafts.resolve_id(data['draftId'])
    memory = DBMemory(db=db, doc_id=draft_id)
    return guided_prerequisite_error(memory, RFP_STATE1_REQUIRED_KEYS, 'requests for production')


@shared_task(ignore_result=False)
@guided_step_operation(DOC_TYPE, 2, lambda data: data['draftId'])
def state2(data: dict) -> str:
    draftId = Drafts.resolve_id(data['draftId'])

    intro_text = data['introBody']
    instructions_text = data['instructionsBody']
    defs_text = data['defsBody']

    # save to memory/drafted_sections/intro
    memory = DBMemory(db=db, doc_id=draftId)
    memory['drafted_sections/intro'] = intro_text
    memory['drafted_sections/instructions'] = instructions_text
    memory['drafted_sections/defs'] = defs_text

    # update full complaint
    filename = Drafts.pdf_filename(data['draftId'])
    memory['inputs/generated_pdf_filename'] = filename
    draft_full_rfp(memory, f"backend/static/{filename}")

    # word_filename = f"{data['draftId']}.docx"
    # draft_full_rfp_word(memory, f"backend/static/{word_filename}")

    return {"file_name": f'{API_URL}static/{filename}'}


def handle_state2(request):
    result = state2.delay(request.form)
    return {"result_id": result.id}, 200


def route_rfp_logic(request):
    """Decides which step should be used and passes request to those steps."""

    # switch on appState in request
    data = request.form
    logger.info("route_rfp_logic data=%s", data)
    appState = int(data['appState'])

    if appState == 0:
        return handle_state0(request)
    elif appState == 1:
        prerequisite_error = _rfp_state1_prerequisite_error(data)
        if prerequisite_error:
            return prerequisite_error, 400
        return handle_state1(request)
    elif appState == 2:
        return handle_state2(request)


@shared_task(ignore_result=False, soft_time_limit=LATER_STEP_SOFT_TIME_LIMIT, time_limit=LATER_STEP_HARD_TIME_LIMIT)
@attach_step_usage(DOC_TYPE, 0, lambda data: data['draftId'])
def further_reduce_rfp_task(data: dict) -> dict:
    draft_id = Drafts.resolve_id(data['draftId'])
    target_n = int(data.get('targetN', 25))
    mem = DBMemory(db=db, doc_id=draft_id)
    logger.info("further_reduce_rfp_task target_n=%s", target_n)

    consolidated_rfps = json.loads(mem.get('temp/consolidated_rfps', '[]'))
    CONS_COMMAND = open('backend/ai/prompts/rfp/CONSOLIDATE_RFPS').read()
    target_instruction = (
        f"For this further reduction, the specified target is {target_n}. "
        f"Output no more than {target_n} of the most critical Requests for Production of Documents."
    )
    is_federal = mem.get('inputs/is_federal', '')
    jurisdiction_context = (
        f"Jurisdiction / Court: {mem.get('inputs/venue', 'Venue not yet specified')} "
        f"({'federal' if str(is_federal).lower() == 'federal' else 'state'}). "
        "When citing any legal basis in a rationale, use authority controlling in this jurisdiction.\n"
    )
    result = "\n".join([f"Topic: {item.get('Topic','')}\nRequest: {item['Request']}\nJustification: {item.get('Justification','')}" for item in consolidated_rfps])
    further_reduce_prompt = "\n".join([jurisdiction_context, result, CONS_COMMAND, target_instruction])
    output = chat_completion_parse(further_reduce_prompt, RFPRequests,
                                   model=DRAFTING_MODEL, reasoning_effort=DRAFTING_REASONING_EFFORT)
    reduced = output.model_dump(by_alias=True)['Requests for Production of Documents'] if output else []
    logger.info("further_reduce_rfp_task reduced_len=%s", len(reduced))

    # reduced = [] # for testing error message, we set it to empty list
    if len(reduced) == 0:
        return {"message": "error", "error_message": "Something went wrong when reducing the number of requests, please try again."}
    mem['temp/reduced_rfps'] = json.dumps(reduced)
    return {"requests": reduced, "reduced_rfps": reduced}
