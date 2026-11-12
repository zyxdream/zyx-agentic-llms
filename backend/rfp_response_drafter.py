from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from celery.result import AsyncResult
from werkzeug.utils import secure_filename
import logging
import os
from backend.constants import API_URL, STATIC_FOLDER, SOURCE_CODE_ROOT, PYTHON_EXECUTABLE
from backend.util.db import DBMemory, BaseMemory
from backend.util.data_tools import create_ignore_set, allowed_filetypes, invert_side, extract_from_json, get_complaint_text_from_file, remove_biz_type, handle_file_upload
from backend.util.guided_draft_reset import RFP_RESPONSE_RESET_KEYS, clear_memory_keys
from backend.util.guided_prerequisites import guided_prerequisite_error
from backend.database import db
from backend.models import Drafts
from backend.pdf_creator import create_rfp_response_pdf
from backend.word_creator import create_rfp_response_word
from backend.ai.base import get_rfp_section_text, chat_completion_j, chat_completion_j_list_requests, chat_completion_parse
from backend.ai.base import extract_parties_venue, num_tokens_from_string
from backend.ai import base as ai_base
from backend.ai.complaint_parser import get_last_ocr_warning
import subprocess
from backend.util.analytics import guided_step_operation, submit_with_context
import json
import concurrent.futures
from pydantic import BaseModel

logger = logging.getLogger(__name__)


DOC_TYPE = "RFP_RESPONSE"
DOWNLOAD_STEP = 3
EXTRACTION_MODEL = "gpt-6-luna"
EXTRACTION_REASONING_EFFORT = "medium"
WRITING_MODEL = ai_base.DEFAULT_MODEL
WRITING_REASONING_EFFORT = "high"
WRITING_BATCH_SIZE = 30
WRITING_MAX_WORKERS = 4
WRITING_SOFT_TIME_LIMIT = 1200
WRITING_HARD_TIME_LIMIT = 1260


class RFPObjection(BaseModel):
    index_id: int
    reason: str
    objection: str


class RFPObjectionBatch(BaseModel):
    objections: list[RFPObjection]


class RFPResponse(BaseModel):
    index_id: int
    response: str


class RFPResponseBatch(BaseModel):
    responses: list[RFPResponse]


def _rfp_proportionality_guidance(venue: str) -> str:
    normalized_venue = " ".join((venue or "").lower().split())
    if any(court in normalized_venue for court in (
        "united states district court", "u.s. district court", "us district court",
    )):
        return "Apply the FRCP 26(b)(1) factors (importance, burden, amount in controversy, etc.)."
    return "Assess importance, burden, amount in controversy, and proportionality under the applicable court's rules without assuming a federal rule applies."

LINES_PER_CHUNK_PDF = 40
LINES_PER_CHUNK_DOCX = 60
RFP_RESPONSE_ACTIVE_PARSE_TASK_KEY = 'rfp_response/active_parse_task_id'
RFP_RESPONSE_ACTIVE_CONTINUE_TASK_KEY = 'rfp_response/active_continue_task_id'
RFP_RESPONSE_GENERATED_RESET_KEYS = (
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
    'inputs/generated_pdf_filename',
    'inputs/requests_start_end_i',
    'inputs/plaintiff',
    'inputs/defendant',
    'inputs/venue',
    RFP_RESPONSE_ACTIVE_CONTINUE_TASK_KEY,
)
RFP_RESPONSE_STATE1_REQUIRED_KEYS = (
    'inputs/requests_start_end_i',
    'inputs/plaintiff',
    'inputs/defendant',
    'inputs/side',
)


def _delete_memory_key(mem: BaseMemory, key: str) -> None:
    try:
        del mem[key]
    except Exception:
        pass


def clear_rfp_response_active_task(mem: BaseMemory, key: str, expected_task_id: str | None = None) -> None:
    current_task_id = mem.get(key, '') or ''
    if expected_task_id and current_task_id and current_task_id != expected_task_id:
        return
    _delete_memory_key(mem, key)


def _current_task_id(task) -> str | None:
    return getattr(getattr(task, 'request', None), 'id', None)


def _rfp_response_progress(task, message: str, progress: int, extra_meta: dict | None = None) -> None:
    if task is None:
        return
    meta = {'message': message, 'progress': progress}
    if extra_meta:
        meta.update(extra_meta)
    try:
        task.update_state(state='PROGRESS', meta=meta)
    except Exception:
        pass


def get_active_rfp_response_task_id(doc_id: int, key: str) -> str | None:
    mem = DBMemory(db=db, doc_id=doc_id)
    task_id = (mem.get(key, '') or '').strip()
    if not task_id:
        return None

    try:
        result = AsyncResult(task_id)
        if result.ready():
            clear_rfp_response_active_task(mem, key)
            return None
    except Exception:
        return task_id

    return task_id


