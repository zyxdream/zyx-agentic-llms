from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from celery.result import AsyncResult
from werkzeug.utils import secure_filename
import logging
import os
from backend.constants import API_URL, STATIC_FOLDER
from backend.util.db import DBMemory, BaseMemory
from backend.util.data_tools import invert_side, get_complaint_text_from_file, remove_biz_type, handle_file_upload
from backend.util.guided_draft_reset import ROG_RESPONSE_RESET_KEYS, clear_memory_keys
from backend.util.guided_prerequisites import guided_prerequisite_error
from backend.ai.base import extract_title_parties_venue, chat_completion_parse, chat_completion
from backend.ai import base as ai_base
from backend.database import db
from backend.models import Drafts
from backend.pdf_creator import create_rog_response_pdf
from backend.word_creator import create_rog_response_word
from backend.util.analytics import guided_step_operation, submit_with_context
from backend.ai.complaint_parser import get_last_ocr_warning
import json
from time import sleep
import concurrent.futures
from backend.ai.base import num_tokens_from_string
from collections import OrderedDict
import unicodedata
from pydantic import BaseModel

logger = logging.getLogger(__name__)



DOC_TYPE = "ROG_RESPONSE"
DOWNLOAD_STEP = 3
EXTRACTION_MODEL = "gpt-6-luna"
EXTRACTION_EFFORT = "medium"
INTRO_MODEL = "gpt-6.1-sol"
WRITING_MODEL = ai_base.DEFAULT_MODEL
INTRO_EFFORT = "high"
WRITING_EFFORT = "high"
WRITING_BATCH_SIZE = 30
WRITING_MAX_WORKERS = 4
WRITING_SOFT_TIME_LIMIT = 1200
WRITING_HARD_TIME_LIMIT = 1260


class ROGIntroduction(BaseModel):
    introduction: str


class ROGInterrogatory(BaseModel):
    interrogatory_id: str
    interrogatory: str


class ROGInterrogatoryList(BaseModel):
    interrogatories: list[ROGInterrogatory]


class ROGSuggestion(BaseModel):
    index_id: int
    objection: str
    rationale: str
    tips: str


class ROGSuggestionBatch(BaseModel):
    objections: list[ROGSuggestion]


class ROGRewrite(BaseModel):
    index_id: int
    objection: str
    response: str


class ROGRewriteBatch(BaseModel):
    objections_and_responses: list[ROGRewrite]


def draft_full_rog_response(mem: BaseMemory, fn: str) -> str:
    gen_pdf_status = create_rog_response_pdf(
        fn=fn,
        plaintiff=mem.get('inputs/plaintiff', "specify plaintiff"),
        defendant=mem.get('inputs/defendant', "specify defendant"),
        attorneys_contact_info=mem.user.contact_info or "No Attorneys_contact_info",
        venue=mem.get('inputs/venue', "Venue not yet specified"),
        introduction=mem.get('drafted_sections/intro', ''),
        general_objections=mem.get('drafted_sections/gen_obj', ''),
        rog_response=mem.get('drafted_sections/rog_response_text', ''),
        is_defendant=mem['inputs/side'].lower().startswith("defendant"),
    )
    return gen_pdf_status


def draft_full_rog_response_word(mem: BaseMemory, fn: str) -> str:
    gen_word_status = create_rog_response_word(
        fn=fn,
        plaintiff=mem.get('inputs/plaintiff', "specify plaintiff"),
        defendant=mem.get('inputs/defendant', "specify defendant"),
        attorneys_contact_info=mem.user.contact_info or "No Attorneys_contact_info",
        venue=mem.get('inputs/venue', "Venue not yet specified"),
        introduction=mem.get('drafted_sections/intro', ''),
        general_objections=mem.get('drafted_sections/gen_obj', ''),
        rog_response=mem.get('drafted_sections/rog_response_text', ''),
        is_defendant=mem['inputs/side'].lower().startswith("defendant"),
    )
    return gen_word_status

