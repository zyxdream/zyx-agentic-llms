
from backend.util.db import DBMemory
from backend.constants import API_URL, STATIC_FOLDER, SOURCE_CODE_ROOT, PYTHON_EXECUTABLE
from backend.pdf_creator import create_complaint_pdf
from backend.word_creator import create_complaint_word
from backend.ai import base as ai_base
from backend.ai.base import chat_completion, chat_completion_parse, num_tokens_from_string
from backend.ai.complaint_schemas import (
    ComplaintDraftedCauses,
    ComplaintFindings,
    ComplaintRegeneratedCitations,
)
from backend.util.data_tools import extract_from_json, create_ignore_set, allowed_filetypes, get_complaint_text_from_file
from backend.util.guided_draft_reset import COMPLAINT_RESET_KEYS, clear_memory_keys
from backend.ai.complaint_parser import get_text_from_pdf_fitz, get_last_ocr_warning
from backend.util.free_law_tools import find_USC_citation, filter_raw_citations, insert_tags, filter_on_casename
from backend.util.subprocess_tools import any_process_remaining
from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from backend.database import db
from backend.models import Drafts
import regex as re
from werkzeug.utils import secure_filename
import os
import logging
import textract
from time import sleep
from backend.util.analytics import (
    attach_step_usage,
    guided_step_operation,
    replay_guided_llm_usage,
)
import subprocess
import json
import concurrent.futures
import uuid

logger = logging.getLogger(__name__)


CONTACT_INFO_NUM_LINES = 20
DOC_TYPE = "complaint"
DOWNLOAD_STEP = 4
COMPLAINT_WRITING_MODEL = ai_base.DEFAULT_MODEL
COMPLAINT_WRITING_EFFORT = "high"

# Venue value (from the Complaint Drafter court picker) for the U.S. Court of Federal Claims, which is
# not a district court and therefore needs a different caption/phrasing than the district courts.
COURT_OF_FEDERAL_CLAIMS = "Court of Federal Claims"


def format_venue_caption(venue: str) -> str:
    """Caption block for document headers. The Court of Federal Claims is not a district court."""
    venue = venue or "Venue not yet specified"
    if venue.strip() == COURT_OF_FEDERAL_CLAIMS:
        return "UNITED STATES COURT OF FEDERAL CLAIMS"
    return "UNITED STATES DISTRICT COURT\n" + venue


def format_venue_phrase(venue: str) -> str:
    """Inline prose form of the venue for prompts."""
    venue = venue or ""
    if venue.strip() == COURT_OF_FEDERAL_CLAIMS:
        return "United States Court of Federal Claims"
    return "United States District Court for the " + venue


def draft_full_complaint(mem: DBMemory, write_fn: str) -> str:
    """Drafts a complaint from available elements in memory."""

    # create formatted pdf
    gen_pdf_status, _, _ = create_complaint_pdf(write_fn,
                                        plaintiff=mem.get('inputs/plaintiff', "specify plaintiff"),
                                        defendant=mem.get('inputs/defendant', "specify defendant"),
                                        attorneys_contact_info=mem.user.contact_info or "No Attorneys_contact_info",
                                        venue=format_venue_caption(mem.get('inputs/venue', '')),
                                        introduction=mem.get('drafted_sections/intro', ''),
                                        parties = mem.get('drafted_sections/parties', ''),
                                        jurisdiction_n_venue=mem.get('drafted_sections/jurisdiction_venue', ''),
                                        factual_allegations=mem.get('drafted_sections/factual_allegations', ''),
                                        cause_of_action=mem.get('drafted_sections/causes', ''),
                                        damages=mem.get('inputs/damages', "specify damages sought"),
                                        prayer_for_relief=mem.get('drafted_sections/prayer', ''),
                                          )
    return gen_pdf_status


def draft_full_complaint_word(mem: DBMemory, write_fn: str) -> str:
    """Drafts a complaint from available elements in memory."""

    # create formatted pdf
    gen_word_status = create_complaint_word(write_fn,
                                        plaintiff=mem.get('inputs/plaintiff', "specify plaintiff"),
                                        defendant=mem.get('inputs/defendant', "specify defendant"),
                                        attorneys_contact_info=mem.user.contact_info or "No Attorneys_contact_info",
                                        venue=format_venue_caption(mem.get('inputs/venue', '')),
                                        introduction=mem.get('drafted_sections/intro', ''),
                                        parties = mem.get('drafted_sections/parties', ''),
                                        jurisdiction_n_venue=mem.get('drafted_sections/jurisdiction_venue', ''),
                                        factual_allegations=mem.get('drafted_sections/factual_allegations', ''),
                                        cause_of_action=mem.get('drafted_sections/causes', ''),
                                        damages=mem.get('inputs/damages', "specify damages sought"),
                                        prayer_for_relief=mem.get('drafted_sections/prayer', ''),
                                        )
    return gen_word_status


def launch_llm_call_process(prompt:str) -> subprocess.Popen:
    # launch subprocess llm_call.py
    os.environ['PYTHONPATH'] = SOURCE_CODE_ROOT + os.pathsep + os.environ.get('PYTHONPATH', '')

    process = subprocess.Popen([PYTHON_EXECUTABLE, 
                             'backend/ai/llm_call.py', 
                             prompt], 
                             stdout=subprocess.PIPE, 
                             stderr=subprocess.PIPE)
    return process