def save_parsed_rfp_response_document(memory: BaseMemory, filename: str, side: str, rfp_text: str, case_number: str) -> list[str]:
    logger.info("save_parsed_rfp_response_document case_number=%s", case_number)
    memory['inputs/uploaded_filename'] = filename
    memory['inputs/case_number'] = case_number

    # save cover_page_text and body_text separately
    f_type = filename.rsplit('.', 1)[1].lower()
    if f_type == 'pdf': # pdf split by special token (<CoverPage>) (marks first page or part/whole of 2nd page)
        coverpage_splits = rfp_text.split("<CoverPage>")
        logger.info(
            "save_parsed_rfp_response_document coverpage_splits=%s",
            len(coverpage_splits),
        )
        # what if length of coverpage_splits is not 2?
        if len(coverpage_splits) == 2:
            first_page_text = coverpage_splits[0].replace('\r\n', '\n').replace('\r', '\n') # # Normalize all line endings
            complaint_body_text = coverpage_splits[1]
            memory['cover_text'] = first_page_text
            text_by_lines = complaint_body_text.splitlines()  # split on \r \n
            logger.info("save_parsed_rfp_response_document pdf_lines=%s", len(text_by_lines))
            memory['body_text'] = json.dumps(text_by_lines)
        else:
            logger.info(
                "save_parsed_rfp_response_document using_coverpage_fallback=true"
            )
            memory['cover_text'] = '\n'.join(rfp_text.splitlines()[:LINES_PER_CHUNK_PDF])
            text_by_lines = rfp_text.splitlines()  # split on \r \n
            logger.info("save_parsed_rfp_response_document pdf_lines=%s", len(text_by_lines))
            memory['body_text'] = json.dumps(text_by_lines)

    else:
        cover_page_text = ' '.join(rfp_text.split('\n')[:LINES_PER_CHUNK_DOCX]).replace('\r\n', '\n').replace('\r', '\n')
        logger.info("save_parsed_rfp_response_document cover_page_text=\n%s", cover_page_text)
        memory['cover_text'] = cover_page_text
        # for word document, save the whole doc  ('<CoverPage>' has not applied for word document)
        text_by_lines = rfp_text.splitlines()
        logger.info("save_parsed_rfp_response_document word_lines=%s", len(text_by_lines))
        memory['body_text'] = json.dumps(text_by_lines)

    memory['inputs/side'] = side
    return text_by_lines


def _load_saved_body_lines(memory: BaseMemory) -> list[str]:
    raw_text = memory.get('body_text', '')
    if not raw_text:
        return []

    try:
        text_by_lines = json.loads(raw_text)
        if isinstance(text_by_lines, list):
            return text_by_lines
    except Exception:
        pass

    return str(raw_text).splitlines()

def draft_full_rfp_response(mem: BaseMemory, fn: str) -> str:
    """Drafts an RFP from available elements in memory.
    """
    # create formatted pdf
    gen_pdf_status = create_rfp_response_pdf(fn=fn,
                                        plaintiff=mem.get('inputs/plaintiff', "specify plaintiff"),
                                        defendant=mem.get('inputs/defendant', "specify defendant"),
                                        attorneys_contact_info=mem.user.contact_info or "No Attorneys_contact_info",
                                        venue=mem.get('inputs/venue', "Venue not yet specified"),
                                        introduction=mem.get('drafted_sections/intro', ''),
                                        general_objections=mem.get('drafted_sections/gen_obj', ''),
                                        rfp_response=mem.get('drafted_sections/rfp_response_text', ''),
                                        is_defendant=mem['inputs/side'] == "defendant",
                                        )
    return gen_pdf_status



def draft_full_rfp_response_word(mem: BaseMemory, fn: str) -> str:
    """Drafts an RFP from available elements in memory.
    """
    # create formatted pdf
    gen_word_status = create_rfp_response_word(fn=fn,
                                        plaintiff=mem.get('inputs/plaintiff', "specify plaintiff"),
                                        defendant=mem.get('inputs/defendant', "specify defendant"),
                                        attorneys_contact_info=mem.user.contact_info or "No Attorneys_contact_info",
                                        venue=mem.get('inputs/venue', "Venue not yet specified"),
                                        introduction=mem.get('drafted_sections/intro', ''),
                                        general_objections=mem.get('drafted_sections/gen_obj', ''),
                                        rfp_response=mem.get('drafted_sections/rfp_response_text', ''),
                                        is_defendant=mem['inputs/side'] == "defendant",
                                        )
    return gen_word_status



