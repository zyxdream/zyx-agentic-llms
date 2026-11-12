from werkzeug.utils import secure_filename
import os
import re
from backend.util.analytics import attach_step_usage, guided_step_operation, submit_with_context
from backend.ai.base import extract_parties_venue, num_tokens_from_string, split_into_chunks
from backend.ai.base import chat_completion, chat_completion_j, get_coa_loc, multichat_completion_parse
from backend.ai import base as ai_base
from backend.util.db import BaseMemory, DBMemory
from backend.constants import API_URL, STATIC_FOLDER
from backend.ai.complaint_parser import get_text_from_pdf_fitz, get_last_ocr_warning
from backend.util.data_tools import (
    handle_file_upload,
    get_complaint_text_from_file,
    get_complaint_text_with_line_metadata_from_file,
    log_complaint_text_metadata_preview,
    prepare_complaint_text_for_memory,
    process_is_federal_output,
    extract_from_json,
    remove_biz_type,
    int_to_roman,
)
from backend.util.guided_draft_reset import DISMISS_RESET_KEYS, clear_memory_keys
from backend.util.free_law_tools import filter_raw_citations, insert_tags, filter_on_casename
from celery import shared_task
from backend.database import db
from backend.models import Drafts
from backend.pdf_creator import create_dismiss_pdf, insert_toa_into_brief, apply_frame_and_footers, create_dismiss_pdf_no_toc
from backend.word_creator import create_dismiss_word
import json
from pydantic import BaseModel, Field

from time import sleep
import concurrent.futures


DOC_TYPE = "motion to dismiss"
DOWNLOAD_STEP = 5
LINES_PER_CHUNK_PDF = 40
LINES_PER_CHUNK_DOCX = 60


class UpdatedBackgroundSection(BaseModel):
    paragraphs: list[str]
    sub_heading: str = Field(alias="sub-heading")


class UpdatedFactualBackground(BaseModel):
    background: list[UpdatedBackgroundSection]

# ###### Set up logging for citation pairs  ####
# CIT_FILE = os.path.join(os.path.dirname(__file__), "citation_pairs.jsonl")
# MISMATCH_FILE = os.path.join(os.path.dirname(__file__), "mismatch_citation_pairs.jsonl")


def _synthesize_factual_background(prompt: str) -> str:
    return chat_completion_j(prompt, model=ai_base.DEFAULT_MODEL, reasoning_effort="high")


def draft_full_dismiss_word(mem: BaseMemory, fn: str) -> str:
    """Drafts an MOTION TO DISMISS from available elements in memory."""
    gen_word_status = create_dismiss_word(fn=fn,
                                        plaintiff=mem.get('inputs/plaintiff', "specify plaintiff"),
                                        defendant=mem.get('inputs/defendant', "specify defendant"),
                                        attorneys_contact_info=mem.user.contact_info or "No Attorneys_contact_info",
                                        venue=mem.get('inputs/venue', "Venue not yet specified"),
                                        introduction=mem.get('drafted_sections/intro', ''),
                                        background = mem.get('drafted_sections/fa_background', ''),
                                        standard = mem.get('drafted_sections/legal_standard', ''),
                                        arguments = mem.get('drafted_sections/arguments', ''),
                                        conclusion=mem.get('drafted_sections/conclusion', '')
                                          )
    return  gen_word_status