LINES_PER_CHUNK_PDF = 40
LINES_PER_CHUNK_DOCX = 60
ROG_RESPONSE_ACTIVE_PARSE_TASK_KEY = 'rog_response/active_parse_task_id'
ROG_RESPONSE_ACTIVE_CONTINUE_TASK_KEY = 'rog_response/active_continue_task_id'
ROG_RESPONSE_GENERATED_RESET_KEYS = (
    'drafted_sections/intro',
    'drafted_sections/gen_obj',
    'drafted_sections/interrogatories',
    'drafted_sections/rog_response_text',
    'inputs/generated_pdf_filename',
    'inputs/plaintiff',
    'inputs/defendant',
    'inputs/title',
    'inputs/venue',
    'inputs/side',
    'inputs/is_federal',
    ROG_RESPONSE_ACTIVE_CONTINUE_TASK_KEY,
)
ROG_RESPONSE_STATE1_REQUIRED_KEYS = (
    'drafted_sections/interrogatories',
    'inputs/plaintiff',
    'inputs/defendant',
    'inputs/side',
)


def _delete_memory_key(mem: BaseMemory, key: str) -> None:
    try:
        del mem[key]
    except Exception:
        pass


def clear_rog_response_active_task(mem: BaseMemory, key: str, expected_task_id: str | None = None) -> None:
    current_task_id = mem.get(key, '') or ''
    if expected_task_id and current_task_id and current_task_id != expected_task_id:
        return
    _delete_memory_key(mem, key)


def _current_task_id(task) -> str | None:
    return getattr(getattr(task, 'request', None), 'id', None)


def _rog_response_progress(task, message: str, progress: int, extra_meta: dict | None = None) -> None:
    if task is None:
        return
    meta = {'message': message, 'progress': progress}
    if extra_meta:
        meta.update(extra_meta)
    try:
        task.update_state(state='PROGRESS', meta=meta)
    except Exception:
        pass


def get_active_rog_response_task_id(doc_id: int, key: str) -> str | None:
    mem = DBMemory(db=db, doc_id=doc_id)
    task_id = (mem.get(key, '') or '').strip()
    if not task_id:
        return None

    try:
        result = AsyncResult(task_id)
        if result.ready():
            clear_rog_response_active_task(mem, key)
            return None
    except Exception:
        return task_id

    return task_id