def launch_llm_call_process_j(prompt: str, schema_name: str | None = None) -> subprocess.Popen:
    os.environ['PYTHONPATH'] = SOURCE_CODE_ROOT + os.pathsep + os.environ.get('PYTHONPATH', '')

    command = [PYTHON_EXECUTABLE, 'backend/ai/llm_call_j.py', '--analytics-envelope']
    if schema_name is not None:
        command.extend(['--schema', schema_name])
    command.append(prompt)
    process = subprocess.Popen(command,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    return process


def _stop_llm_processes(processes: list[subprocess.Popen]) -> None:
    """Do not leave Step 0's provider calls running after its Celery budget expires."""
    for process in processes:
        try:
            if process.poll() is not None:
                continue
            process.terminate()
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired) as exc:
                logger.warning("Could not stop timed-out Complaint LLM child: %s", exc)
        except OSError as exc:
            logger.warning("Could not terminate timed-out Complaint LLM child: %s", exc)


def _read_llm_process_output(process: subprocess.Popen) -> dict | str:
    """Replay subprocess usage into the parent step and return its LLM output."""
    raw_output = process.stdout.read().decode('utf-8')
    try:
        envelope = json.loads(raw_output)
        if not isinstance(envelope, dict) or "output" not in envelope:
            raise ValueError("Missing subprocess analytics envelope")
        replay_guided_llm_usage(envelope.get("usage"))
        return envelope.get("output") or ""
    except Exception:
        logger.exception("Could not parse Complaint subprocess analytics envelope")
        return raw_output


def _parse_complaint_output(
    prompt: str, response_model: type, *, model: str | None = None,
    reasoning_effort: str | None = None
) -> dict | None:
    parsed = chat_completion_parse(
        prompt,
        response_model,
        model=model or ai_base.DEFAULT_MODEL,
        reasoning_effort=reasoning_effort,
    )
    return parsed.model_dump(by_alias=True) if parsed is not None else None


def _legacy_cause_suggestions(suggestions: list[dict]) -> list[dict]:
    """Keep the persisted and frontend single-key-per-cause representation."""
    return [
        {
            suggestion['name']: {
                'description': suggestion['description'],
                'citations': suggestion['citations'],
            }
        }
        for suggestion in suggestions
    ]


def remove_md(instr: str) -> str:
    """Simply removes markdown."""
    outstr = instr.replace('**', '')
    for remove_str in ['###', '##', '#']:
        outstr = outstr.replace(remove_str, '')

    return outstr


def md2caps(instr: str) -> str:
    """Capatilizes any markdown headings."""
    ret_l = []
    for line in instr.split('\n'):
        clean_text = line.strip()
        if clean_text.startswith('##'):
            ret_l.append(line)
        else:
            ret_l.append(line)

    return '\n'.join(ret_l)


def combine_multiple(data_in: dict, prefix: str) -> str:
    # for a given prefix, sort and combine items into one string
    filtered_items = [(key, value) for key, value in data_in.items() if key.startswith(prefix)]
    filtered_items.sort(key=lambda item: item[0])
    return ", ".join([value for key, value in filtered_items])