@shared_task(bind=True, ignore_result=False)
@guided_step_operation(DOC_TYPE, 0, lambda self, filename, draftId: draftId)
def state0(self, filename: str, draftId: str) -> dict:
    """
    - get details of factual allegation from uploaded complaint
    - return a list of fact details and suggested fact background for MTD 
    """
    doc_id=Drafts.resolve_id(draftId)
    memory = DBMemory(db=db, doc_id=doc_id)
    memory['inputs/uploaded_filename'] = filename

    # parse text from submitted PDF
    complaint_text, case_number, is_image, line_metadata = get_complaint_text_with_line_metadata_from_file(STATIC_FOLDER + "/" + filename)
    ocr_warning = get_last_ocr_warning()
    # ocr_warning = True   # for testing error message
    if ocr_warning:
        try:
            self.update_state(state='PROGRESS', meta={
                'status': 'ocr_warning',
                'ocr_warning': True,
                'ocr_warning_message': "Some scanned pages could not be read. Your document is still processed. Press OK to continue.",
            })
        except Exception as e:
            print(f"Warning: failed to push OCR warning state: {e}")
    print("case number: ", case_number)
    memory['inputs/case_number'] = case_number

    cover_text, text_by_lines, normalized_metadata = prepare_complaint_text_for_memory(
        filename,
        complaint_text,
        line_metadata,
        lines_per_chunk_pdf=LINES_PER_CHUNK_PDF,
        lines_per_chunk_docx=LINES_PER_CHUNK_DOCX,
    )
    print("total # of lines in complaint_text: ", len(text_by_lines))
    log_complaint_text_metadata_preview(filename, text_by_lines, normalized_metadata)
    memory['complaint_cover_text'] = cover_text
    memory['complaint_text'] = json.dumps(text_by_lines)
    memory['line_metadata'] = json.dumps(normalized_metadata)
        

    plaintiff_l, defendant_l, venue = extract_parties_venue(memory['complaint_cover_text'])
    print("plaintiff: ", plaintiff_l)
    print("# plaintiff = ", len(plaintiff_l))
    print("defendant: ", defendant_l)
    print("# defendant = ", len(defendant_l))
    print("venue: ", venue)
   
    memory['inputs/plaintiff'] = ";".join([remove_biz_type(each) for each in plaintiff_l])
    print("memory plaintiff saved as: ", memory['inputs/plaintiff'])
    memory['inputs/venue'] = venue
    memory['inputs/defendant'] = ";".join([remove_biz_type(each) for each in defendant_l])

    # determine if this is a federal or state case
    comand_prompt = open('backend/ai/prompts/answer/DETERMINE_FEDERAL').read()
    filled_in_prompt = comand_prompt + venue
    llm_output =  chat_completion(filled_in_prompt)
    is_federal = process_is_federal_output(llm_output)
    print("is_fedenral: ", is_federal)
    memory['inputs/is_federal'] = is_federal

    # create a string that collects basic information of the case to be used later in prompts
    case_info_str = f"Plaintiff: {plaintiff_l}\nDefendants: {defendant_l}\nVenue: {venue}\n Is federal: {is_federal}\n"

    # # extract FA from complaint as text lines  
    # start_to_end_i = get_fa_loc(text_by_lines)
    # fa_lines = text_by_lines[start_to_end_i[0] : start_to_end_i[-1]]
    # print(f"*** fa begins: ***\n{fa_lines[:5]}")
    # print(f"*** fa ends: ***\n{fa_lines[-5:]}")
    # memory["fa_loc"] = ','.join(map(str, start_to_end_i))  # save the entire list (first number is the start of fa, last is the end of fa)
    
 
     # ======== 1st Step: extract COA from complaint as list of text lines  ======
    start_to_end_i = get_coa_loc(text_by_lines)
    coa_lines_blocks = []
    if start_to_end_i:
        for i in range(len(start_to_end_i)-1):
            start_i = start_to_end_i[i]
            end_i = start_to_end_i[i+1]
            coa_lines_blocks.append(text_by_lines[start_i: end_i])
    else:
        coa_lines_blocks = [text_by_lines]
        print("# coa blocks = 1")
    print("number of coa_blocks: ", len(coa_lines_blocks))
    memory['coa_loc'] = json.dumps(start_to_end_i)

    coa_sum_comand = open("backend/ai/prompts/dismiss/COA_SUM").read()
    coa_tasks = []
    for i, block in enumerate(coa_lines_blocks):
        filled_in_prompt = coa_sum_comand.format(coa=' '.join(block))
        # if i == 0 or i == len(coa_lines_blocks)-1:print("*** coa sum filled in prompt ***\n", filled_in_prompt)
        print("*** coa sum filled in prompt ***\n", filled_in_prompt[:250])
        coa_tasks.append((i, filled_in_prompt))

    coa_tasks_outputs = [[] for _ in range(len(coa_tasks))]   # initialize placeholder to hold result for each task
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
        future_map = {
            submit_with_context(pool, chat_completion_j, task[1]): task for task in coa_tasks}
        sleep(1)
        print(f"-- COA_SUM created {len(pool._threads)} threads")
        for fut in concurrent.futures.as_completed(future_map):
            task = future_map[fut]
            task_id = task[0]  # index
            try:
                output = fut.result()
                coas_l = extract_from_json("output", output)
                coa_tasks_outputs[task_id] = coas_l
                print(f"task block i = {task_id} found {len(coas_l)} cause of actions.")
            except Exception as exc:
                print(f"-- COA_SUM task failed with exception: {str(exc)}")
    
    # flatten
    coa_sums = [each_coa for coas in coa_tasks_outputs for each_coa in coas]
    print("Total number of llm identified COAs: ", len(coa_sums))
    coa_dict = dict()
    for i, each_coa in enumerate(coa_sums):
        coa_name = each_coa.get('cause of action', f'Cause of Action {i+1}')
        coa_desc = each_coa.get('description', '')
        print("llm coa_name: ", coa_name)
        print("llm description: ", coa_desc[:20])
        # coa_dict[coa_name] = {'description':coa_desc}
        coa_dict[str(i)] =  {'title':coa_name, 'description':coa_desc}

    memory['inputs/coa_sums'] = json.dumps(coa_dict)
    coa_titles = '\n'.join([data['title'] for _, data in coa_dict.items()])
    print("== AI identified COA titles ==\n", coa_titles)
    case_info_str += f"Claims Asserted in the Complaint:\n{coa_titles}"
    memory['inputs/case_info'] = case_info_str
    # save title -> {'description':desc} to database

    # ===== 2nd Step: after coas being recognized, treat everything before start_to_end_i[0] as 'facts'
    # split fa lines into chunk of fa lines, with 5 overlapping lines
    # Alternatively, we can send the whole complaint (McLaughlin v. Homelight, facts are largely embedded inside claims)
    
    print("coa start_to_end_i: ", start_to_end_i)
    if start_to_end_i:
        fa_lines = text_by_lines[:start_to_end_i[0]]
    else:
        fa_lines = text_by_lines
        print("# fa lines = 1")
    fa_chunks = split_into_chunks(fa_lines)
    # fa_chunks = split_into_chunks(text_by_lines)
    print("# fa chunks: ", len(fa_chunks))

    # get key information from each chunk["fa"] in fa_chunks, combine llm output to get a complete list of detailed summary
    summarize_command = open("backend/ai/prompts/element_checker/FA_SUM").read()
    fa_sums = []
    fa_tasks = []
    for i, chunk in enumerate(fa_chunks):
        filled_in_prompt = summarize_command.format(fa_sub=' '.join(chunk))
        # if i == 0 or i == len(fa_chunks)-1: print("*** fa sub-summary filled in prompt ***\n", filled_in_prompt)
        fa_tasks.append((i, filled_in_prompt))

    fa_tasks_outputs = [[] for _ in range(len(fa_tasks))]   # initialize placeholder to hold result for each task
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
        future_map = {
            submit_with_context(pool, chat_completion_j, task[1]): task for task in fa_tasks}
        sleep(1)
        print(f"-- FA_SUM created {len(pool._threads)} threads")
        for fut in concurrent.futures.as_completed(future_map):
            task = future_map[fut]
            task_id = task[0]  # index
            try:
                output = fut.result()
                fa_l = extract_from_json("summary", output)
                fa_tasks_outputs[task_id] = fa_l
            except Exception as exc:
                print(f"-- FA_SUM task failed with exception: {str(exc)}")
     # flatten
    fa_sums = [each_fa for fas in fa_tasks_outputs for each_fa in fas]
    print("# total fa_sums from sub-processes: ", len(fa_sums))
    # save list of detailed summary 
    memory['inputs/fa_sums'] = json.dumps(fa_sums)  # saved data must be str, json, to use, json.loads()

    # send aggregated details to get final factual background
    fact_bg_command = open("backend/ai/prompts/dismiss/SUGGEST_FACT_BG").read()
    filled_in_prompt = fact_bg_command.format(summary='\n'.join(fa_sums), case_info = case_info_str)
    print(f"== SUGGEST_FACT_BG prompt === {num_tokens_from_string(filled_in_prompt)} tokens\n{filled_in_prompt[:500]}")
    output = _synthesize_factual_background(filled_in_prompt)
    llm_fa_bg = extract_from_json("background", output)
    print(f"=== llm_fa_bg ===\n", llm_fa_bg)
    # constructure background from the json structured llm output
    fa_bg_str = ""
    for sub in llm_fa_bg:
        if 'sub-heading' in sub.keys():
            fa_bg_str += f"\n**{sub['sub-heading']}**\n"
        for para in sub.get('paragraphs', []):
            fa_bg_str += para + '\n'
    print(f"=== fa background ===\n", fa_bg_str)        
    # save initial suggested fa background
    memory['drafted_sections/fa_background'] = fa_bg_str

    # ===  next generate legal standard  ===
    legal_std_comand = open("backend/ai/prompts/dismiss/SUGGEST_STANDARD").read()

    if is_federal:
        is_federal_line = "Please note the above case is a federal case. Do not cite any state procedural rule while referencing Federal Rule 12(b)(6)"
    else:
        is_federal_line = "Please note the above case is a state case. Please cite state procedure accordingly. "

    filled_in_prompt = legal_std_comand.format(case_info=case_info_str, fact=fa_bg_str, is_federal_line = is_federal_line)
    print("== SUGGEST_STANDARD filled_in_prompt==\n", filled_in_prompt)
    output = chat_completion_j(filled_in_prompt, model=ai_base.DEFAULT_MODEL, reasoning_effort="high")
    
    llm_legal_std = extract_from_json('legal standard', output)
    print("=== llm_legal_std ====: \n", llm_legal_std)
   
    llm_citations = extract_from_json('citations', output)
    print("llm_rules: ", llm_citations)

    memory['drafted_sections/legal_standard'] = '\n\n'.join(llm_legal_std)
    memory['drafted_sections/standard_citations'] = json.dumps(llm_citations)

 
    return {"message": "Form processed successfully", 'file_name': f'{API_URL}static/{filename}',
        "fas":fa_sums,
        "background": fa_bg_str,
        "standard_citations":'\n'.join(str(cite) for cite in llm_citations),
        "standard": '\n\n'.join(llm_legal_std)
        }


def handle_state0(request):
    """State 0 is to simply upload a file, and generate fa background and legal standard."""
    filename, error_message = handle_file_upload(request, STATIC_FOLDER)
    if error_message:
        print(error_message)
        return {'message': error_message}, 401

    doc_id = Drafts.resolve_id(request.form['draftId'])
    memory = DBMemory(db=db, doc_id=doc_id)
    clear_memory_keys(memory, DISMISS_RESET_KEYS)
    memory['inputs/uploaded_filename'] = filename

    # Call state0 as a Celery task to process the file
    result = state0.delay(filename=filename, draftId=request.form['draftId'])
    return {"result_id": result.id}, 200  # Return the result ID of the task