@shared_task(bind=True, ignore_result=False)
def rfp_response_parse_document_task(self, filename: str, draftId: str, side: str) -> dict:
    doc_id = Drafts.resolve_id(draftId)
    task_id = _current_task_id(self)
    memory = DBMemory(db=db, doc_id=doc_id)

    try:
        from backend.util.account_access import chat_account_id, require_enabled_account
        require_enabled_account(chat_account_id(doc_id))
        _rfp_response_progress(self, 'Parsing RFP document...', 15)
        rfp_text, case_number, _ = get_complaint_text_from_file(os.path.join(STATIC_FOLDER, filename))
        ocr_warning = get_last_ocr_warning()
        if ocr_warning:
            _rfp_response_progress(self, "Some scanned pages could not be read. Your document is still processed.", 60, {
                'status': 'ocr_warning',
                'ocr_warning': True,
                'ocr_warning_message': "Some scanned pages could not be read. Your document is still processed. Press OK to continue.",
            })
        _rfp_response_progress(self, 'Saving parsed RFP text...', 80)
        require_enabled_account(chat_account_id(doc_id))
        save_parsed_rfp_response_document(memory, filename, side, rfp_text, case_number)
        _rfp_response_progress(self, 'RFP parsed successfully.', 100)
        return {
            'message': 'RFP parsed successfully',
            'uploaded_filename': filename,
            'has_parsed_document': True,
            'ocr_warning': ocr_warning,
            'ocr_warning_message': "Some scanned pages could not be read. Your document is still processed. Press OK to continue.",
            'file_name': f'{API_URL}static/{filename}',
        }
    except Exception as exc:
        return {
            'message': 'error',
            'error_message': f'Error parsing RFP document: {str(exc)}',
        }
    finally:
        clear_rfp_response_active_task(memory, RFP_RESPONSE_ACTIVE_PARSE_TASK_KEY, expected_task_id=task_id)