@shared_task(bind=True, ignore_result=False, soft_time_limit=600, time_limit=630)
@guided_step_operation(DOC_TYPE, 0, lambda self, data: data['draftId'])
def state0(self, data: dict) -> str:
    doc_id=Drafts.resolve_id(data['draftId'])

    memory = DBMemory(db=db, doc_id=doc_id)
    ocr_warning = bool(data.get('ocr_warning', False))
    if ocr_warning:
        try:
            self.update_state(state='PROGRESS', meta={
                'status': 'ocr_warning',
                'ocr_warning': True,
                'ocr_warning_message': "Some scanned pages could not be read. Your document is still processed. Press OK to continue.",
            })
        except Exception as e:
            logger.warning("failed to push OCR warning state: %s", e)

    def save_if_something(keyname: str):
        """save the form field if something was submitted."""
        if keyname in data and len(data[keyname]) > 0:
                memory[f'inputs/{keyname}'] = data[keyname]

    # save inputs to memory/inputs
    for key in ['venue', 'factualAllegations']:
        save_if_something(key)

    # combine into single strings: 'plaintiff', 'defendant', 'plaintiffAddress', 'defendantAddress'
    memory['inputs/defendant'] = combine_multiple(data, 'defendant-')
    memory['inputs/defendantAddress'] = combine_multiple(data, 'defendantAddress-')

    memory['inputs/plaintiff'] = combine_multiple(data, 'plaintiff-')
    memory['inputs/plaintiffAddress'] = combine_multiple(data, 'plaintiffAddress-')

    # call the LLM 
    processes = []


    facts_and_parties = """
    List of Facts:
    {facts}

    Plaintiff: {plaintiff} 
    Defendant: {defendant}
    """
    facts_and_parties = facts_and_parties.format(facts=memory['inputs/factualAllegations'],
                                                                      plaintiff=memory['inputs/plaintiff'],
                                                                      defendant=memory['inputs/defendant'])
    try:
        # draft FA section
        comand_prompt = open('backend/ai/prompts/complaint/DRAFT_FA').read()
        processes.append(launch_llm_call_process_j(
            comand_prompt.format(facts_and_parties=facts_and_parties), 'complaint_facts'
        ))

        # suggest PfRs
        comand_prompt = open('backend/ai/prompts/complaint/SUGGEST_PFR').read()
        processes.append(launch_llm_call_process_j(
            comand_prompt.format(facts_and_parties=facts_and_parties), 'complaint_relief'
        ))

        # suggest introduction
        comand_prompt = open('backend/ai/prompts/complaint/DRAFT_INTRO').read()
        processes.append(launch_llm_call_process_j(
            comand_prompt.format(facts_and_parties=facts_and_parties), 'complaint_introduction'
        ))

        # suggest CoAs
        coa_template = open('backend/ai/prompts/complaint/SUGGEST_COA').read()
        comand_prompt = coa_template.format(venue=memory['inputs/venue'], facts_and_parties=facts_and_parties)
        processes.append(launch_llm_call_process_j(comand_prompt, 'complaint_causes'))

        # write parties and venue
        comand_prompt = open('backend/ai/prompts/complaint/DRAFT_PARTIESVENUE').read()
        filled_in_prompt = comand_prompt.format(facts_and_parties=facts_and_parties,
                                                plaintiff_address=memory.get('inputs/plaintiffAddress') or "Unknown",
            defendant_address=memory.get('inputs/defendantAddress') or "Unknown",
            venue=format_venue_phrase(memory['inputs/venue'])
        )
        processes.append(launch_llm_call_process_j(filled_in_prompt, 'complaint_parties_venue'))

        # Wait for all api calls to complete
        while any_process_remaining(processes):
            sleep(1)
    except SoftTimeLimitExceeded:
        _stop_llm_processes(processes)
        raise

    logger.info("all the LLM calls are done")

    process_outputs = [_read_llm_process_output(process) for process in processes]
    fa_raw = extract_from_json("Factual Allegations", process_outputs[0])
    # print("=== llm fa raw ===\n", fa_raw)
    # memory['drafted_sections/factual_allegations'] = remove_md(md2caps(fa_raw))
    memory['drafted_sections/factual_allegations'] = md2caps(fa_raw) if fa_raw else ""

    pfr_suggestions = extract_from_json("Suggested Prayers for Relief", process_outputs[1])
    logger.info("llm pfr_suggestions:\n%s", pfr_suggestions)
    suggestioned_intro = extract_from_json("Introduction", process_outputs[2])
    logger.info("llm suggestioned_intro:\n%s", suggestioned_intro)
    coa_suggestions = _legacy_cause_suggestions(
        extract_from_json("Suggested Causes of Action", process_outputs[3])
    )
    logger.info("llm coa_suggestions:\n%s", coa_suggestions)
    parties_venue_raw = process_outputs[4]
    # print(f"== parties and juridsdiction/venue output ===\n>{parties_venue_raw}<")
    parties_llm = extract_from_json("Parties", parties_venue_raw)
    logger.info("parties_llm # paras: %s", len(parties_llm))
    # parties_llm now is a list of 2 string, one plaintiff paragraph, one defendant paragraph
    memory['drafted_sections/parties'] = '\n\n'.join(parties_llm)

    jurisdiction_llm = extract_from_json("Jurisdiction", parties_venue_raw) # here expexted string, but extract_from_json will return [] if anything went wrong, 
    venue_llm = extract_from_json("Venue", parties_venue_raw)
    memory['drafted_sections/jurisdiction_venue'] = '\n'.join([jurisdiction_llm, venue_llm]) if (jurisdiction_llm and venue_llm) else "" 

    # parse out citations in suggested_coa, go through validation
    # Now citations come as structured objects with case_name, citation, parenthetical
    for each_coa in coa_suggestions:
        
        for key_coa, val in each_coa.items():
            # Always initialize these arrays, even if empty
            raw_citations = val.get('citations', [])
                        # manually add one  for testing
            logger.info("raw_citations: %s", raw_citations)
            filtered_citations = []
            filtered_citations_info_l = []
            citations_html = []
            
            for raw_cit in raw_citations:
                if not raw_cit or 'citation' not in raw_cit:
                    continue
                try:
                    logger.info("filtering raw citation: %s", raw_cit)
                    # First filter through FreeLaw API
                    before_case_filtered_citations_info = filter_raw_citations(raw_cit['citation'])
                    # if raw_cit.get('case_name', '') == '':  # means it is a statute/code, not a case, so no need to filter on case name
                    #     filtered_citations_info = before_case_filtered_citations_info
                    #     print("no need to filter on case name, it is a statute/code")
                    # else:

                    # Further filter on case name to avoid false positives
                    filtered_citations_info = filter_on_casename(raw_cit, before_case_filtered_citations_info)
                    logger.info(
                        "# of filtered citations before filter_on_casename: %s",
                        len(before_case_filtered_citations_info),
                    )
                    logger.info(
                        "# of filtered citations after filter_on_casename: %s",
                        len(filtered_citations_info),
                    )

                    if filtered_citations_info:
                        # Build full citation string
                        full_citation_str = f"{raw_cit.get('case_name', '')}, {raw_cit.get('citation', '')} {raw_cit.get('parenthetical', '')}"

                        filtered_citations.append(full_citation_str)
                        filtered_citations_info_l.extend(filtered_citations_info)

                        # Create HTML version with tags
                        if len(filtered_citations_info) > 1:
                            filtered_citations_info.sort(key=lambda x: x['start_index'])

                        annotated_result = raw_cit['citation']
                        offst = 0
                        for citation_info in filtered_citations_info:
                            annotated_result, next_offset = insert_tags(annotated_result, citation_info, offst)
                            offst += next_offset

                        # here we are sure about the case_name so we want to include it inside the <href> tags inside the annotated_result. just extend the start anchor to include raw_cit.get('case_name')
                        # Match the opening <a> tag by looking for the specific format from insert_tags
                        # Format is: <a href="..." target="_blank" rel="noopener noreferrer">
                        # Handle case where URL contains < and > like "523-F<dot>"
                        areft_tag_regex = r'<a\s+href="[^"]*"\s+target="_blank"\s+rel="noopener noreferrer">'
                        
                        to_replace = re.search(areft_tag_regex, annotated_result)
                        if to_replace:
                            logger.info("expanding the anchor to include the case_name")
                            logger.info("matched part: %s", to_replace.group(0))
                            if raw_cit.get('case_name', ''):
                                tag_plus_casename = to_replace.group(0) + ' ' + str(raw_cit.get('case_name', '')) + ', '
                                logger.info("tag_plus_casename: %s", tag_plus_casename)
                                annotated_result = annotated_result.replace(to_replace.group(0), tag_plus_casename)
                                
                            # Build full HTML citation string
                            annotated_full_str = f"{annotated_result} {raw_cit.get('parenthetical', '')}"
                        else:
                            if raw_cit.get('case_name', ''):
                            # Build full HTML citation string without expanding anchor
                                annotated_full_str = f"{raw_cit.get('case_name', '')}, {annotated_result} {raw_cit.get('parenthetical', '')}"
                            else:
                                annotated_full_str = f"{annotated_result} {raw_cit.get('parenthetical', '')}"

                        citations_html.append(annotated_full_str)


                except Exception as exc:
                    logger.warning(
                        "Failed to filter citation %s: %s",
                        raw_cit.get('citation', ''),
                        exc,
                    )
            
            # Always set these arrays, even if empty - frontend needs them to exist
            val['filtered_citations'] = filtered_citations if filtered_citations else []
            val['filtered_citations_info'] = filtered_citations_info_l if filtered_citations_info_l else []
            val['citations_html'] = citations_html if citations_html else []
            
            # Debug: Log what we're setting
            logger.info(
                "COA '%s': filtered_citations=%s, citations_html=%s",
                key_coa,
                len(filtered_citations),
                len(citations_html),
            )
            if citations_html:
                logger.info("  First citation_html: %s...", citations_html[0][:100])
            else:
                logger.info("  No citations passed filtering - arrays initialized as empty")

    # Save COA suggestions, pfr suggestions, and intro suggestion to memory for later retrieval
    memory['analysis/coa_suggestions'] = json.dumps(coa_suggestions)
    memory['analysis/pfr_suggestions'] = json.dumps(pfr_suggestions if pfr_suggestions else [])
    memory['analysis/intro_suggestion'] = suggestioned_intro if suggestioned_intro else ""
    logger.info(
        "Final coa_suggestions structure (first COA): %s",
        coa_suggestions[0] if coa_suggestions else "empty",
    )
    filename = Drafts.pdf_filename(data['draftId'])
    memory['inputs/generated_pdf_filename'] = filename
    # draft_full_complaint(memory, f"backend/static/{filename}")
    gen_pdf_status, paras, _ = create_complaint_pdf(f"backend/static/{filename}",
                                        plaintiff=memory.get('inputs/plaintiff', "specify plaintiff"),
                                        defendant=memory.get('inputs/defendant', "specify defendant"),
                                        attorneys_contact_info=memory.user.contact_info or "No Attorneys_contact_info",
                                        venue=format_venue_caption(memory.get('inputs/venue', '')),
                                        introduction='',
                                        parties = memory.get('drafted_sections/parties', ''),
                                        jurisdiction_n_venue=memory.get('drafted_sections/jurisdiction_venue', ''),
                                        factual_allegations=memory.get('drafted_sections/factual_allegations', ''),
                                        cause_of_action='',
                                        damages="",
                                        prayer_for_relief='',
                                        )
    logger.info("== State0 generated paragraphs ==\n%s", paras)
    memory['drafted_sections/paragraphs'] = paras

    return {"url": f'{API_URL}static/{filename}',
            "intro": suggestioned_intro if suggestioned_intro else "",
            "pfr_suggestions": pfr_suggestions if pfr_suggestions else "",
            "coa_suggestions": coa_suggestions if coa_suggestions else ""}