@attach_step_usage(DOC_TYPE, 0, lambda request: request.form['draftId'])
def route_motion_dismiss_update_background(request):
    """
    A partial update (LLM call) that returns an updated background immediately.
    """
    data = request.form
    doc_id = Drafts.resolve_id(data['draftId'])

    # OPTIONAL: If you also need additional facts that the user typed, 
    # they might be in data['additionalFact'] or data['fas']. 
    # For example:
    new_fas_text = data['newfas'] if 'newfas' in data else ""
    print("NEW facts as text: ", new_fas_text)

    # NOTE: Adjust to match your actual DBMemory usage:
    mem = DBMemory(db=db, doc_id=doc_id)

    # Possibly do your LLM call to refine background or add more text
    # For example:
    fa_bg_comand = open("backend/ai/prompts/dismiss/SUGGEST_FACT_BG").read()
    fa_bg_comand_filled = fa_bg_comand.format(
        summary = mem['inputs/fa_sums'],
        case_info=mem['inputs/case_info'],
    )
    curr_bg = data['background']
    update_bg_comand = open("backend/ai/prompts/dismiss/UPDATE_FACT_BG").read()
    update_bg_comand_filled = update_bg_comand.format(user_input = new_fas_text)

    output = multichat_completion_parse(
        [(fa_bg_comand_filled, curr_bg)],
        update_bg_comand_filled,
        response_model=UpdatedFactualBackground,
        model=ai_base.DEFAULT_MODEL,
        reasoning_effort="high",
    )
    if output is None:
        return {"message": "Failed to update background from LLM"}, 502

    # constructure background from the json structured llm output
    fa_bg_str = ""
    for sub in output.background:
        if sub.sub_heading:
            fa_bg_str += f"\n**{sub.sub_heading}**\n"   #subheading use ** consistently, will be used later in pdf drafter
        for para in sub.paragraphs:
            fa_bg_str += para + '\n'
    if not fa_bg_str.strip():
        return {"message": "Failed to update background from LLM"}, 502
    print(f"=== UPDATED fa background ===\n", fa_bg_str)    

    # save updated fa background
    mem['drafted_sections/fa_background'] = fa_bg_str

    # Return the updated background to the front-end
    return {
        "background": fa_bg_str,
        "message": "Updated background from LLM"
    }, 200
       