@guided_step_operation(
    DOC_TYPE,
    0,
    lambda memory, draftId, task=None, ocr_warning=False: draftId,
)
def generate_rfp_response_setup_from_memory(
    memory: BaseMemory,
    draftId: str,
    task=None,
    ocr_warning: bool = False,
) -> dict:
    """
    Generate definition/instruction objections, intro, and general objections
    from a previously parsed RFP document.
    """
    text_by_lines = _load_saved_body_lines(memory)
    cover_text = memory.get('cover_text', '')
    if not text_by_lines or not cover_text:
        return {
            'message': 'error',
            'error_message': 'Please upload and parse RFPs before continuing.',
        }

    filename = memory.get('inputs/uploaded_filename', '')
    side = memory.get('inputs/side', 'defendant')
    logger.info("generate_rfp_response_setup_from_memory first_20_lines=\n%s", text_by_lines[:20])
    # ===== Extract plaintiff, defendant, venue from cover page text and save to memory ======
    _rfp_response_progress(task, 'Identifying parties and venue...', 15)
    plaintiff_l, defendant_l, venue = extract_parties_venue(
        cover_text,
        model=EXTRACTION_MODEL,
        reasoning_effort=EXTRACTION_REASONING_EFFORT,
    )
    logger.info("generate_rfp_response_setup_from_memory plaintiff=%s", plaintiff_l)
    logger.info("generate_rfp_response_setup_from_memory plaintiff_count=%s", len(plaintiff_l))
    logger.info("generate_rfp_response_setup_from_memory defendant=%s", defendant_l)
    logger.info("generate_rfp_response_setup_from_memory defendant_count=%s", len(defendant_l))
    logger.info("generate_rfp_response_setup_from_memory venue=%s", venue)

    memory['inputs/plaintiff'] = ";".join([remove_biz_type(each) for each in plaintiff_l])
    logger.info(
        "generate_rfp_response_setup_from_memory saved_plaintiff=%s",
        memory['inputs/plaintiff'],
    )
    memory['inputs/defendant'] = ";".join([remove_biz_type(each) for each in defendant_l])    # might be over-written in state1
    logger.info(
        "generate_rfp_response_setup_from_memory saved_defendant=%s",
        memory['inputs/defendant'],
    )
    memory['inputs/venue'] = venue
    memory['inputs/side'] = side

    side_name = memory['inputs/plaintiff'] if side == "plaintiff" else memory['inputs/defendant']

    # make llm call to extract REQUEST items, and use it to split the raw text into half, send the first half for Definitions and Instructions extraction
    requests_start_end_i = get_rfp_section_text(text_by_lines, section="specific requests for production of Documents")
    logger.info(
        "generate_rfp_response_setup_from_memory requests_start_end_i=%s",
        requests_start_end_i,
    )
    memory['inputs/requests_start_end_i'] = ','.join(map(str, requests_start_end_i))  # Convert list of integers to a comma-separated string
    
    requests_textlines = text_by_lines[requests_start_end_i[0]:requests_start_end_i[-1]]
    textlines_before_requests = text_by_lines[:requests_start_end_i[0]]
    logger.info(
        "generate_rfp_response_setup_from_memory lines_before_requests=%s",
        len(textlines_before_requests),
    )

    definitions_l = []  # a list of dict [{'item': '...', 'objection': '...'}, ...]
    instructions_l = []  # a list of dict [{'item': '....', 'objection': '...'}, ...]

    # if if_any_str.strip().lower() == 'yes':
    _rfp_response_progress(task, 'Generating definition and instruction objections...', 45)
    comand = open("backend/ai/prompts/rfp_response/DEF_INSTRUCT_OBJECTION").read()
    for section_name in ['definition', 'instruction']:
        logger.info("DEF_INSTRUCT_OBJECTION section_name=%s", section_name)
        filled_in_prompt = comand.format(
            section=section_name,
            side_name = side_name,
            context='\n'.join(textlines_before_requests)
            )
        logger.info("DEF_INSTRUCT_OBJECTION prompt=\n%s", filled_in_prompt)
        output = chat_completion_j(
            filled_in_prompt,
            model=WRITING_MODEL,
            reasoning_effort=WRITING_REASONING_EFFORT,
        )
        llm_list = extract_from_json(f"{section_name}s", output)
        logger.info("DEF_INSTRUCT_OBJECTION llm_list=%s", llm_list)
        logger.info("DEF_INSTRUCT_OBJECTION %s_list_len=%s", section_name, len(llm_list))
        if section_name == 'definition':
            definitions_l = llm_list
        else:
            instructions_l = llm_list
    memory['drafted_sections/definitions'] = json.dumps(definitions_l)
    memory['drafted_sections/instructions'] = json.dumps(instructions_l)

    # build general objection text immediately so the frontend can display it in
    # the next step without an additional round trip
    def_objections_text = ""       
    for each_def in definitions_l:
        if each_def.get('objection'):
            def_objections_text += each_def['objection'] + "\n"

    instruct_objections_text = ""
    for each_inst in instructions_l:
        if each_inst.get('objection'):
            logger.info("instruction_objection=%s", each_inst['objection'])
            instruct_objections_text += each_inst['objection'] + "\n"

    side_name = memory['inputs/plaintiff'] if memory['inputs/side'] == "plaintiff" else memory['inputs/defendant']
    if len(side_name.split(';')) < 2:
        general_obj_template = open('backend/ai/prompts/rfp_response/GEN_TEMPLATE_S').read()
        
    else:  # MULTIPLE SIDE PARTIES
        general_obj_template = open('backend/ai/prompts/rfp_response/GEN_TEMPLATE_P').read()
    
    general_obj_template_filled = general_obj_template.format(party_name=side_name)

    if memory['inputs/side'] == "plaintiff":
        if len(side_name.split(';')) > 1:  # plural plaintiff
            intro_text = f"Plaintiffs {memory['inputs/plaintiff']} hereby object and respond to the First Set of Requests for Production of Documents served by Defendant {memory['inputs/defendant']}."
        else: # single plaintiff
            intro_text = f"Plaintiff {memory['inputs/plaintiff']} hereby objects and responds to the First Set of Requests for Production of Documents served by Defendant {memory['inputs/defendant']}."
    else:
        if len(side_name.split(';')) > 1: # plural defendant
            intro_text = f"Defendants {memory['inputs/defendant']} hereby object and respond to the First Set of Requests for Production of Documents served by Plaintiff {memory['inputs/plaintiff']}."
        else:  # single defendant
            intro_text = f"Defendant {memory['inputs/defendant']} hereby objects and responds to the First Set of Requests for Production of Documents served by Plaintiff {memory['inputs/plaintiff']}."
 
    memory['analysis/intro'] = intro_text
    memory['analysis/general'] = general_obj_template_filled
    memory['analysis/definition_objections'] = def_objections_text
    memory['analysis/instruction_objections'] = instruct_objections_text
    _rfp_response_progress(task, 'RFP response setup ready.', 100)

    return {
            "message": "Form processed successfully",
            "definition_objections": def_objections_text,
            "instruction_objections": instruct_objections_text,
            "intro": intro_text,
            "general": general_obj_template_filled,
            "ocr_warning": ocr_warning,
            "ocr_warning_message": "Some scanned pages could not be read. Your document is still processed. Press OK to continue.",
            'file_name': f'{API_URL}static/{filename}'}


@shared_task(bind=True, ignore_result=False)
def state0(self, filename: str, draftId: str, side: str) -> dict:
    """
    Legacy combined step-0 task: parse the uploaded RFP and run response setup.
    New UI uses rfp_response_parse_document_task followed by rfp_response_continue_task.
    """
    memory = DBMemory(db=db, doc_id=Drafts.resolve_id(draftId))
    from backend.util.account_access import chat_account_id, require_enabled_account
    require_enabled_account(chat_account_id(draftId))
    _rfp_response_progress(self, 'Parsing RFP document...', 15)
    rfp_text, case_number, _ = get_complaint_text_from_file(os.path.join(STATIC_FOLDER, filename))
    ocr_warning = get_last_ocr_warning()
    if ocr_warning:
        _rfp_response_progress(self, "Some scanned pages could not be read. Your document is still processed.", 60, {
            'status': 'ocr_warning',
            'ocr_warning': True,
            'ocr_warning_message': "Some scanned pages could not be read. Your document is still processed. Press OK to continue.",
        })
    _rfp_response_progress(self, 'Saving parsed RFP text...', 80)
    require_enabled_account(chat_account_id(draftId))
    save_parsed_rfp_response_document(memory, filename, side, rfp_text, case_number)
    return generate_rfp_response_setup_from_memory(memory, draftId, task=self, ocr_warning=ocr_warning)