def handle_state0(request):
    """Unpack form inputs from request if needed."""
    # if a file was submitted, get text from it 
    data = request.form

    ocr_warning = False

    if 'file' in request.files:
        logger.info("handle_state0 received a file upload")
        file = request.files['file']
        filename = secure_filename(file.filename)
        if allowed_filetypes(filename=filename):
            f_type = filename.rsplit('.', 1)[1].lower()
            if f_type == 'txt':
                file_contents = file.read().decode('utf-8')
            elif f_type in ['doc', 'docx', 'pdf']:
                temp_filename = f"{uuid.uuid4()}_{filename}"
                filepath = os.path.join(STATIC_FOLDER, temp_filename)
                try:
                    file.save(filepath)
                    # Use shared complaint parser to leverage OCR + warning flag
                    parsed_text, _, _ = get_complaint_text_from_file(filepath)
                    ocr_warning = get_last_ocr_warning()

                    # for testing error message, we set it to True
                    # ocr_warning = True
                    file_contents = parsed_text or ""
                finally:
                    if os.path.exists(filepath):
                        try:
                            os.remove(filepath)
                        except OSError:
                            pass
            else:
                raise Exception("all allowed filetypes are covered.")

            data = dict(request.form)
            data['factualAllegations'] = data.get('factualAllegations', '') + file_contents
        else:
            return {'message': 'Only txt, pdf, doc and docx files are accepted'}, 401

    logger.info("received form data: %s", data)
    data = dict(data)
    data['ocr_warning'] = ocr_warning
    doc_id = Drafts.resolve_id(data['draftId'])
    memory = DBMemory(db=db, doc_id=doc_id)
    clear_memory_keys(memory, COMPLAINT_RESET_KEYS)
    result = state0.delay(data)
    return {
        "result_id": result.id,
    }