@shared_task(ignore_result=False)
@guided_step_operation(DOC_TYPE, 1, lambda data: data['draftId'])
def state1(data: dict) -> dict:
    '''1. parse out each coa, summarize each, 2. then do element analysis, 3. suggest grounds
    '''
    # 'file_name': f'{API_URL}static/{filename}'
    for k,v in data.items():
        print(f"key={k}; value={v}")

    doc_id=Drafts.resolve_id(data['draftId'])
    mem = DBMemory(db=db, doc_id=doc_id)

    if 'background' in data.keys():
        mem['drafted_sections/fa_background'] = data['background']
        print("Save user edit fa_background to database")
    
    if 'standard' in data.keys():
        print("Use approved standard: ", data['standard'])
        mem['drafted_sections/legal_standard'] = data['standard']
        print("Saved user edited legal standard to database")
    
    text_by_lines = json.loads(mem['complaint_text'])
    print("Retrieved text_by_lines: ", len(text_by_lines))
    # memory['inputs/fa_sums'] = json.dumps(fa_sums)
    fa_sums = json.loads(mem['inputs/fa_sums'])
    case_info_str = mem['inputs/case_info']

    coa_dict = json.loads(mem['inputs/coa_sums'])
    print("retrieve coa_dict of len: ", len(coa_dict))
    for val in coa_dict.values(): 
        print(val['title']) 

    #    == suggest element analysis for each cause of action
    element_command = open("backend/ai/prompts/dismiss/EVAL_ELEMENT").read()

    # NEW thread-based implementation
    futures_map = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
        for i, val in coa_dict.items():
            print("*** Start Element step for key coa", val['title'])
            element_command_filled = element_command.format(
                case_info=case_info_str,
                fact=fa_sums,
                coa='\n'.join([val['title'], val['description']])
            )
            if int(i) == 0:
                print("== EVAL_ELEMENT filled_in_prompt==\n", element_command_filled)
            fut = submit_with_context(pool, chat_completion_j, element_command_filled,
                                      model=ai_base.DEFAULT_MODEL, reasoning_effort="high")
            futures_map[fut] = str(i)
        print(f"-- EVAL_ELEMENT created {len(getattr(pool, '_threads', []))} threads")

        for fut in concurrent.futures.as_completed(futures_map):
            id = futures_map[fut]
            try:
                output = fut.result()
                print("decoding id: ", id)
                llm_elements = extract_from_json('legal elements', output)
                print("# llm_elements: ", len(llm_elements))
                coa_dict[id]['elements'] = llm_elements
            except Exception as e:
                print(f"Error decoding COA-element output (id={id}): ", e)
                coa_dict[id]['elements'] = []

    print("coa_dict After element analysis and before grounds: \n", coa_dict)

    #  == Suggest legal grounds for each COA  based on elements analysis
    ground_command = open("backend/ai/prompts/dismiss/SUGGEST_GROUND").read()

    futures_map = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
        for i, val in coa_dict.items():   # recall i is str
            print("*** Start grounds for coa: ", val['title'])
            ground_command_filled = ground_command.format(
                case_info=case_info_str,
                fact=fa_sums,
                coa='\n'.join([val.get('title', ''), val.get('description', '')]),
                elements=json.dumps(val.get('elements', []))
            )
            if int(i) == 0:
                print("== SUGGEST_GROUND filled_in_prompt ==\n", ground_command_filled)
            fut = submit_with_context(pool, chat_completion_j, ground_command_filled,
                                      model=ai_base.DEFAULT_MODEL, reasoning_effort="high")
            futures_map[fut] = str(i)
        print(f"-- SUGGEST_GROUND created {len(getattr(pool, '_threads', []))} threads")

        for fut in concurrent.futures.as_completed(futures_map):
            id = futures_map[fut]
            try:
                output = fut.result()
                print("decoding id: ", id)
                coa_dismisal_heading = extract_from_json("dismissal_heading", output)
                if isinstance(coa_dismisal_heading, str):
                    coa_dict[id]['dismissal_heading'] = coa_dismisal_heading.upper()
                else:
                    coa_dict[id]['dismissal_heading'] = ''

                output_grounds = extract_from_json('legal grounds', output)
                print("Output grounds: ", output_grounds)
                coa_dict[id]['grounds'] = output_grounds
                print(f"len of coa_dict id {id} grounds : {len(output_grounds)}.")
            except Exception as e:
                print(f"Error decoding COA-ground output (id={id}): ", e)
                coa_dict[id]['dismissal_heading'] = ''
                coa_dict[id]['grounds'] = []

    # Apply filter function to 
    tasks = []
    for coa_id, coa_val in coa_dict.items():   # coa_id is a str
        grounds = coa_val.get('grounds', [])
        for ground_idx, each_ground in enumerate(grounds):
            citations = each_ground.get('citations', [])  # raw_cit is the dictionary with "case_name":"...", "citation":"....", "year":"...", "parenthetical"
            for raw_cit in citations:
                tasks.append({
                    'coa_id': int(coa_id),
                    'ground_idx': ground_idx,
                    'raw_citation': raw_cit
                })

    # Filter out malformed raw_citation entries missing 'citation'
    bad_tasks = [t for t in tasks if not (isinstance(t.get('raw_citation'), dict) and t['raw_citation'].get('citation'))]
    if bad_tasks:
        print(f"Skipping {len(bad_tasks)} raw_citation entries without citation field. Sample:", bad_tasks[:3])
    
    filtered_tasks = [t for t in tasks if isinstance(t.get('raw_citation'), dict) and t['raw_citation'].get('citation')]

    # spin up a pool of threads  ->   doing # citations_info_filtered = filter_raw_citations(str(raw_citation_form))
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        # submit each raw_citation to filter_raw_citations
        future_map = {
            pool.submit(filter_raw_citations,  str(t['raw_citation'].get('citation', ''))): t  for t in filtered_tasks}
        # we'll collect back into a structure keyed by (coa_id, ground_idx)
        citation_results = {}  # (coa_id,ground_idx) -> list of filtered dicts

        
        for fut in concurrent.futures.as_completed(future_map):
            task = future_map[fut]
            coa_id = task['coa_id']   # int , should -> str
            ground_idx= task['ground_idx']
            raw_citation = task['raw_citation']
            citation_str = raw_citation.get('citation', 'N/A')
            print("collect fut result for: ", coa_id, ground_idx, citation_str)
            try:
                before_case_filtered_citations_info = fut.result()  # list of dict
                # further filter on zero_common_word case names
                filtered_citations_info = filter_on_casename(raw_citation, before_case_filtered_citations_info)
                print("# of filtered citations before filter_on_casename: ", len(before_case_filtered_citations_info))
                print("# of filtered citations after filter_case_name: ", len(filtered_citations_info))
                ######

            except Exception as exc:
                filtered_citations_info = []  # or handle error
                print(f"Task (coa_id, ground_id) = ({coa_id}, {ground_idx}), raw_citation = {raw_citation}  Failed: {exc}")
            
            key = (coa_id, ground_idx)
            entry = citation_results.setdefault(key, {'filtered_raw_citations':[], 'citations_info':[]})   # 'citations_info' is a list of list(mostly len=1)
            if filtered_citations_info:
                # 1) keep the original raw citation dict
                entry['filtered_raw_citations'].append(raw_citation)
                print("entry filtered_raw_citations append: ", raw_citation)
                # 2) add each filtered dict into the list
                if len(filtered_citations_info) == 1:
                    entry['citations_info'].append(filtered_citations_info)
                    print("if entry citations_info extend: ", filtered_citations_info)
                else:
                    # if more than one citation found, sort in place by start_index   (should not happen)
                    filtered_citations_info.sort(key=lambda x: x['start_index'])
                    print("else entry citations_info extend: ", filtered_citations_info)
                    entry['citations_info'].append(filtered_citations_info)

    # 3) now re-inject into your coa_dict
    for (coa_id, ground_idx), result in citation_results.items():
        
        coa_dict[str(coa_id)]['grounds'][ground_idx]['citations_info'] = result['citations_info']
        citation_jstr_list = []
        annotated_citation_jstr_list = []
        for i, raw_cit in enumerate(result['filtered_raw_citations']):  # loop filtered_raw_citations for this coa, ground
            raw_citation_form = raw_cit.get('citation', '')
            llm_case_name = raw_cit.get('case_name', '')
            parenthetical = raw_cit.get('parenthetical', '')
            print("raw_citation form: ", raw_citation_form)
            citation_jstr_list.append(f"{llm_case_name}, {raw_citation_form} {parenthetical}")

            annotated_result = raw_citation_form
            offst = 0
            if i < len(result['citations_info']):
                print(f"len of result['citations_info'][i]: {len(result['citations_info'][i])}")
                for citation_info in result['citations_info'][i]:  # this raw_citation corresponding info list (one raw_citation -> [])
                    annotated_result, next_offset = insert_tags(annotated_result, citation_info, offst)
                    offst += next_offset
            
            # annotated_full_str = ' '.join([case_name, annotated_result, parenthetical])
            # here we are sure about the case_name so we want to include it inside the <href> tags inside the annotated_result. just extend the start anchor to include raw_cit.get('case_name')
            # Match the opening <a> tag by looking for the specific format from insert_tags
            # Format is: <a href="..." target="_blank" rel="noopener noreferrer">
            # Handle case where URL contains < and > like "523-F<dot>"
            areft_tag_regex = r'<a\s+href="[^"]*"\s+target="_blank"\s+rel="noopener noreferrer">'
            to_replace = re.search(areft_tag_regex, annotated_result)
            if to_replace:
                print(f"expanding the anchor to include the case_name")
                print(f"matched part: {to_replace.group(0)}")
                if llm_case_name:
                    tag_plus_casename = to_replace.group(0) + ' ' + str(llm_case_name) + ', '
                    annotated_result = annotated_result.replace(to_replace.group(0), tag_plus_casename)
                
                # else: # if this is no llm case name, just use the annotated_result
                annotated_full_str = ' '.join([
                    annotated_result,
                    str(raw_cit.get('parenthetical', ''))
                ]).strip()
            else:
                print("no matched part found.")
                # Build full HTML citation string without expanding anchor
                annotated_full_str = ' '.join([
                    annotated_result,
                    str(raw_cit.get('parenthetical', ''))
                ]).strip()

            print("annotated full string: ", annotated_full_str)
            annotated_citation_jstr_list.append(annotated_full_str)

        coa_dict[str(coa_id)]['grounds'][ground_idx]['citations'] = citation_jstr_list
        coa_dict[str(coa_id)]['grounds'][ground_idx]['annotated_citations'] = annotated_citation_jstr_list
        # Initialize selected_citations as all citations selected by default
        coa_dict[str(coa_id)]['grounds'][ground_idx]['selected_citations'] = citation_jstr_list

    # save all info about coa, description, elements, grounds
    mem["coa_dict"] = json.dumps(coa_dict)
    # print('coa_dict_list: \n', [val for val in coa_dict.values()])

    return {"coa_dict_list":[val for val in coa_dict.values()],
        "message": "Form processed successfully"}


def update_citation_selections_dismiss(data: dict) -> dict:
    """
    Update the stored COA dict to keep only selected citations.
    Expected data keys:
    - draftId: int
    - citationSelections: dict with keys like "0-0" mapping to arrays of booleans
    """
    doc_id = Drafts.resolve_id(data['draftId'])
    citation_selections = data.get('citationSelections', {})
    print("=== update_citation_selections_dismiss ===")
    print("citation_selections: ", citation_selections)
    
    mem = DBMemory(db=db, doc_id=doc_id)
    
    # Load the COA dict
    coa_dict = json.loads(mem.get("coa_dict", "{}"))
    print("original coa_dict: ") 
    print(json.dumps(coa_dict, indent=4))
    
    # Update each ground's citations based on selections
    for key, selections in citation_selections.items():
        if not selections:
            continue
        
        parts = key.split('-')
        if len(parts) != 2:
            continue
        coa_id, ground_idx = parts
        coa_id = str(coa_id)
        ground_idx = int(ground_idx)
        
        if coa_id in coa_dict and ground_idx < len(coa_dict[coa_id]['grounds']):
            ground = coa_dict[coa_id]['grounds'][ground_idx]
            
            # Build selected_citations based on selections
            if 'citations' in ground and isinstance(ground['citations'], list):
                original_citations = ground['citations']
                selected_citations = [
                    cit for i, cit in enumerate(original_citations)
                    if i < len(selections) and selections[i]
                ]
                ground['selected_citations'] = selected_citations
                print(f"Updated selected_citations for COA {coa_id}, ground {ground_idx}: {selected_citations}")
    
    # Save updated COA dict back to memory
    mem["coa_dict"] = json.dumps(coa_dict)
    print("updated coa_dict: ")
    print(json.dumps(coa_dict, indent=4))
    print(f"Updated citation selections for draft {doc_id}")
    return {"message": "Citation selections updated successfully"}