def save_parsed_rog_response_document(memory: BaseMemory, filename: str, side: str, rog_text: str, case_number: str) -> list[str]:
    memory['inputs/uploaded_filename'] = filename
    memory['inputs/case_number'] = case_number
    memory['inputs/side_selection'] = side.lower()

    f_type = filename.rsplit('.', 1)[1].lower()
    if f_type == 'pdf':
        coverpage_splits = rog_text.split("<CoverPage>")
        logger.info("save_parsed_rog_response_document coverpage_splits=%s", len(coverpage_splits))
        # what if length of coverpage_splits is not 2?
        if len(coverpage_splits) == 2:
            first_page_text = coverpage_splits[0].replace('\r\n', '\n').replace('\r', '\n') # # Normalize all line endings
            complaint_body_text = coverpage_splits[1]
            memory['cover_text'] = first_page_text
            text_by_lines = complaint_body_text.splitlines()  # split on \r \n
            logger.info("save_parsed_rog_response_document pdf_lines=%s", len(text_by_lines))
            memory['body_text'] = json.dumps(text_by_lines)
        else:
            logger.info("save_parsed_rog_response_document using_coverpage_fallback=true")
            memory['cover_text'] = '\n'.join(rog_text.splitlines()[:LINES_PER_CHUNK_PDF])
            text_by_lines = rog_text.splitlines()  # split on \r \n
            logger.info("save_parsed_rog_response_document pdf_lines=%s", len(text_by_lines))
            memory['body_text'] = json.dumps(text_by_lines)
    else:
        cover_page_text = ' '.join(rog_text.split('\n')[:LINES_PER_CHUNK_DOCX]).replace('\r\n', '\n').replace('\r', '\n')
        memory['cover_text'] = cover_page_text
        text_by_lines = rog_text.splitlines()
        memory['body_text'] = json.dumps(text_by_lines)

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
@shared_task(bind=True, ignore_result=False)
def rog_response_parse_document_task(self, filename: str, draftId: str, side: str) -> dict:
    doc_id = Drafts.resolve_id(draftId)
    task_id = _current_task_id(self)
    memory = DBMemory(db=db, doc_id=doc_id)

    try:
        from backend.util.account_access import chat_account_id, require_enabled_account
        require_enabled_account(chat_account_id(doc_id))
        _rog_response_progress(self, 'Parsing interrogatories document...', 15)
        rog_text, case_number, _ = get_complaint_text_from_file(os.path.join(STATIC_FOLDER, filename))
        ocr_warning = get_last_ocr_warning()
        if ocr_warning:
            _rog_response_progress(self, "Some scanned pages could not be read. Your document is still processed.", 60, {
                'status': 'ocr_warning',
                'ocr_warning': True,
                'ocr_warning_message': "Some scanned pages could not be read. Your document is still processed. Press OK to continue.",
            })
        _rog_response_progress(self, 'Saving parsed interrogatories text...', 80)
        require_enabled_account(chat_account_id(doc_id))
        save_parsed_rog_response_document(memory, filename, side, rog_text, case_number)
        _rog_response_progress(self, 'Interrogatories parsed successfully.', 100)
        return {
            'message': 'Interrogatories parsed successfully',
            'uploaded_filename': filename,
            'has_parsed_document': True,
            'ocr_warning': ocr_warning,
            'ocr_warning_message': "Some scanned pages could not be read. Your document is still processed. Press OK to continue.",
            'file_name': f'{API_URL}static/{filename}',
        }
    except Exception as exc:
        return {
            'message': 'error',
            'error_message': f'Error parsing interrogatories: {str(exc)}',
        }
    finally:
        clear_rog_response_active_task(memory, ROG_RESPONSE_ACTIVE_PARSE_TASK_KEY, expected_task_id=task_id)