@shared_task(ignore_result=False, soft_time_limit=600, time_limit=630)
@guided_step_operation(DOC_TYPE, 1, lambda data: data['draftId'])
def state1(data: dict) -> str:
    doc_id=Drafts.resolve_id(data['draftId'])
    logger.info("state1 data received")
    for k, v in data.items():
        logger.info("%s:%s", k, v)
    # print("===")
    # remove ignored CoAs
    # Initialize memory before reading saved data
    memory = DBMemory(db=db, doc_id=doc_id)
    try:
        saved_coa_suggestions = json.loads(memory.get("analysis/coa_suggestions", "[]"))
    except Exception as e:
        logger.warning("Error loading coa suggestions: %s", e)
        saved_coa_suggestions = []

    logger.info("saved_coa_suggestions: %s", saved_coa_suggestions)

    ignores = create_ignore_set(data)
    logger.info("ignore set: %s", ignores)
    ignores_index = [int(coa[3:]) for coa in ignores]
    logger.info("ignores_index: %s", ignores_index)
    
      
    instruction_text = (
            "\n**GUIDANCE OF USING USE SELECTED CITATIONS IN YOUR FOLLOWING DRAFTING:** Only choose from the user's selected law principles or cases in your drafting for each specific cause of action. "
            "these citations are verified by user, so be sure to correctly use them when needed. "
            "You do not requireed to use all of them, based on other drafting guidance, use them at your best judges. "
            "Do not use or add other law inferences other than what have been provided.\n"
        )

    coa_blocks_str = []
    num_coas = 0
    for idx, coa_dict in enumerate(saved_coa_suggestions):
        logger.info("idx: %s", idx)
        if idx not in ignores_index:
            logger.info("index not in ignores_index")
            num_coas += 1
            coa_name, val = next(iter(coa_dict.items()))
            coa_blocks_str.append(f"COA {idx+1}: {coa_name}\n")
            coa_blocks_str.append(f"Description: {val['description']}\n")
            coa_blocks_str.append(f"User-selected verified citations for this cause of action:\n")
            selected_cits = val.get('selected_citations', [])
            if selected_cits:
                for cit in selected_cits:
                    coa_blocks_str.append(f"- {cit}\n")
            
            else:
                coa_blocks_str.append("- (none)\n")

    coa_blocks_str =  "\n".join(coa_blocks_str) + instruction_text

    # call LLM to draft Causes of Action seciton
    # memory = DiskMemory('backend/memory/')
    memory = DBMemory(db=db, doc_id=doc_id)
    # use SO FAR GENERATED paragraphs  for correct paragraph numbers
    fa = " {} \n Plaintiff: {} \nDefendant: {}".format(memory['drafted_sections/paragraphs'],
                                                       memory['inputs/plaintiff'],
                                                       memory['inputs/defendant'])

    prompt_template = open('backend/ai/prompts/complaint/DRAFT_COA').read()
    filled_in_prompt = prompt_template.format( memory['inputs/venue'], fa, coa_blocks_str, num_coas)
    logger.info("=== DRAFT COA filled in prompt ===\n%s", filled_in_prompt)

    coa_output = _parse_complaint_output(
        filled_in_prompt, ComplaintDraftedCauses,
        model=COMPLAINT_WRITING_MODEL, reasoning_effort=COMPLAINT_WRITING_EFFORT,
    )
    coa_section_llm = extract_from_json("Causes of Action", coa_output)
    coa_section_text = '\n'.join(coa_section_llm)
    logger.info("state1 llm coa_section_text:\n%s", coa_section_text)

    # save to memory/drafted_sections causes
    memory['drafted_sections/causes'] = remove_md(md2caps(coa_section_text))
    logger.info("saved causes text:\n%s", memory['drafted_sections/causes'])

    # update full complaint
    filename = Drafts.pdf_filename(data['draftId'])
    memory['inputs/generated_pdf_filename'] = filename
    # draft_full_complaint(memory, f"backend/static/{filename}")
    gen_pdf_status, paras, _ = create_complaint_pdf(f"backend/static/{filename}",
                                plaintiff=memory.get('inputs/plaintiff', "specify plaintiff"),
                                defendant=memory.get('inputs/defendant', "specify defendant"),
                                attorneys_contact_info=memory.user.contact_info or "No Attorneys_contact_info",
                                venue=format_venue_caption(memory.get('inputs/venue', '')),
                                introduction='',
                                parties = memory.get('drafted_sections/parties', ''),
                                jurisdiction_n_venue=memory.get('drafted_sections/jurisdiction_venue', ''),
                                factual_allegations=memory.get('drafted_sections/factual_allegations', ''),
                                cause_of_action=memory.get('drafted_sections/causes', ''),
                                damages="",
                                prayer_for_relief="",
                                    )
    logger.info("== State_1 generated paragraphs ==\n%s", paras)
    memory['drafted_sections/paragraphs'] = paras

    return {"url": f'{API_URL}static/{filename}'}

def handle_state1(request):
    result = state1.delay(request.form)
    return {"result_id": result.id}, 200