def handle_state1(request):
    result = state1.delay(request.form)
    return {"result_id": result.id}, 200


def alphanumeric_only(text: str) -> str:
        # Extract only alphanumeric characters and convert to lowercase
        return ''.join(re.findall(r'[A-Za-z0-9]+', text)).lower()

def extract_citations_with_openai(text: str) -> list[str]:
    prompt = """You are asked to assist with creating the table of authorities for the following legal document. Your ask is to Extract all cited cases, statues, and rules from the following legal argument text.
Output only a list of unique authorities (case names, statutes, rules) with citations exactly as shown in the following document, \
    no explanations or any other text. for example, 
    1. example for cases: 'AlterG, Inc. v. Boost Treadmills LLC,  388 F.Supp.3d 1133 (N.D. Cal. 2019)'
    2. example for statues: '15 U.S.C. §1125(a)'
    3. example for rules: 'Fed. R. Civ. P. 8'
Important note: You may output empty list if there is no qualified items, but DO NOT make up cases or laws.
==
Document:
{text}

== Output the list of unique authorities, group into cases, statues, and rules, using the following JSON format:
{{"cases":['...', '...', '...',...], "statutes":['...','...',...], rules:['...','...',...]}}

"""
    response = chat_completion_j(prompt.format(text))
    cases_llm = extract_from_json('cases', response)
    statutes_llm = extract_from_json('statutes', response)
    rules_llm = extract_from_json('rules', response)
    return ['Cases'] + sorted(cases_llm) + ['Statutes'] + sorted(statutes_llm) + ['Rules'] + sorted(rules_llm)