@guided_step_operation(
    DOC_TYPE,
    0,
    lambda memory, draftId, task=None, ocr_warning=False: draftId,
)
def generate_rog_response_setup_from_memory(
    memory: BaseMemory,
    draftId: str,
    task=None,
    ocr_warning: bool = False,
) -> dict:
    """Generate ROG response setup from a previously parsed interrogatories document."""
    text_by_lines = _load_saved_body_lines(memory)
    cover_text = memory.get('cover_text', '')
    if not text_by_lines or not cover_text:
        return {
            'message': 'error',
            'error_message': 'Please upload and parse interrogatories before continuing.',
        }

    filename = memory.get('inputs/uploaded_filename', '')
    selected_side = memory.get('inputs/side_selection', memory.get('inputs/side', 'defendant')).lower()

    _rog_response_progress(task, 'Identifying parties and venue...', 15)
    plaintiff_l, defendant_l, title, venue = extract_title_parties_venue(
        cover_text, model=EXTRACTION_MODEL, reasoning_effort=EXTRACTION_EFFORT,
    )
    if not plaintiff_l or not defendant_l or not title.strip() or not venue.strip():
        raise ValueError("Could not extract parties, title, and venue from the uploaded document.")
    plaintiff_names = ";".join([remove_biz_type(each) for each in plaintiff_l])
    defendant_names = ";".join([remove_biz_type(each) for each in defendant_l])
    memory['inputs/plaintiff'] = plaintiff_names
    memory['inputs/defendant'] = defendant_names
    memory['inputs/title'] = title
    memory['inputs/venue'] = venue
    if selected_side == "plaintiff":
        side = "Plaintiffs" if len(plaintiff_l) > 1 else "Plaintiff"
    else:
        side = "Defendants" if len(defendant_l) >1 else "Defendant"
    memory['inputs/side'] = side

    # determine if this is a federal or state case
    comand_prompt = open('backend/ai/prompts/answer/DETERMINE_FEDERAL').read()
    filled_in_prompt = comand_prompt + venue
    llm_output = chat_completion(
        filled_in_prompt, model=EXTRACTION_MODEL, reasoning_effort=EXTRACTION_EFFORT,
    )
    is_federal = llm_output.strip().upper() if llm_output is not None else ""
    if is_federal not in {"FEDERAL", "STATE"}:
        raise ValueError("Could not determine whether the venue is federal or state court.")
    memory['inputs/is_federal'] = is_federal


    _rog_response_progress(task, 'Generating introduction and general objections...', 35)
    intro_command = open("backend/ai/prompts/rog_response/SUGGEST_INTRO").read()
    filled_in_prompt = intro_command.format(side = side, 
                                            plaintiff = plaintiff_names,
                                            defendant = defendant_names,
                                            title = title,
                                            venue = venue,
                                            is_federal = "YES" if is_federal == "FEDERAL" else "NO")
    logger.info("SUGGEST_INTRO prompt=%s", filled_in_prompt)
    intro_result = chat_completion_parse(
        filled_in_prompt, ROGIntroduction,
        model=INTRO_MODEL, reasoning_effort=INTRO_EFFORT,
    )
    if intro_result is None or not intro_result.introduction.strip():
        raise ValueError("Could not generate the ROG response introduction.")
    intro_text = intro_result.introduction
    memory['drafted_sections/intro'] = intro_text

    side_b_name = defendant_names.split(';')[0] if side.lower().startswith("plaintiff") else plaintiff_names.split(';')[0]
    gen_obj_text = open("backend/ai/prompts/rog_response/GEN_OBJ_TEMP").read().format(side=side, side_b_name=side_b_name)
    memory['drafted_sections/gen_obj'] = gen_obj_text
     
    # Extract interrogatories list using LLM
    _rog_response_progress(task, 'Listing interrogatories...', 70)
    comand = open("backend/ai/prompts/rog_response/LIST_INTERROGATORIES").read()
    filled_in_prompt = comand.format(context='\n'.join(text_by_lines))
    logger.info(
        "LIST_INTERROGATORIES prompt_tokens=%s",
        num_tokens_from_string(filled_in_prompt),
    )
    list_result = chat_completion_parse(
        filled_in_prompt, ROGInterrogatoryList,
        model=EXTRACTION_MODEL, reasoning_effort=EXTRACTION_EFFORT,
    )
    if list_result is None or not list_result.interrogatories:
        raise ValueError("Could not extract interrogatories from the uploaded document.")
    interrogatories_l = [item.model_dump() for item in list_result.interrogatories]
    for item in interrogatories_l:
        item['interrogatory'] = item['interrogatory'].replace('\n', ' ') + '\n'
        item['interrogatory_id'] = item['interrogatory_id'].replace('\n', ' ') + '\n'
        logger.info("LIST_INTERROGATORIES id=%s", item['interrogatory_id'])
        logger.info("LIST_INTERROGATORIES text=%s", item['interrogatory'])
        item.setdefault('objection', '')
        item.setdefault('response', '')
    memory['drafted_sections/interrogatories'] = json.dumps(interrogatories_l)
    _rog_response_progress(task, 'Interrogatory response setup ready.', 100)

    return {"message": "Form processed successfully",
            "intro": intro_text,
            "general": gen_obj_text,
            "interrogatories": interrogatories_l,
            "ocr_warning": ocr_warning,
            "ocr_warning_message": "Some scanned pages could not be read. Your document is still processed. Press OK to continue.",
            'file_name': f'{API_URL}static/{filename}'}