@shared_task(ignore_result=False, soft_time_limit=600, time_limit=630)
@guided_step_operation(DOC_TYPE, 2, lambda data: data['draftId'])
def state2(data: dict) -> str:
    doc_id=Drafts.resolve_id(data['draftId'])
    # remove ignored PfRs
    ignores = create_ignore_set(data)
    pfrs = [v for k,v in data.items() if k.startswith('pfr') and k not in ignores]

    logger.info("pfrs to be used: %s", pfrs)
    # call LLM to draft Prayer for Relief seciton
    # memory = DiskMemory('backend/memory/')
    memory = DBMemory(db=db, doc_id=doc_id)
    fa = " {} \n Plaintiff: {} \nDefendant: {}".format(memory['drafted_sections/paragraphs'],
                                                       memory['inputs/plaintiff'],
                                                       memory['inputs/defendant'])
    pfr_block_str = "\n".join(pfrs)
    comand_prompt = open('backend/ai/prompts/complaint/DRAFT_PFR').read()
    filled_in_prompt = "{} {} \nList of Facts: \n {}".format(comand_prompt, pfr_block_str, fa)
    pfr_section_text = chat_completion(
        filled_in_prompt, model=COMPLAINT_WRITING_MODEL,
        reasoning_effort=COMPLAINT_WRITING_EFFORT,
    )
    logger.info("llm pfr_section_text:\n%s", pfr_section_text)

    # save to memory/drafted_sections prayer
    memory['drafted_sections/prayer'] = remove_md(md2caps(pfr_section_text))
    logger.info("saved prayer text:\n%s", memory['drafted_sections/prayer'])

    # update full complaint
    filename = Drafts.pdf_filename(data['draftId'])
    memory['inputs/generated_pdf_filename'] = filename
    # draft_full_complaint(memory, f"backend/static/{filename}")
    gen_pdf_status, paras, _ = create_complaint_pdf(f"backend/static/{filename}",
                            plaintiff=memory.get('inputs/plaintiff', "specify plaintiff"),
                            defendant=memory.get('inputs/defendant', "specify defendant"),
                            attorneys_contact_info=memory.user.contact_info or "No Attorneys_contact_info",
                            venue=format_venue_caption(memory.get('inputs/venue', '')),
                            introduction='',
                            parties = memory.get('drafted_sections/parties', ''),
                            jurisdiction_n_venue=memory.get('drafted_sections/jurisdiction_venue', ''),
                            factual_allegations=memory.get('drafted_sections/factual_allegations', ''),
                            cause_of_action=memory.get('drafted_sections/causes', ''),
                            damages="",
                            prayer_for_relief=memory.get('drafted_sections/prayer', ""),
                            )
    logger.info("== State_2 generated paragraphs ==\n%s", paras)
    memory['drafted_sections/paragraphs'] = paras
    return {"url": f'{API_URL}static/{filename}'}

def handle_state2(request):
    result = state2.delay(request.form)
    return {"result_id": result.id}, 200


@shared_task(ignore_result=False)
@guided_step_operation(DOC_TYPE, 3, lambda data: data['draftId'])
def state3(data: dict) -> str:
    doc_id=Drafts.resolve_id(data['draftId'])

    # see if the intro is going to be ignored.
    if data['useIntro'] == 'no':
        intro_text = ""
    else:
        intro_text = data['introBody']

    # save to memory/drafted_sections/intro
    mem = DBMemory(db=db, doc_id=doc_id)
    mem['drafted_sections/intro'] = remove_md(md2caps(intro_text))

    # draft pdf
    filename = Drafts.pdf_filename(data['draftId'])
    mem['inputs/generated_pdf_filename'] = filename
    # first run through intro and facts and see how many paragraphs have generated so far
    gen_pdf_status, paras, last_para_i = create_complaint_pdf(f"backend/static/{filename}",
                            plaintiff=mem.get('inputs/plaintiff', "specify plaintiff"),
                            defendant=mem.get('inputs/defendant', "specify defendant"),
                            attorneys_contact_info=mem.user.contact_info or "No Attorneys_contact_info",
                            venue=format_venue_caption(mem.get('inputs/venue', '')),
                            introduction=mem['drafted_sections/intro'],
                            parties = mem.get('drafted_sections/parties', ''),
                            jurisdiction_n_venue=mem.get('drafted_sections/jurisdiction_venue', ''),
                            factual_allegations=mem.get('drafted_sections/factual_allegations', ''),
                            cause_of_action='',
                            damages="",
                            prayer_for_relief="")
    
    logger.info(
        "Before Draft full complaint, update COA paragraph referring, last_para_i: %s",
        last_para_i,
    )
    # Replaces 'paragraphs 1 through <number>' with 'paragraphs <new_paragraph_number>' everywhere in the string.
    pattern = r'paragraphs \d+ through \d+'
    replacement = rf'paragraphs 1 through {last_para_i}'
    update_coa = re.sub(pattern, replacement, mem['drafted_sections/causes'])
    # print("updated COA: \n", update_coa)

    mem['drafted_sections/causes'] = update_coa
    draft_full_complaint(mem, f"backend/static/{filename}")

    # Run hallucination guard

    # comand_prompt = open('backend/ai/prompts/complaint/HALLUCINATION_GUARD').read()
    # filled_in_prompt = comand_prompt.format(intro=mem.get('drafted_sections/intro', ''),
    #                                         jurisdiction_and_venue=mem.get('drafted_sections/parties_venue', ''),
    #                                         factual_allegations=mem.get('drafted_sections/factual_allegations', ''),
    #                                         causes=mem.get('drafted_sections/causes', ''),
    #                                         prayer=mem.get('drafted_sections/prayer', ''))
    # parse the just-drafted answer pdf file,
    drafted_text, _, _ = get_text_from_pdf_fitz(f"backend/static/{filename}")
    logger.info("number of tokens in answer_text: %s", num_tokens_from_string(drafted_text))
    comand_prompt = open('backend/ai/prompts/answer/HALLUCINATION_GUARD').read()
    filled_in_prompt = comand_prompt.format(text=drafted_text)
    llm_output = _parse_complaint_output(
        filled_in_prompt, ComplaintFindings,
        model=COMPLAINT_WRITING_MODEL,
        reasoning_effort=COMPLAINT_WRITING_EFFORT,
    )
    # print(llm_output)
    findings = extract_from_json("Findings", llm_output)
    halls = attach_ref_links([
        [finding['label'], finding['description']] for finding in findings
    ])
    mem['hallucination_check'] = json.dumps(halls)

    return {"url": f'{API_URL}static/{filename}',
            "halls": halls
            }