@shared_task(ignore_result=False)
@guided_step_operation(DOC_TYPE, 2, lambda data: data['draftId'])
def state2(data: dict) -> dict:
    '''Draft section by section, save previous edited arguments, and update PDF after each.'''
    doc_id = Drafts.resolve_id(data['draftId'])
    mem = DBMemory(db=db, doc_id=doc_id)

    coa_dict = json.loads(mem['coa_dict'])
    # print("coa_dict ", coa_dict)
    # loads previous saved analysis
    fa_sums = json.loads(mem['inputs/fa_sums'])
    case_info_str = mem['inputs/case_info']
    for k,v in data.items():
        print(f"key={k}; value={v}")
    
    if 'batchIndex' not in data.keys():  # this means it is from step2 to step3, calling this for the first time
        # FIRST TIME into State2: Create batches according to user ignore choice
        ignore_set = set()
        for k,v in data.items():
            if k.startswith('ignore'):
                parts = k.split('-')
                if len(parts) == 3:
                    _, coaIndex, groundIndex = parts
                    ignore_set.add((int(coaIndex), int(groundIndex)))
        print("ignore set: ", ignore_set)

        batch_ids = []
        ground_headings = []
        for coa_id, coa_value in coa_dict.items():
            grounds = coa_value.get('grounds', [])
            for ground_idx, ground_data in enumerate(grounds):  # it's a list
                # print("coa_id: ", coa_id, type(coa_id), "; ground_idx: ", ground_idx, type(ground_idx))
                if (int(coa_id), ground_idx) not in ignore_set:
                    batch_ids.append((int(coa_id), ground_idx))
                    # ground_headings.append(ground_data.get('dismissal', ''))
                    ground_headings.append(ground_data.get('legal_ground', f'Ground {ground_idx + 1}'))

        print("batch ids: ", batch_ids)

        # Create a list of tuples for sorting
        combined_batches = list(zip(batch_ids, ground_headings))
        # Sort based on the compound key (coa_id, ground_idx)
        combined_batches.sort(key=lambda x: x[0])  # Sort by the first element of the tuple (batch_id)
        # Create the batches dictionary using the sorted list, Tip: use string key for anything that will be serialized into JSON
        batches = {str(idx): {"id": batch_id, "sub_heading": ground_hd} for idx, (batch_id, ground_hd) in enumerate(combined_batches)}
        mem['drafted_sections/batches'] = json.dumps(batches)

        # # Save citations if any edited
        # for k, v in data.items():
        #     if k.startswith('citations-'):   #  name={`citations-${coaIndex}-${groundIndex}`} save user edited citations 
        #         _, coa_idx, grd_idx = k.split('-')
        #         coa_dict[coa_idx]['grounds'][int(grd_idx)]['citations'] = v
        #         print(f"Saving citations to batch coa={coa_idx}, ground={grd_idx}: {v}")
        # mem['coa_dict'] = json.dumps(coa_dict)  

        batchIndex = 0   # assign 0 for the first time
        print("Initialized batches; total =", len(batches))
        
    else:  # Normal flow: if batchIndex is in data, which means first batch has processed and we are on the second or later

        batchIndex = int(data['batchIndex'])  # find the current batch location we are at corresponding to the frontend
        print("batchIndex in data keys: ", batchIndex)
        batches = json.loads(mem['drafted_sections/batches'])  # retreive the batches, note here idx will be interpreted as str, not int after json.loads {idx: {"id": batch_id, "sub_heading": ground_hd}
        print("loaded batches keys: ", batches.keys())

    # end of batchIndex config logic, main body
    total_batches = len(batches)
    filename = Drafts.pdf_filename(data['draftId'])
    mem['inputs/generated_pdf_filename'] = filename

    #  construct a map that map from coa_id to order number, for later generating roman number for coa heading
    batch_ids_l = []
    for _, batch in batches.items():    # {str(idx): {"id": batch_id, "sub_heading": ground_hd}
        batch_ids_l.append(batch['id'])
    # 1. Extract the first elements, take a set, sort it
    unique_sorted = sorted({first for first, _ in batch_ids_l})
    # 2. Enumerate starting from 1 to build your map
    coa_order_map = {coa_id_num: idx for idx, coa_id_num in enumerate(unique_sorted, start=1)}
    print("coa_order_map: ", coa_order_map)


    # save previous user edited argument from the last step into database if batchIndex > 0
    if batchIndex == 0:
        # For first batch, just use the existing filename
        print("first batch, just include legal standard in pdf")
        print(mem['drafted_sections/legal_standard'])
        fn = create_dismiss_pdf_no_toc(
            fn=f"backend/static/{filename}",
            plaintiff=mem.get('inputs/plaintiff', "specify plaintiff"),
            defendant=mem.get('inputs/defendant', "specify defendant"),
            attorneys_contact_info=mem.user.contact_info or "No Attorneys_contact_info",
            venue=mem.get('inputs/venue', "Venue not yet specified"),
            introduction='',
            background=mem.get('drafted_sections/fa_background', ''),
            standard= mem.get('drafted_sections/legal_standard', ''),
            arguments='',
            conclusion=''
        )
    else:   
        # retrieve previous section argument edit from frontend form data, update the pdf
        prev_batch_idx = batchIndex - 1
        # print(f"backend calculated prev_batch_idx: {prev_batch_idx}  should == frontend sent prev_batch_index: {data['prev_batch_index']}")
        if 'argument' in data.keys():
            batches[str(prev_batch_idx)]['argument'] = data['argument']
            print(f"Saving user verified argument to batch {prev_batch_idx}")
        
        mem['drafted_sections/batches'] = json.dumps(batches)  # Save the updated batches back to memory

        drafted_batches_arguments = []
        prev_coa_idx = -1
        print("prepare PDF argument up to batch, ", batchIndex)

        drafted_batches_citations = []
        prev_roman_i = 0
        for idx, batch in batches.items():
            ##
            if int(idx) < int(batchIndex):  # collect so-far generated batch arguments
                print("add batch idx: ", idx)
                batch_id = batch.get('id', [])
                if not batch_id:
                    continue
                current_coa_idx = batch_id[0]
                dismissal_heading = coa_dict.get(str(current_coa_idx), {}).get('dismissal_heading', '')
                heading = f"**{int_to_roman(prev_roman_i+1)}. {dismissal_heading}**" if int(current_coa_idx) > prev_coa_idx and dismissal_heading else ''
                batch_argument = batch.get('argument', '')
                drafted_batches_arguments.append((heading, batch_argument))
                if int(current_coa_idx) > prev_coa_idx:
                    #update prev_roman counter
                    prev_roman_i += 1
                # update prev_coa_idx
                prev_coa_idx = int(current_coa_idx)

                # print("batch: ", batch)
                # load final_citations 
                final_citations = batch.get('final_citations', [])
                if final_citations:
                    citations = []
                    for cit_str in final_citations:
                        try:
                            citations.append(json.loads(cit_str))
                        except json.JSONDecodeError:
                            print(f"Warning: Failed to parse citation JSON: {cit_str}")
                    # Example: print all citation names in this batch
                    for citation in citations:
                        display_name = citation.get('displayName', '')
                        if display_name:
                            parts = display_name.split('\t')
                            if len(parts) == 2:
                                drafted_batches_citations.append(f"{parts[0]} ({parts[1]})")
                            else:
                                drafted_batches_citations.append(parts[0])

        mem['drafted_sections/arguments'] = '\n'.join('\n\n'.join(item) for item in drafted_batches_arguments)
        print("PDF so-far generated batch arguments LENGTH: ", len(drafted_batches_arguments))
        print("drafted_batches_citations set: ", set(drafted_batches_citations))

        fn = create_dismiss_pdf_no_toc(
            fn=f"backend/static/{filename}",
            plaintiff=mem.get('inputs/plaintiff', "specify plaintiff"),
            defendant=mem.get('inputs/defendant', "specify defendant"),
            attorneys_contact_info=mem.user.contact_info or "No Attorneys_contact_info",
            venue=mem.get('inputs/venue', "Venue not yet specified"),
            introduction='',
            background=mem.get('drafted_sections/fa_background', ''),
            standard = mem.get('drafted_sections/legal_standard', ''),
            arguments= mem.get('drafted_sections/arguments', ''),
            conclusion=''
        )

        # fn = create_dismiss_pdf_with_toa(
        #     fn=f"backend/static/{filename}",
        #     plaintiff=mem.get('inputs/plaintiff', "specify plaintiff"),
        #     defendant=mem.get('inputs/defendant', "specify defendant"),
        #     attorneys_contact_info=mem.user.contact_info or "No Attorneys_contact_info",
        #     venue=mem.get('inputs/venue', "Venue not yet specified"),
        #     introduction='',
        #     background=mem.get('drafted_sections/fa_background', ''),
        #     standard = mem.get('drafted_sections/legal_standard', ''),
        #     arguments= mem.get('drafted_sections/arguments', ''),
        #     conclusion='',
        #     authority_list=sorted(drafted_batches_citations))

    # consider whether this batchIndex is the last batch
    # if batchIndex == total_batches:

    # generate new argument 
    batch = batches.get(str(batchIndex), {})
    batch_id = batch.get('id')
    if not batch_id or len(batch_id) < 2:
        return {"message": f"Invalid batch {batchIndex}", "error": "Missing batch id"}
    coa_id, ground_idx = batch_id[0], batch_id[1]
    coa_id = str(coa_id)
    coa_heading = coa_dict.get(coa_id, {}).get('dismissal_heading', '')
    print("Retrieved coa dismissal header: ", coa_heading)
    print("coa_id = ", coa_id)
    coa_order_num = coa_order_map.get(int(coa_id), 0)
    print("coa_order_num: ", coa_order_num)

    if 'argument' in batch:
        print(f"Arguemnt for this batch {batchIndex} already exits")
        sec_argument = batch['argument']
        raw_list = batch.get('final_citations', [])  # this is already a Python list
        # rehydrate each element
        citation_info_filtered = []
        for s in raw_list:
            try:
                citation_info_filtered.append(json.loads(s))
            except json.JSONDecodeError:
                print(f"Warning: Failed to parse citation JSON: {s}")
        # citation_info_filtered = json.loads(batch['final_citations'])   #WRONG
        print(f"Loaded existing argument for batch {batchIndex}")

    else:
        # Draft new argument
        draft_command = open("backend/ai/prompts/dismiss/DRAFT_SECTION").read()
        
        coa_data = coa_dict.get(coa_id, {})
        grounds = coa_data.get('grounds', [])
        if ground_idx >= len(grounds):
            return {"message": f"Invalid ground index {ground_idx} for COA {coa_id}", "error": "Invalid ground index"}
        
        ground_data = grounds[ground_idx]
        selected_citations = ground_data.get('selected_citations', [])
        citations_lines = '\n'.join(selected_citations) if selected_citations else ''
        
        coa_title = coa_data.get('title', '')
        coa_description = coa_data.get('description', '')
        legal_ground = ground_data.get('legal_ground', '')
        ground_description = ground_data.get('description', '')
        
        batch_coa_ground_str = f'''Cause of action: {coa_title}\nDescription: {coa_description}\n
        Legal Ground for dismissal: {legal_ground}\n
        Description: {ground_description}\n
        == Use approved Supporting Citations for this dismissal 
        (Do NOT use other citations in your answer), if there is no citations provided below, make the best argument without specify legal citations \n
        If it is the first time to cite any of the following cases, stick to the exact same case names and citations provided as followng. for later reference, you may use short forms, or variation of the casename.===:\n
        {citations_lines}'''

        draft_command_filled = draft_command.format(
                case_info = case_info_str,
                facts = '\n'.join(fa_sums),
                batch_coa_ground = batch_coa_ground_str,
                sub_heading = batch['sub_heading']
        )
        print("=== draft_command_filled ===\n", draft_command_filled)
        returned = chat_completion_j(draft_command_filled, model=ai_base.DEFAULT_MODEL,
                                     reasoning_effort="high")
        sec_argument = extract_from_json('section arguments', returned)
        # print("SECTION ARGUMENT: \n", sec_argument)

        # look for citation and infos after the first draft
        citation_info_filtered = filter_raw_citations(str(sec_argument))
        print(f"found {len(citation_info_filtered)} citations in 1st draft")
        print(citation_info_filtered)

        citation_displaynames = [cit_info.get('displayName', '') for cit_info in citation_info_filtered]
        print(f"Filtered {len(citation_displaynames)} citation forms: ", citation_displaynames)

        batch['argument'] = sec_argument
        batches[str(batchIndex)]['final_citations'] = [json.dumps(cit) for cit in citation_info_filtered]
        # Save results
        mem['drafted_sections/batches'] = json.dumps(batches)

    # ---- NORMAL RETURN (still editing)  ----
    mem['drafted_sections/current_batch_index'] = str(batchIndex)
    return {
        'message': 'Batch argument generated successfully',
        'all_batches_done': int(batchIndex) + 1 == total_batches,
        'total_batches': total_batches,
        'batch_id': batchIndex,
        'batch_heading': f"{int_to_roman(coa_order_num)}. {coa_heading}",
        'batch_argument': batch.get('argument', ''),
        'batch_citations':citation_info_filtered,
        'file_name': f'{API_URL}static/{filename}'
        }