@shared_task(bind=True, ignore_result=False)
def rfp_response_continue_task(self, data: dict) -> dict:
    draftId = data['draftId']
    doc_id = Drafts.resolve_id(draftId)
    task_id = _current_task_id(self)
    memory = DBMemory(db=db, doc_id=doc_id)

    try:
        return generate_rfp_response_setup_from_memory(memory, draftId, task=self)
    except Exception as exc:
        return {
            'message': 'error',
            'error_message': f'Error preparing RFP response draft: {str(exc)}',
        }
    finally:
        clear_rfp_response_active_task(memory, RFP_RESPONSE_ACTIVE_CONTINUE_TASK_KEY, expected_task_id=task_id)


def handle_state0(request):
    """State 0 is to simply upload a file, and generate fa summary and coa summary."""
    filename, error_message = handle_file_upload(request, STATIC_FOLDER)
    if error_message:
        logger.warning("handle_state0 upload_error=%s", error_message)
        return {'message': error_message}, 401

    doc_id = Drafts.resolve_id(request.form['draftId'])
    memory = DBMemory(db=db, doc_id=doc_id)
    clear_memory_keys(memory, RFP_RESPONSE_RESET_KEYS)
    memory['inputs/uploaded_filename'] = filename

    # Call state0 as a Celery task to process the file
    result = state0.delay(filename=filename, draftId=request.form['draftId'], side=request.form['side'])
    return {"result_id": result.id}, 200  # Return the result ID of the task


def handle_upload_rfp(request):
    """Upload and parse RFPs without running response setup."""
    doc_id = Drafts.resolve_id(request.form['draftId'])
    active_parse_task_id = get_active_rfp_response_task_id(doc_id, RFP_RESPONSE_ACTIVE_PARSE_TASK_KEY)
    if active_parse_task_id:
        return {'message': 'RFP parsing is already running.', 'result_id': active_parse_task_id}, 409

    active_continue_task_id = get_active_rfp_response_task_id(doc_id, RFP_RESPONSE_ACTIVE_CONTINUE_TASK_KEY)
    if active_continue_task_id:
        return {'message': 'RFP response setup is already running.', 'result_id': active_continue_task_id}, 409

    filename, error_message = handle_file_upload(request, STATIC_FOLDER)
    if error_message:
        logger.warning("handle_upload_rfp upload_error=%s", error_message)
        return {'message': error_message}, 401

    memory = DBMemory(db=db, doc_id=doc_id)
    clear_memory_keys(memory, RFP_RESPONSE_RESET_KEYS)
    memory['inputs/uploaded_filename'] = filename

    result = rfp_response_parse_document_task.delay(
        filename=filename,
        draftId=request.form['draftId'],
        side=request.form.get('side', 'defendant'),
    )
    memory[RFP_RESPONSE_ACTIVE_PARSE_TASK_KEY] = result.id
    return {'result_id': result.id, 'uploaded_filename': filename}, 200


def handle_continue_step0(request):
    """Run RFP response setup from previously parsed RFP text."""
    data = request.get_json(silent=True) or request.form
    draft_id = data.get('draftId')
    doc_id = Drafts.resolve_id(draft_id)
    memory = DBMemory(db=db, doc_id=doc_id)

    active_parse_task_id = get_active_rfp_response_task_id(doc_id, RFP_RESPONSE_ACTIVE_PARSE_TASK_KEY)
    if active_parse_task_id:
        return {'message': 'RFP parsing is still running.', 'result_id': active_parse_task_id}, 400

    if not memory.get('body_text', '') or not memory.get('cover_text', ''):
        return {'message': 'Please upload and parse RFPs before continuing.'}, 400

    active_continue_task_id = get_active_rfp_response_task_id(doc_id, RFP_RESPONSE_ACTIVE_CONTINUE_TASK_KEY)
    if active_continue_task_id:
        return {'result_id': active_continue_task_id, 'already_running': True}, 200

    clear_memory_keys(memory, RFP_RESPONSE_GENERATED_RESET_KEYS)
    result = rfp_response_continue_task.delay({'draftId': draft_id})
    memory[RFP_RESPONSE_ACTIVE_CONTINUE_TASK_KEY] = result.id
    return {'result_id': result.id}, 200


handle_rfp_response_upload_rfp = handle_upload_rfp
handle_rfp_response_continue_step0 = handle_continue_step0