def handle_state3(request):
    result = state3.delay(request.form)
    return {"result_id": result.id}, 200



def attach_ref_links(intext: list) -> list:
    output = []
    for ref_l in intext:
        citations = find_USC_citation(ref_l[1])   # find_USC_citation return a unique list, here each ref_l will only contain one citation, so we take the first one
        if citations is not None:
            first_citation = citations[0]
            ref_URL = f'https://www.law.cornell.edu/uscode/text/{first_citation[0]}/{first_citation[1]}'
        else:
            ref_URL = None
        output.append((ref_l[0], ref_l[1], ref_URL))

    return output


@shared_task(ignore_result=False, soft_time_limit=600, time_limit=630)
@attach_step_usage(DOC_TYPE, 0, lambda data: data['draftId'])
def regenerate_citations_complaint(data: dict) -> dict:
    """
    Regenerate citations for a specific COA in complaint drafter.
    Expected data keys:
    - draftId: int
    - coa_index: int (the COA index)
    - coa_name: str
    - coa_description: str
    """
    doc_id = Drafts.resolve_id(data['draftId'])
    coa_index = int(data['coa_index'])
    for k, v in data.items():
        logger.info("key=%s, value=%s", k, v)
    
    mem = DBMemory(db=db, doc_id=doc_id)
    
    # Load the COA suggestions
    coa_suggestions = json.loads(mem.get("analysis/coa_suggestions", "[]"))
    
    coa_name = data['coa_name']
    coa_description = data.get('coa_description', '')
    venue = mem.get('inputs/venue', '')
    
    # Get existing citations from the COA structure
    existing_citations = []
    if coa_index < len(coa_suggestions):
        coa_obj = coa_suggestions[coa_index]
        for key, val in coa_obj.items():
            existing_citations = val.get('filtered_citations', [])
            break
    logger.info("existing_citations: %s", existing_citations)
    
    
    # Prepare the prompt for regenerating citations
    regenerate_prompt = f"""Given the following cause of action for a complaint, and existing legal citations, Can you provide additional legal authorities and citations to support it.

Venue: {venue}

Cause of Action:
{coa_name}

Description:
{coa_description}

Existing legal citations:
{existing_citations}

Please provide one or two additional legal authorities, statutes, governing regulations, or cases that also relavent and supporting this cause of action. Focus on controlling authority from the relevant jurisdiction.
Please only provide cases for which you can find a published official Bluebook form citation. Since this is for establishing Cause of Action, Governing Rules are preferred over cases.
Output format:
Return a JSON object with a "citations" array. Each citation should have:
- 'case_name': full case name in a string. if this is a law or a code and there is no case name, return empty string for this field.
- 'citation': One Bluebook form official citation for the case, or code / statutes / rules when germane.
- 'parenthetical': a short parenthetical explaining the key legal proposition. always wrap the content in parenthesis.

Example:
{{
  "citations": [
    {{"case_name": "Smith v. Jones", "citation": "123 F.3d 456", "parenthetical": "(holding that...)"}},
    {{"case_name": "", "citation": "35 U.S.C. § 271", "parenthetical": "(elements: unauthorized making, using, or selling of a patented invention)"}}
  ]
}}
"""
    
    logger.info("*** Regenerate citations prompt for coa_index=%s ***", coa_index)
    logger.info("regenerate_prompt: %s", regenerate_prompt)
    
    # Call LLM to get new citations
    try:
        llm_result = _parse_complaint_output(
            regenerate_prompt, ComplaintRegeneratedCitations,
            model=COMPLAINT_WRITING_MODEL,
            reasoning_effort=COMPLAINT_WRITING_EFFORT,
        )
        raw_citations = extract_from_json("citations", llm_result)
        logger.info(
            "Got %s raw citations from LLM: \n%s",
            len(raw_citations) if raw_citations else 0,
            raw_citations,
        )
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        logger.warning("Failed to get citations from LLM: %s", exc)
        return {"message": "Failed to generate new citations", "citations_html": [], "filtered_citations": []}
    
    if not raw_citations:
        return {"message": "No new citations generated", "citations_html": [], "filtered_citations": []}
    
    # Filter the raw citations - now they are structured objects
    new_citations_html = []
    new_filtered_citations = []
    new_filtered_citations_info = []
    
    for raw_cit in raw_citations:
        # raw_cit is now a dict with case_name, citation, year, parenthetical
        if not raw_cit or 'citation' not in raw_cit:
            continue
        
        try:
            # First filter through FreeLaw API
            before_case_filtered_citations_info = filter_raw_citations(raw_cit['citation'])
            
            # Further filter on case name to avoid false positives
            filtered_citations_info = filter_on_casename(raw_cit, before_case_filtered_citations_info)
            logger.info(
                "Filtered %s -> %s citations for %s",
                len(before_case_filtered_citations_info),
                len(filtered_citations_info),
                raw_cit.get('citation', ''),
            )
            
            if filtered_citations_info:
                # Build full citation string
                full_citation_str = ' '.join([
                    str(raw_cit.get('case_name', '')),
                    str(raw_cit.get('citation', '')),
                    str(raw_cit.get('parenthetical', ''))
                ]).strip()

                new_filtered_citations.append(full_citation_str)
                new_filtered_citations_info.extend(filtered_citations_info)

                # Create HTML version with tags
                if len(filtered_citations_info) > 1:
                    filtered_citations_info.sort(key=lambda x: x['start_index'])

                annotated_result = raw_cit['citation']
                offst = 0
                for citation_info in filtered_citations_info:
                    annotated_result, next_offset = insert_tags(annotated_result, citation_info, offst)
                    offst += next_offset

                # here we are sure about the case_name so we want to include it inside the <href> tags inside the annotated_result. just extend the start anchor to include raw_cit.get('case_name')
                # Match the opening <a> tag by looking for the specific format from insert_tags
                # Format is: <a href="..." target="_blank" rel="noopener noreferrer">
                # Handle case where URL contains < and > like "523-F<dot>"
                areft_tag_regex = r'<a\s+href="[^"]*"\s+target="_blank"\s+rel="noopener noreferrer">'
                
                to_replace = re.search(areft_tag_regex, annotated_result)
                if to_replace:
                    logger.info("expanding the anchor to include the case_name")
                    logger.info("matched part: %s", to_replace.group(0))
                    tag_plus_casename = to_replace.group(0) + ' ' + str(raw_cit.get('case_name', '')) + ' '
                    annotated_result = annotated_result.replace(to_replace.group(0), tag_plus_casename)
                    
                    # Build full HTML citation string
                    annotated_full_str = ' '.join([
                        annotated_result,
                        str(raw_cit.get('parenthetical', ''))
                    ]).strip()
                else:
                    # Build full HTML citation string without expanding anchor
                    annotated_full_str = ' '.join([
                        str(raw_cit.get('case_name', '')),
                        annotated_result,
                        str(raw_cit.get('parenthetical', ''))
                    ]).strip()
                
                new_citations_html.append(annotated_full_str)
                
        except SoftTimeLimitExceeded:
            raise
        except Exception as exc:
            logger.warning(
                "Failed to filter citation %s: %s",
                raw_cit.get('citation', ''),
                exc,
            )
    
    # Update the COA in memory with the new citations (appending to existing)
    if coa_index < len(coa_suggestions):
        coa_obj = coa_suggestions[coa_index]
        for key, val in coa_obj.items():
            logger.info("key: %s", key)
            
            val['citations_html'].extend(new_citations_html)
            val['filtered_citations'].extend(new_filtered_citations)
            val['filtered_citations_info'].extend(new_filtered_citations_info)

            logger.info("new_citations_html: %s", val['citations_html'])
            logger.info("new_filtered_citations: %s", val['filtered_citations'])
            logger.info("new_filtered_citations_info: %s", val['filtered_citations_info'])
    
    # print out data for this coa_suggestions[coa_index] after update
    logger.info(
        "data for this coa_suggestions[coa_index] after update: \n%s",
        json.dumps(coa_suggestions[coa_index], indent=4),
    )
    # Save updated COA suggestions back to memory
    mem["analysis/coa_suggestions"] = json.dumps(coa_suggestions)
    
    
    return {
        "message": "Successfully regenerated citations",
        "citations_html": new_citations_html,
        "filtered_citations": new_filtered_citations,
        "coa_index": coa_index
    }