@shared_task(bind=True, ignore_result=False)
def state0(self, filename: str, draftId: str, side: str) -> dict:
    """Legacy combined task: parse uploaded interrogatories and run response setup."""
    memory = DBMemory(db=db, doc_id=Drafts.resolve_id(draftId))
    from backend.util.account_access import chat_account_id, require_enabled_account
    require_enabled_account(chat_account_id(draftId))
    _rog_response_progress(self, 'Parsing interrogatories document...', 15)
    rog_text, case_number, _ = get_complaint_text_from_file(os.path.join(STATIC_FOLDER, filename))
    ocr_warning = get_last_ocr_warning()
    if ocr_warning:
        _rog_response_progress(self, "Some scanned pages could not be read. Your document is still processed.", 60, {
            'status': 'ocr_warning',
            'ocr_warning': True,
            'ocr_warning_message': "Some scanned pages could not be read. Your document is still processed. Press OK to continue.",
        })
    _rog_response_progress(self, 'Saving parsed interrogatories text...', 80)
    require_enabled_account(chat_account_id(draftId))
    save_parsed_rog_response_document(memory, filename, side, rog_text, case_number)
    return generate_rog_response_setup_from_memory(memory, draftId, task=self, ocr_warning=ocr_warning)


@shared_task(bind=True, ignore_result=False)
def rog_response_continue_task(self, data: dict) -> dict:
    draftId = data['draftId']
    doc_id = Drafts.resolve_id(draftId)
    task_id = _current_task_id(self)
    memory = DBMemory(db=db, doc_id=doc_id)

    try:
        return generate_rog_response_setup_from_memory(memory, draftId, task=self)
    except Exception as exc:
        return {
            'message': 'error',
            'error_message': f'Error preparing ROG response draft: {str(exc)}',
        }
    finally:
        clear_rog_response_active_task(memory, ROG_RESPONSE_ACTIVE_CONTINUE_TASK_KEY, expected_task_id=task_id)


def handle_state0(request):
    filename, error_message = handle_file_upload(request, STATIC_FOLDER)
    if error_message:
        return {'message': error_message}, 401
    doc_id = Drafts.resolve_id(request.form['draftId'])
    memory = DBMemory(db=db, doc_id=doc_id)
    clear_memory_keys(memory, ROG_RESPONSE_RESET_KEYS)
    memory['inputs/uploaded_filename'] = filename
    result = state0.delay(filename=filename, draftId=request.form['draftId'], side=request.form['side'])
    return {"result_id": result.id}, 200


def handle_upload_rog(request):
    """Upload and parse interrogatories without running response setup."""
    doc_id = Drafts.resolve_id(request.form['draftId'])
    active_parse_task_id = get_active_rog_response_task_id(doc_id, ROG_RESPONSE_ACTIVE_PARSE_TASK_KEY)
    if active_parse_task_id:
        return {'message': 'Interrogatories parsing is already running.', 'result_id': active_parse_task_id}, 409

    active_continue_task_id = get_active_rog_response_task_id(doc_id, ROG_RESPONSE_ACTIVE_CONTINUE_TASK_KEY)
    if active_continue_task_id:
        return {'message': 'ROG response setup is already running.', 'result_id': active_continue_task_id}, 409

    filename, error_message = handle_file_upload(request, STATIC_FOLDER)
    if error_message:
        logger.warning("handle_upload_rog upload_error=%s", error_message)
        return {'message': error_message}, 401

    memory = DBMemory(db=db, doc_id=doc_id)
    clear_memory_keys(memory, ROG_RESPONSE_RESET_KEYS)
    memory['inputs/uploaded_filename'] = filename

    result = rog_response_parse_document_task.delay(
        filename=filename,
        draftId=request.form['draftId'],
        side=request.form.get('side', 'defendant'),
    )
    memory[ROG_RESPONSE_ACTIVE_PARSE_TASK_KEY] = result.id
    return {'result_id': result.id, 'uploaded_filename': filename}, 200


def handle_continue_step0(request):
    """Run ROG response setup from previously parsed interrogatories text."""
    data = request.get_json(silent=True) or request.form
    draft_id = data.get('draftId')
    doc_id = Drafts.resolve_id(draft_id)
    memory = DBMemory(db=db, doc_id=doc_id)

    active_parse_task_id = get_active_rog_response_task_id(doc_id, ROG_RESPONSE_ACTIVE_PARSE_TASK_KEY)
    if active_parse_task_id:
        return {'message': 'Interrogatories parsing is still running.', 'result_id': active_parse_task_id}, 400

    if not memory.get('body_text', '') or not memory.get('cover_text', ''):
        return {'message': 'Please upload and parse interrogatories before continuing.'}, 400

    active_continue_task_id = get_active_rog_response_task_id(doc_id, ROG_RESPONSE_ACTIVE_CONTINUE_TASK_KEY)
    if active_continue_task_id:
        return {'result_id': active_continue_task_id, 'already_running': True}, 200

    clear_memory_keys(memory, ROG_RESPONSE_GENERATED_RESET_KEYS)
    result = rog_response_continue_task.delay({'draftId': draft_id})
    memory[ROG_RESPONSE_ACTIVE_CONTINUE_TASK_KEY] = result.id
    return {'result_id': result.id}, 200