@shared_task(ignore_result=False, soft_time_limit=WRITING_SOFT_TIME_LIMIT, time_limit=WRITING_HARD_TIME_LIMIT)
@guided_step_operation(DOC_TYPE, 1, lambda data: data['draftId'])
def state1(data:dict) -> dict:
    """
    make llm calls for specific request objection
    """
    # Fetch database for draftId
    draftId = Drafts.resolve_id(data['draftId'])
    filename = Drafts.pdf_filename(data['draftId'])
    memory = DBMemory(db=db, doc_id=Drafts.resolve_id(draftId))
    for k, v in data.items():
        logger.info("state1 key=%s value=%s", k, v)

    # save user modified entries on previous step
    memory['drafted_sections/intro'] = data['introBody']
    memory['drafted_sections/gen_obj'] = '\n'.join([data['generalBody'], data['objectionBody']])

    # Do not generate a draft PDF in state1; we only prepare data for selections
    

    text_by_lines = json.loads(memory['body_text'])
    logger.info("state1 retrieved_text_by_lines=%s", len(text_by_lines))
    requests_start_end_i_str = memory['inputs/requests_start_end_i']  # Load the string from memory
    requests_start_end_i = list(map(int, requests_start_end_i_str.split(',')))  # Convert the string back to a list of integers
    requests_textlines = text_by_lines[requests_start_end_i[0]:requests_start_end_i[-1]]
    logger.info("state1 request_textlines_start=\n%s", requests_textlines[:20])
    logger.info("state1 request_textlines_end=\n%s", requests_textlines[-20:])


    side = memory['inputs/side']
    side_b = invert_side(side)
    side_name = memory['inputs/plaintiff'] if side == "plaintiff" else memory['inputs/defendant']
    side_b_name = memory['inputs/defendant'] if side == "plaintiff" else memory['inputs/plaintiff']

    # ===================== EXTRACT REQUESTS AS A LIST ==========================================
    # make llm call to extract requests textlines into a list, identified with id
    # new error: {"error":"The requested output (a complete JSON list of 146 requests) is too large ....
    comand = open("backend/ai/prompts/rfp_response/LIST_REQUESTS").read()
    filled_in_prompt = comand.format(context='\n'.join(requests_textlines))
    logger.info(
        "LIST_REQUESTS prompt_tokens=%s prompt=\n%s",
        num_tokens_from_string(filled_in_prompt),
        filled_in_prompt,
    )
    # output = chat_completion_j(filled_in_prompt)
    output = chat_completion_j_list_requests(
        filled_in_prompt,
        model=EXTRACTION_MODEL,
        reasoning_effort=EXTRACTION_REASONING_EFFORT,
    )
    requests_l = extract_from_json(expected_key="requests", data_from_llm=output)
    for req in requests_l:
        req['request'] = req['request'].replace('\n', ' ') + '\n'
        req['request_id'] = req['request_id'].replace('\n', ' ') + '\n'
    logger.info("LIST_REQUESTS request_items_len=%s", len(requests_l))

    # # Force empty to surface retry/error flow (for testing)
    # requests_l = []
    
    # ============  SEND REQUEST ID AND TEXT AS GROUPS TO LLM FOR RESPONSE ===========
    # Frontend needs: const [responses, setResponses] = useState([[{'request_id': '1', 'request': 'Documents ....', 'objection': '...', 'objectionable': 'yes', 'reason': 'The request for documents concerning'}]]);

     # Add more fields with default values to each dictionary in the list
    for req in requests_l:
        req.setdefault('objection', '')
        req.setdefault('reason', '')

    retry_key = 'rfp_retry_attempts'
    retry_count = int(memory.get(retry_key, "0") or 0)

    # Handle empty extraction from LLM with up to 1 user-visible retry
    if not requests_l:
        if retry_count < 1:
            memory[retry_key] = str(retry_count + 1)
            return {
                "message": "retry",
                "retry": True,
                "retry_message": "Something went wrong when generating responses, please try again.",
                "retries_left": 1 - (retry_count + 1),
                "file_name": f'{API_URL}static/{filename}',
            }
        else:
            memory[retry_key] = str(retry_count + 1)
            return {
                "message": "error",
                "retry": False,
                "error_message": "There was an error. Please contact customer support at support@lextext.ai or call 415-617-9439.",
                "file_name": f'{API_URL}static/{filename}',
            }
    else:
        # reset retry counter on success
        memory[retry_key] = "0"

    if requests_l:
        comand_prompt = open('backend/ai/prompts/rfp_response/SUGGEST_RESPONSE').read()
        venue = memory.get('inputs/venue', '') or ''
        # here we could extract complaint summary if available in the database, or generate one if user provide complaint
        # ====
        if side == "plaintiff":
            side_dep_instr = "The plaintiff's overall strategy in objecting to RFPs from the defendant is to protect their legal and business interests by focusing on the relevance and proportionality of the discovery requests, safeguarding privileged and sensitive information, minimizing burdens and costs, and maintaining control over the discovery process. These objections should be specific, legally sound, and aimed at keeping the discovery process fair and focused on the core issues of the case."
        else:
            side_dep_instr = "The defendant's overall strategy when objecting to RFPs from the plaintiff is to protect their legal and business interests by narrowing the scope of discovery, minimizing burdens, and safeguarding privileged or sensitive information. The objections should be precise, grounded in legal principles, and tailored to the specific circumstances of the case to ensure that discovery remains fair and focused on the relevant issues."

        n_requests = len(requests_l)
        number_of_chunks = -(-n_requests// WRITING_BATCH_SIZE)  # ceiling division
        logger.info("SUGGEST_RESPONSE number_of_chunks=%s", number_of_chunks)

        processes = []
        # for i in range(2):  #  for testing
        for i in range(number_of_chunks):
            start_index = i * WRITING_BATCH_SIZE
            end_index = min(start_index + WRITING_BATCH_SIZE, n_requests)
            logger.info(
                "SUGGEST_RESPONSE chunk=%s start_index=%s end_index=%s",
                i,
                start_index,
                end_index,
            )
            batch_requests_str = "\n".join([f"index_id: {j},   request: {requests_l[j]['request_id']}\t{requests_l[j]['request']}\n" for j in range(start_index, end_index)])

            filled_in_prompt = comand_prompt.format(
                side=side, side_b=side_b, side_name=side_name, side_b_name=side_b_name,
                side_dep_inst=side_dep_instr,
                venue=venue,
                proportionality_guidance=_rfp_proportionality_guidance(venue),
                summary="",
                requests=batch_requests_str
            )
            if i == 0:
                logger.info("SUGGEST_RESPONSE first_prompt=%s", filled_in_prompt)
            processes.append(filled_in_prompt)

        with concurrent.futures.ThreadPoolExecutor(max_workers=WRITING_MAX_WORKERS) as pool:
            future_map = {
                submit_with_context(
                    pool, chat_completion_parse, prompt, RFPObjectionBatch,
                    model=WRITING_MODEL, reasoning_effort=WRITING_REASONING_EFFORT,
                ): prompt for prompt in processes}
            logger.info("SUGGEST_RESPONSE created_threads=%s", len(pool._threads))
            for fut in concurrent.futures.as_completed(future_map):
                task = future_map[fut]
                try:
                    returned = fut.result()
                    objections_l = returned.objections if returned is not None else []
                except SoftTimeLimitExceeded:
                    raise
                except Exception as exc:
                    logger.warning("SUGGEST_RESPONSE task failed: %s", exc)
                    objections_l = []
                
                for each_ob in objections_l:
                    index_id = each_ob.index_id
                    if not 0 <= index_id < len(requests_l):
                        continue
                    requests_l[index_id]['objection'] = each_ob.objection
                    requests_l[index_id]['reason'] = each_ob.reason

    memory['drafted_sections/requests'] = json.dumps(requests_l)

    # Do not return a new file; frontend will keep showing the uploaded file from state0
    return {"message": "Form processed successfully",
            "rfp_response": requests_l}


def handle_state1(request):
    ''' state1 is to generate specific rfp objections or responses '''
    # save file
    if 'file' in request.files:
        file = request.files['file']
        filename = secure_filename(file.filename)
        if allowed_filetypes(filename=filename):
            file.save(os.path.join(STATIC_FOLDER, filename))
        upload_file_status = "Form processed successfully"
    else:
        upload_file_status = "No file submitted"
        filename = None
    result = state1.delay(request.form)
    return {"result_id": result.id}, 200


def _rfp_response_state1_prerequisite_error(data: dict) -> dict | None:
    draft_id = Drafts.resolve_id(data['draftId'])
    memory = DBMemory(db=db, doc_id=draft_id)
    return guided_prerequisite_error(memory, RFP_RESPONSE_STATE1_REQUIRED_KEYS, 'RFP response objections')


@shared_task(ignore_result=False, soft_time_limit=WRITING_SOFT_TIME_LIMIT, time_limit=WRITING_HARD_TIME_LIMIT)
@guided_step_operation(DOC_TYPE, 2, lambda data: data['draftId'])
def state2(data: dict) -> dict:
    """State 2 is to add specific rfp responses to the draft"""
    draftId = Drafts.resolve_id(data['draftId'])

    # get definition and instruction objections from database, or state0 results
    memory = DBMemory(db=db, doc_id=draftId)
    side = memory['inputs/side']
    side_b = invert_side(side)
    side_name = memory['inputs/plaintiff'] if side == "plaintiff" else memory['inputs/defendant']
    side_b_name = memory['inputs/defendant'] if side == "plaintiff" else memory['inputs/plaintiff']

    # requests_by_id = json.loads(memory['drafted_sections/requests'])
    requests_l = json.loads(memory['drafted_sections/requests'])
    logger.info("state2 retrieved_requests_len=%s", len(requests_l))

    user_selections = {}
    user_customs = {}
    for k, v in data.items():  
        # req-142:Object/Full Production/No Documents/Custom
        # objection-142: 'value'
        # custom-141: 'value'
        logger.info("state2 data key=%s value=%s", k, v)
        if '-' in k:
            item, idx = k.split('-')
            if item == 'req':
                # user_selections[int(idx)] = v
                if v != 'Custom':
                    requests_l[int(idx)]['selection'] = v
            elif item == 'custom':
                requests_l[int(idx)]['selection'] = v

    generate_response_command = open("backend/ai/prompts/rfp_response/REWRITE_RESPONSE").read()
    n_requests = len(requests_l)
    n_batches = -(-n_requests//WRITING_BATCH_SIZE)   # ceiling
    tasks = []
    for k in range(n_batches):
        batch_requests = requests_l[WRITING_BATCH_SIZE * k: WRITING_BATCH_SIZE * k + WRITING_BATCH_SIZE]
        batch_data_str = ""
        if k == n_batches-1:
            for i in range(min(WRITING_BATCH_SIZE, n_requests - k * WRITING_BATCH_SIZE)):
                index = WRITING_BATCH_SIZE * k + i
                request = requests_l[index]
                batch_data_str += f"index_id:{index}, Request: {request['request']}\n Objection: {request['objection']}\n Lawyer's response:{request['selection']}\n\n"

        else:   
            for i in range(WRITING_BATCH_SIZE):
                index = WRITING_BATCH_SIZE * k + i
                request = requests_l[index]
                batch_data_str += f"index_id:{index}, Request: {request['request']}\n Objection: {request['objection']}\n Lawyer's response:{request['selection']}\n\n"

        if k == 0:
            logger.info("REWRITE_RESPONSE first_batch_data=\n%s", batch_data_str)
        if k == n_batches-1:
            logger.info("REWRITE_RESPONSE last_batch_data=\n%s", batch_data_str)

        filled_command = generate_response_command.format(
                side=side, side_b=side_b, side_name=side_name, side_b_name=side_b_name, 
                rfp_responses=batch_data_str
                )
        tasks.append((k, filled_command))
    
    returned_indexes = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=WRITING_MAX_WORKERS) as pool:
        future_map = {
            submit_with_context(
                pool, chat_completion_parse, t[1], RFPResponseBatch,
                model=WRITING_MODEL, reasoning_effort=WRITING_REASONING_EFFORT,
            ): t for t in tasks}
        logger.info("REWRITE_RESPONSE created_threads=%s", len(pool._threads))

        for fut in concurrent.futures.as_completed(future_map):
            task = future_map[fut]
            batch_i = task[0]
            try:
                returned = fut.result()
                responses_l = returned.responses if returned is not None else []
            except SoftTimeLimitExceeded:
                raise
            except Exception as exc:
                logger.warning("REWRITE_RESPONSE batch=%s failed: %s", batch_i, exc)
                responses_l = []
            
            for each_response in responses_l:
                index_id = each_response.index_id
                if not 0 <= index_id < len(requests_l):
                    continue
                returned_indexes.append(index_id)
                resp = each_response.response
                requests_l[index_id]['response'] = resp

    returned_indexes.sort()
    logger.info(
        "state2 returned_indexes_len=%s returned_indexes=%s",
        len(returned_indexes),
        returned_indexes,
    )

    # Construct response text
    rfp_response_text = "SPECIFIC OBJECTIONS AND RESPONSES\n"  # an empty string at the start

    for idx, request in enumerate(requests_l):
        response = request.get('response', "")
        if response:
            rid = request['request_id']
            # If there are no alphabetical characters in rid, prefix it
            if not any(ch.isalpha() for ch in rid):
                rid = f"Request No. {rid}"
            rfp_response_text += f"""{rid.upper()}\n{request['request']}\n
        RESPONSE\n{response}\n\n"""
            
    # print(f'rfp_response_text: {rfp_response_text}')
    memory['drafted_sections/rfp_response_text'] = rfp_response_text

    # update second half of document'
    filename = Drafts.pdf_filename(data['draftId'])
    memory['inputs/generated_pdf_filename'] = filename
    draft_full_rfp_response(memory, f"backend/static/{filename}")

    return {"file_name": f'{API_URL}static/{filename}'}


def handle_state2(request):
    result = state2.delay(request.form)
    return {"result_id": result.id}, 200


def route_rfp_response_logic(request):
    """Decides which step should be used and passes request to those steps."""

    # switch on appState in request
    data = request.form
    logger.info("route_rfp_response_logic data=%s", data)
    appState = int(data['appState'])

    if appState == 0:
        return handle_state0(request)
    elif appState == 1:
        prerequisite_error = _rfp_response_state1_prerequisite_error(data)
        if prerequisite_error:
            return prerequisite_error, 400
        return handle_state1(request)
    elif appState == 2:
        return handle_state2(request)