def update_citation_selections_complaint(data: dict) -> dict:
    """
    Update the stored COA suggestions to keep only selected citations.
    Expected data keys:
    - draftId: int
    - citationSelections: dict with keys like "0" mapping to arrays of booleans
    """
    doc_id = Drafts.resolve_id(data['draftId'])
    citation_selections = data['citationSelections']
    logger.info("=== update_citation_selections_complaint ===")
    logger.info("citation_selections: %s", citation_selections)
    
    mem = DBMemory(db=db, doc_id=doc_id)
    
    # Load the COA suggestions
    coa_suggestions = json.loads(mem.get("analysis/coa_suggestions", "[]"))
    
    # Update each COA's citations based on selections
    for key, selections in citation_selections.items():
        coa_index = int(key)
        
        if coa_index >= len(coa_suggestions):
            continue
        
        coa_obj = coa_suggestions[coa_index]
        for coa_name, val in coa_obj.items():
            logger.info("COA: %s", coa_name)
            
            # Build selected_citations based on selections (do not mutate originals)
            original_filtered_citations = val.get('filtered_citations', [])
            logger.info("original_filtered_citations: %s", original_filtered_citations)

            # If user selections are empty OR there are no filtered citations, set to empty
            if not selections or not original_filtered_citations:
                val['selected_citations'] = []
                logger.info(
                    "User selected_citations: [] (empty due to empty selections or no filtered citations)"
                )
                continue

            selected_filtered_citations = [
                cit for i, cit in enumerate(original_filtered_citations)
                if i < len(selections) and selections[i]
            ]
            val['selected_citations'] = selected_filtered_citations
            logger.info("User selected_citations: %s", selected_filtered_citations)

    # Save updated COA suggestions back to memory
    mem["analysis/coa_suggestions"] = json.dumps(coa_suggestions)
    
    logger.info("Updated citation selections for draft %s", doc_id)
    return {"message": "Citation selections updated successfully"}


def route_complaint_logic(request):
    """Decides which step should be used and passes request to those steps."""

    # switch on appState in request
    data = request.form
    appState = int(data['appState'])

    if appState == 0:
        return handle_state0(request)
    elif appState == 1:
        return  handle_state1(request)
    elif appState == 2:
        return  handle_state2(request)
    elif appState == 3:
        return  handle_state3(request)