def handle_state2(request):
    result = state2.delay(request.form)
    return {"result_id": result.id}, 200


def route_motion_dismiss_expand_argument(data:dict):
    result = motion_dismiss_expand_argument.delay(data)
    return {"result_id": result.id}, 200


@shared_task(ignore_result=False)
@attach_step_usage(DOC_TYPE, 2, lambda data: data['draftId'])
def motion_dismiss_expand_argument(data: dict) -> dict:
    """
    LLM call to expand/revise an argument with additional facts.
    Uses the same selected_citations from state1 (no new citation generation).
    """
    print("inside route_motion_dismiss_expand_argument")
    for k, v in data.items():
        print(f"data key = {k}; value = {v}")
    
    batchIndex = str(data.get('batchIndex', ''))
    print("Retrieved batchindex = ", batchIndex)
    curr_arg = data.get('argument', '')

    doc_id = Drafts.resolve_id(data.get('draftId', 0))
    mem = DBMemory(db=db, doc_id=doc_id)
    
    coa_dict = json.loads(mem.get('coa_dict', '{}'))
    fa_sums = json.loads(mem.get('inputs/fa_sums', '[]'))
    case_info_str = mem.get('inputs/case_info', '')
    batches = json.loads(mem.get('drafted_sections/batches', '{}'))
    
    batch_data = batches.get(str(batchIndex), {})
    batch_id = batch_data.get('id', [])
    if not batch_id or len(batch_id) < 2:
        return {"message": f"Invalid batch {batchIndex}", "error": "Missing batch id"}
    
    coa_id, ground_idx = batch_id[0], batch_id[1]
    coa_id = str(coa_id)
    coa_data = coa_dict.get(coa_id, {})
    coa_heading = coa_data.get('dismissal_heading', '')
    print("Retrieved coa dismissal header: ", coa_heading)

    # Get selected citations for this ground
    grounds = coa_data.get('grounds', [])
    if ground_idx >= len(grounds):
        return {"message": f"Invalid ground index {ground_idx} for COA {coa_id}", "error": "Invalid ground index"}
    
    ground_data = grounds[ground_idx]
    selected_citations = ground_data.get('selected_citations', [])
    citations_str = '\n'.join(selected_citations) if selected_citations else ''
    print(f"Using {len(selected_citations)} selected citations for expansion")

    coa_title = coa_data.get('title', '')
    coa_description = coa_data.get('description', '')
    legal_ground = ground_data.get('legal_ground', '')
    ground_description = ground_data.get('description', '')
    
    # Build batch COA ground string with citations
    batch_coa_ground_str = f'''Cause of action: {coa_title}\nDescription: {coa_description}\n
        Legal Ground for dismissal: {legal_ground}\n
        Description: {ground_description}\n
        
        == Use approved Supporting Citations for this dismissal: ====\n
    ** Do NOT use other citations in your answer ** 
    ** if there is no citations provided below, make the best argument without specify legal citations **\n
    {citations_str}'''

    # Step 1: Generate additional facts specific to this ground
    section_fa_comand = open("backend/ai/prompts/dismiss/SUGGEST_BATCH_FA").read()
    fa_chunks = split_into_chunks(fa_sums)
    tasks = []
    for i, chunk in enumerate(fa_chunks):
        section_fa_comand_filled = section_fa_comand.format(
                batch_coa_ground = batch_coa_ground_str,
                facts = '\n'.join(chunk))
        tasks.append((i, section_fa_comand_filled))
    
    tasks_outputs = [[] for _ in range(len(tasks))]
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
        future_map = {
            submit_with_context(pool, chat_completion_j, task[1], model=ai_base.DEFAULT_MODEL,
                                reasoning_effort="medium"): task for task in tasks}
        sleep(1)
        print(f"-- SUGGEST_BATCH_FA created {len(pool._threads)} threads")
        for fut in concurrent.futures.as_completed(future_map):
            task = future_map[fut]
            task_id = task[0]
            try:
                output = fut.result()
                fa_l = extract_from_json("Relevant Facts", output)
                tasks_outputs[task_id] = fa_l
            except Exception as exc:
                print(f"-- SUGGEST_BATCH_FA task failed with exception: {str(exc)}")
    
    # Flatten additional facts
    batch_facts = [each_fa for fas in tasks_outputs for each_fa in fas]
    print("# total batch_facts from sub-processes: ", len(batch_facts))

    # Step 2: Revise argument with additional facts and selected citations
    revise_command = open("backend/ai/prompts/dismiss/REVISE_SECTION").read()
    revise_command_filled = revise_command.format(
        batch_argument = curr_arg,
        additional_facts = '\n'.join(batch_facts),
        coa_ground = batch_coa_ground_str,
        citations = citations_str,
    )
    print("=== REVISE Argument filled in prompt ===\n", revise_command_filled)
    
    returned = chat_completion_j(revise_command_filled, model=ai_base.DEFAULT_MODEL,
                                 reasoning_effort="high")
    sec_argument_revised = extract_from_json('section arguments', returned)
    print("REVISED ARGUMENT: \n", sec_argument_revised[:200])

    # Persist the expanded argument so it survives a page reload
    batches[str(batchIndex)]['expanded_argument'] = sec_argument_revised
    mem['drafted_sections/batches'] = json.dumps(batches)

    # Return the revised argument (citations remain unchanged)
    return {
        'expanded_argument': sec_argument_revised,
        'expanded_citations_info': [],  # No new citations
        "message": "Argument expanded with additional facts"
    }