handle_rog_response_upload_rog = handle_upload_rog
handle_rog_response_continue_step0 = handle_continue_step0


@shared_task(ignore_result=False, soft_time_limit=WRITING_SOFT_TIME_LIMIT, time_limit=WRITING_HARD_TIME_LIMIT)
@guided_step_operation(DOC_TYPE, 1, lambda data: data['draftId'])
def state1(data:dict) -> dict:
    logger.info("state1 data")
    for k, v in data.items():
        logger.info("state1 %s: %s", k, v)
        
    draftId = Drafts.resolve_id(data['draftId'])
    # filename_out = f"{draftId}.pdf"
    memory = DBMemory(db=db, doc_id=draftId)

    # save intro and general objection again from the frontend
    memory['drafted_sections/intro'] = data['introBody']
    memory['drafted_sections/gen_obj'] = data['generalBody']

    # gen_pdf_status = create_rog_response_pdf(
    #     fn=f"backend/static/{filename_out}",
    #     plaintiff=memory.get('inputs/plaintiff', "specify plaintiff"),
    #     defendant=memory.get('inputs/defendant', "specify defendant"),
    #     attorneys_contact_info=memory.user.contact_info or "No Attorneys_contact_info",
    #     venue=memory.get('inputs/venue', "Venue not yet specified"),
    #     introduction=memory.get('drafted_sections/intro', ''),
    #     general_objections=memory.get('drafted_sections/gen_obj', ''),
    #     rog_response='',
    #     is_defendant=memory['inputs/side'] == "defendant",
    # )

    interrogatories_l = json.loads(memory['drafted_sections/interrogatories'])
    logger.info("state1 loaded_interrogatories_len=%s", len(interrogatories_l))
    side = memory['inputs/side']
    logger.info("state1 loaded_side=%s", side)
    side_b = invert_side(side)
    logger.info("state1 calculated_side_b=%s", side_b)
    side_name = memory['inputs/plaintiff'] if side.lower().startswith("plaintiff") else memory['inputs/defendant']
    side_b_name = memory['inputs/defendant'] if side.lower().startswith("plaintiff") else memory['inputs/plaintiff']

    if side.lower().startswith("plaintiff"):
        side_dep_instr = "The plaintiff's overall strategy in objecting to interrogatories from the defendant is to protect their legal and business interests by focusing on relevance and proportionality, safeguarding privileged and sensitive information, minimizing burdens and costs, and maintaining control over the discovery process."
    else:
        side_dep_instr = "The defendant's overall strategy when objecting to interrogatories from the plaintiff is to protect their legal and business interests by narrowing the scope of discovery, minimizing burdens, and safeguarding privileged or sensitive information."

    n_inter = len(interrogatories_l)
    number_of_chunks = -(-n_inter // WRITING_BATCH_SIZE)
    processes = []
    for i in range(number_of_chunks):
        start_index = i * WRITING_BATCH_SIZE
        end_index = min(start_index + WRITING_BATCH_SIZE, n_inter)
        batch_inter_str = "\n".join([f"index_id: {j},   interrogatory: {interrogatories_l[j]['interrogatory_id']}\t{interrogatories_l[j]['interrogatory']}\n" for j in range(start_index, end_index)])
        comand_prompt = open('backend/ai/prompts/rog_response/SUGGEST_RESPONSE').read()
        filled_in_prompt = comand_prompt.format(
            side=side,
            side_b=side_b,
            side_name=side_name,
            side_b_name=side_b_name,
            side_dep_inst=side_dep_instr,
            interrogatories=batch_inter_str
        )
        if i == 0:
            logger.info("SUGGEST_RESPONSE first_prompt=%s", filled_in_prompt)
        processes.append(filled_in_prompt)

    with concurrent.futures.ThreadPoolExecutor(max_workers=WRITING_MAX_WORKERS) as pool:
        future_map = {
            submit_with_context(
                pool, chat_completion_parse, prompt, ROGSuggestionBatch,
                model=WRITING_MODEL, reasoning_effort=WRITING_EFFORT,
            ): prompt for prompt in processes
        }
        sleep(1)
        for fut in concurrent.futures.as_completed(future_map):
            try:
                returned = fut.result()
                responses_l = returned.objections if returned is not None else []
            except SoftTimeLimitExceeded:
                raise
            except Exception:
                responses_l = []
            for each_resp in responses_l:
                try:
                    index_id = each_resp.index_id
                    if not 0 <= index_id < len(interrogatories_l):
                        continue
                    interrogatories_l[index_id]['objection'] = each_resp.objection
                    logger.info("SUGGEST_RESPONSE index_id=%s", index_id)
                    logger.info("SUGGEST_RESPONSE objection=%s", each_resp.objection)
                    interrogatories_l[index_id]['rationale'] = each_resp.rationale
                    interrogatories_l[index_id]['tips'] = each_resp.tips
                    logger.info("SUGGEST_RESPONSE rationale=%s", each_resp.rationale)
                    logger.info("SUGGEST_RESPONSE tips=%s", each_resp.tips)

                except (KeyError, ValueError, IndexError):
                    continue

    memory['drafted_sections/interrogatories'] = json.dumps(interrogatories_l)

    return {"message": "Form processed successfully",
            "interrogatories": interrogatories_l}


def handle_state1(request):
    result = state1.delay(request.form)
    return {"result_id": result.id}, 200


def _rog_response_state1_prerequisite_error(data: dict) -> dict | None:
    draft_id = Drafts.resolve_id(data['draftId'])
    memory = DBMemory(db=db, doc_id=draft_id)
    return guided_prerequisite_error(memory, ROG_RESPONSE_STATE1_REQUIRED_KEYS, 'interrogatory objections')


def show_hidden(s):
    logger.info("repr: %r", s)
    logger.info("unicode_escape: %s", s.encode("unicode_escape").decode("ascii"))
    logger.info("utf8 bytes: %s", list(s.encode("utf-8")))
    logger.info("codepoints:")
    for i, ch in enumerate(s):
        cp = ord(ch)
        name = unicodedata.name(ch, "UNNAMED")
        cat = unicodedata.category(ch)  # e.g. Zs = space, Cf = format, Cc = control
        logger.info("%04d U+%04X %s %s %r", i, cp, cat, name, ch)

@shared_task(ignore_result=False, soft_time_limit=WRITING_SOFT_TIME_LIMIT, time_limit=WRITING_HARD_TIME_LIMIT)
@guided_step_operation(DOC_TYPE, 2, lambda data: data['draftId'])
def state2(data: dict) -> dict:
    logger.info("state2 data")
    for k, v in data.items():
        logger.info("state2 %s: %s", k, v)

    draftId = Drafts.resolve_id(data['draftId'])
    memory = DBMemory(db=db, doc_id=draftId)

    interrogatories_l = json.loads(memory['drafted_sections/interrogatories'])
    # print("first item in interrogatories_l:")
    # show_hidden(interrogatories_l[0]['response'])

    side = memory['inputs/side']
    side_b = invert_side(side)
    side_name = memory['inputs/plaintiff'] if side.lower().startswith("plaintiff") else memory['inputs/defendant']
    side_b_name = memory['inputs/defendant'] if side.lower().startswith("plaintiff") else memory['inputs/plaintiff']

    pairs = [(int(k.split('-', 1)[1]), v.strip()) for k, v in data.items() if k.startswith('custom-') and v.strip()]
    custom_notes = OrderedDict(sorted(pairs, key=lambda t: t[0]))
    logger.info("state2 custom_notes=%s", custom_notes)

    tasks = []

    def pop_in_groups_fifo(d: OrderedDict, size: int):
        """Mutates d: pops FIFO and yields OrderedDict batches of up to `size`."""
        while d:
            n = min(size, len(d))
            group = OrderedDict()
            for _ in range(n):
                k, v = d.popitem(last=False)  # FIFO
                group[k] = v
            yield group

    # simplest: iterate the generator
    for batch_custom in pop_in_groups_fifo(custom_notes, WRITING_BATCH_SIZE):
        batch_str = ""
        for idx, note in batch_custom.items():
            # idx is the index_id, which corresponds to index of interrogatories_l
            inter = interrogatories_l[idx]
            batch_str += f"index_id:{idx}\nInterrogatory:{inter['interrogatory_id']}\t{inter['interrogatory']}\nExisting Objection:\n{inter['objection']}\nExisting Response:\n{inter['response']}\n==Lawyer's custom response:===\n{note}\n\n"
        command = open('backend/ai/prompts/rog_response/REWRITE_RESPONSE').read()
        filled_command = command.format(side=side, side_b=side_b, side_name=side_name, side_b_name=side_b_name, rog_responses=batch_str)
        logger.info("REWRITE_RESPONSE prompt=%s", filled_command)
        tasks.append(filled_command)

    with concurrent.futures.ThreadPoolExecutor(max_workers=WRITING_MAX_WORKERS) as pool:
        futures = [
            submit_with_context(
                pool, chat_completion_parse, t, ROGRewriteBatch,
                model=WRITING_MODEL, reasoning_effort=WRITING_EFFORT,
            ) for t in tasks
        ]
        sleep(1)
        for fut in concurrent.futures.as_completed(futures):
            try:
                returned = fut.result()
                responses_l = returned.objections_and_responses if returned is not None else []
            except SoftTimeLimitExceeded:
                raise
            except Exception:
                responses_l = []
            for each_resp in responses_l:
                try:
                    index_id = each_resp.index_id
                    if not 0 <= index_id < len(interrogatories_l):
                        continue
                    interrogatories_l[index_id]['objection'] = each_resp.objection
                    interrogatories_l[index_id]['response'] = each_resp.response
                    logger.info("REWRITE_RESPONSE index_id=%s", index_id)
                    logger.info("REWRITE_RESPONSE objection=%s", each_resp.objection)
                    logger.info("REWRITE_RESPONSE response=%s", each_resp.response)
                except (KeyError, ValueError, IndexError):
                    continue


    rog_response_text = "RESPONSES TO INTERROGATORIES\n"
    for item in interrogatories_l:
        resp_text = item.get('response', '')
        obj_text = item.get('objection', '')
        inter_id = item['interrogatory_id']
        if not any(ch.isalpha() for ch in inter_id):
            inter_id = f"Interrogatory No. {inter_id}"
        rog_response_text += f"{inter_id.upper()}\n{item['interrogatory']}\nRESPONSE\n{obj_text}\n{resp_text}\n\n"

    memory['drafted_sections/rog_response_text'] = rog_response_text
    memory['drafted_sections/interrogatories'] = json.dumps(interrogatories_l)

    filename = Drafts.pdf_filename(data['draftId'])
    memory['inputs/generated_pdf_filename'] = filename
    draft_full_rog_response(memory, f"backend/static/{filename}")

    return {"file_name": f'{API_URL}static/{filename}'}


def handle_state2(request):
    result = state2.delay(request.form)
    return {"result_id": result.id}, 200


def route_rog_response_logic(request):
    data = request.form
    appState = int(data['appState'])
    if appState == 0:
        return handle_state0(request)
    elif appState == 1:
        prerequisite_error = _rog_response_state1_prerequisite_error(data)
        if prerequisite_error:
            return prerequisite_error, 400
        return handle_state1(request)
    elif appState == 2:
        return handle_state2(request)