@shared_task(ignore_result=False)
@guided_step_operation(DOC_TYPE, 3, lambda data: data['draftId'])
def state3(data: dict) -> dict:
    '''Suggest intro and conclusion'''
    # 'file_name': f'{API_URL}static/{filename}'

    doc_id=Drafts.resolve_id(data.get('draftId', 0))
    mem = DBMemory(db=db, doc_id=doc_id)
    # loads previous saved analysis
    fa_background = mem.get('drafted_sections/fa_background', '')
    case_info_str = mem.get('inputs/case_info', '')
    coa_dict = json.loads(mem.get('coa_dict', '{}'))

    batches = json.loads(mem.get('drafted_sections/batches', '{}'))
    print("batches keys: ", batches.keys())  # str ['0', '1']
    # collect the last section argument

    print("Data received for state3")
    for k, v in data.items():
        print(k)
        print(v)
    prev_idx = data.get('prev_batch_index', '0')   # this should the last batch
    if str(prev_idx) in batches:
        batches[str(prev_idx)]['argument'] = data.get('argument', '')
        print(f"Saving user verified argument to batch {prev_idx}")   
    else:
        print(f"Warning: batch {prev_idx} not found in batches")

    mem['drafted_sections/batches'] = json.dumps(batches)

    drafted_batches_arguments = []
    prev_coa_idx = -1
    prev_roman_i = 0
    for idx, batch in batches.items():
        print("add batch idx: ", idx)
        batch_id = batch.get('id', [])
        if not batch_id:
            continue
        current_coa_idx = batch_id[0]
        dismissal_heading = coa_dict.get(str(current_coa_idx), {}).get('dismissal_heading', '')
        heading = f"**{int_to_roman(prev_roman_i+1)}. {dismissal_heading}**" if int(current_coa_idx) > prev_coa_idx and dismissal_heading else ''
        batch_argument = batch.get('argument', '')
        drafted_batches_arguments.append((heading, batch_argument))
        if int(current_coa_idx) > prev_coa_idx:
            #update prev_roman counter
            prev_roman_i += 1
        # update prev_coa_idx
        prev_coa_idx = int(current_coa_idx)


    mem['drafted_sections/arguments'] = '\n'.join('\n\n'.join(item) for item in drafted_batches_arguments)
    print("PDF so-far generated batch arguments LENGTH: ", len(drafted_batches_arguments))

    batches_info_str = "\n".join(b['sub_heading'] for b in batches.values())
    print("batches_info_str: \n", batches_info_str)
    
    intro_command = open("backend/ai/prompts/dismiss/SUGGEST_INTRO").read()
    filled_in_prompt = intro_command.format(case_info = case_info_str, fa_background = fa_background, batches_info = batches_info_str)
    print("== SUGGEST_INTRO filled_in_prompt ==\n", filled_in_prompt)

    output = chat_completion_j(filled_in_prompt, model=ai_base.DEFAULT_MODEL,
                               reasoning_effort="high")
    intro_llm = extract_from_json('introduction', output)  # list of string
    conclusion_llm = extract_from_json('conclusion', output)  # string

    # create formatted pdf so far (without intro and conclusion)
    filename = Drafts.pdf_filename(data['draftId'])
    mem['inputs/generated_pdf_filename'] = filename
    finished_fn = create_dismiss_pdf_no_toc(fn=f"backend/static/{filename}",
                                        plaintiff=mem.get('inputs/plaintiff', "specify plaintiff"),
                                        defendant=mem.get('inputs/defendant', "specify defendant"),
                                        attorneys_contact_info=mem.user.contact_info or "No Attorneys_contact_info",
                                        venue=mem.get('inputs/venue', "Venue not yet specified"),
                                        introduction='',
                                        background = mem.get('drafted_sections/fa_background', ''),
                                        standard = mem.get('drafted_sections/legal_standard', ''),
                                        arguments = mem.get('drafted_sections/arguments', ''),
                                        conclusion=''
                                        )

    return {
    'message': 'Batch argument generated successfully',
    'intro': '\n\n'.join(intro_llm), 
    'conclusion': conclusion_llm,
    'file_name': f'{API_URL}static/{filename}'
    }


def handle_state3(request):
    result = state3.delay(request.form)
    return {"result_id": result.id}, 200


@shared_task(ignore_result=False)
@guided_step_operation(DOC_TYPE, 4, lambda data: data['draftId'])
def state4(data: dict) -> dict:
    '''Save intro and conclusion, generate pdf'''
    # 'file_name': f'{API_URL}static/{filename}'

    doc_id=Drafts.resolve_id(data['draftId'])
    mem = DBMemory(db=db, doc_id=doc_id)

    for k, v in data.items():
        print('key: ', k)
        print('value: ', v)
    
    mem['drafted_sections/intro'] = data.get('introduction', '')
    mem['drafted_sections/conclusion'] = data.get('conclusion', '')

    # create formatted pdf
    draft_id = str(data['draftId'])
    filename = Drafts.pdf_filename(data['draftId'])
    mem['inputs/generated_pdf_filename'] = filename
    gen_pdf_status = create_dismiss_pdf(fn=f"backend/static/{draft_id}_without_toa.pdf",
                                        plaintiff=mem.get('inputs/plaintiff', "specify plaintiff"),
                                        defendant=mem.get('inputs/defendant', "specify defendant"),
                                        attorneys_contact_info=mem.user.contact_info or "No Attorneys_contact_info",
                                        venue=mem.get('inputs/venue', "Venue not yet specified"),
                                        introduction=mem.get('drafted_sections/intro', ''),
                                        background = mem.get('drafted_sections/fa_background', ''),
                                        standard = mem.get('drafted_sections/legal_standard', ''),
                                        arguments = mem.get('drafted_sections/arguments', ''),
                                        conclusion=mem.get('drafted_sections/conclusion', '')
                                        )

    # parse the just-drafted answer pdf file,
    drafted_text, _, _ = get_text_from_pdf_fitz(f"backend/static/{filename}")
    # print(answer_text)
    print("number of tokens in answer_text: ", num_tokens_from_string(drafted_text))
    comand_prompt = open('backend/ai/prompts/answer/HALLUCINATION_GUARD').read()
    filled_in_prompt = comand_prompt.format(text=drafted_text)
    llm_output = chat_completion_j(filled_in_prompt, model=ai_base.DEFAULT_MODEL,
                                  reasoning_effort="high")
    # print(llm_output)
    halls = extract_from_json("Findings", llm_output)

    # add TOA to the pdf
    authority_list = []
    batches = json.loads(mem['drafted_sections/batches'])
    print("batches keys: ", batches.keys())
    for k, b in batches.items():
        final_citations = b.get('final_citations', [])
        print(final_citations)
        for cit in final_citations:
            try:
                cit = json.loads(cit)
                display_name = cit.get('displayName', '')
                if display_name:
                    splits = display_name.split('\t')
                    if len(splits) > 1:
                        casename = splits[0]
                        cit_form = cit.get('form', '')
                        toa_case = f"{casename}\t({cit_form})" if cit_form else casename
                        authority_list.append(toa_case)
                    else:
                        authority_list.append(display_name)
            except json.JSONDecodeError:
                print(f"Warning: Failed to parse citation JSON: {cit}")

    # authority_list = [
    #     "Ashcroft v. Iqbal (556 U.S. 662)",
    #     "Bell Atlantic Corp. v. Twombly (550 U.S. 544)",
    #     "17 U.S.C. § 411",
    #     "A&M Records, Inc. v. Napster, Inc. (239 F.3d 1004)",
    #     "Gershwin Publishing Corporation v. Columbia Artists Management, Inc., and Community Concerts, Inc. (443 F.2d 1159)",
    #     "17 U.S.C. § 501"
    # ]
    print("len of authority list: ", len(authority_list))

    first_body_sentence = mem.get('drafted_sections/intro', '')[:30]
    print(" first body sentence: ", first_body_sentence)
    toc_pages, toa_pages = insert_toa_into_brief(
        f"backend/static/{draft_id}_without_toa.pdf", 
                          authority_list, 
                          f"backend/static/{draft_id}_with_toa.pdf", 
                          first_body_sentence)

    apply_frame_and_footers(f"backend/static/{draft_id}_with_toa.pdf",
                f"backend/static/{filename}",
                first_numbered = 0)  # same numbers returned earlier

                  
    return {'message': 'pdf successfully', 
    'file_name': f'{API_URL}static/{filename}',
    'halls': halls}


def handle_state4(request):
    result = state4.delay(request.form)
    return {"result_id": result.id}, 200


def route_motion_dismiss(request):
    data = request.form
    appState = int(data['appState'])

    if appState == 0:
        return handle_state0(request)
    elif appState == 1:
        return handle_state1(request)
    elif appState == 2:
        return handle_state2(request)
    elif appState == 3:
        return handle_state3(request)
    elif appState == 4:
        return handle_state4(request)
    else:
        raise Exception("Invalid App State in Element Checker")
    

        
