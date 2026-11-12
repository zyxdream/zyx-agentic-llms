
from backend.models import Drafts
from backend.util.analytics import attach_step_usage, guided_step_operation, submit_with_context
from backend.ai.base import split_into_chunks, num_tokens_from_string
from backend.ai import base as ai_base
from backend.ai.base import GPT_6_1_SOL_MODEL, get_coa_loc, get_headers_before_coa, extract_parties_venue, chat_completion_j, chat_completion, chat_completion_parse
from backend.util.db import DBMemory
from backend.constants import API_URL, STATIC_FOLDER
from backend.util.data_tools import (
    handle_file_upload,
    get_complaint_text_from_file,
    get_complaint_text_with_line_metadata_from_file,
    log_complaint_text_metadata_preview,
    prepare_complaint_text_for_memory,
    extract_from_json,
)
from backend.util.guided_draft_reset import ELEMENT_CHECKER_RESET_KEYS, clear_memory_keys
from backend.ai.complaint_parser import get_last_ocr_warning
from backend.ai.complaint_parser import get_text_from_pdf_fitz
from backend.util.free_law_tools import filter_raw_citations, insert_tags, filter_on_casename
from backend.database import db
from backend.word_creator import create_complaint_analysis_word
from backend.pdf_creator import create_analysis_pdf
from time import sleep
import json
from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
import concurrent.futures
from pydantic import BaseModel, Field


DOC_TYPE = "Complaint Analyzer"
DOWNLOAD_STEP = 4
UPLOAD_ANALYSIS_MODEL = "gpt-6-luna"
UPLOAD_ANALYSIS_EFFORT = "medium"
ANALYSIS_MODEL = ai_base.DEFAULT_MODEL
ANALYSIS_EFFORT = "high"
ANALYSIS_SOFT_TIME_LIMIT = 600
ANALYSIS_TIME_LIMIT = 630


class CauseOfActionSummary(BaseModel):
    cause_of_action: str = Field(alias="cause of action")
    description: list[str]


def _get_element_index_by_id(elements: list, element_id) -> int | None:
    """Return the list index for a stored element_id."""
    try:
        target_id = int(element_id)
    except (TypeError, ValueError):
        return None

    for idx, element in enumerate(elements):
        try:
            if int(element.get('element_id')) == target_id:
                return idx
        except (TypeError, ValueError, AttributeError):
            continue
    return None


def _normalize_hallucination_findings(findings: list) -> list[list]:
    """Keep Element Checker's stored and frontend findings as indexed arrays."""
    if not isinstance(findings, list):
        return []
    normalized = []
    for finding in findings:
        if isinstance(finding, dict):
            label = finding.get("label")
            description = finding.get("description")
            if isinstance(label, str) and isinstance(description, str):
                normalized.append([label, description])
        elif isinstance(finding, (list, tuple)) and len(finding) >= 2:
            normalized.append(list(finding))
    return normalized


def draft_full_analysis_word(mem: DBMemory, write_fn: str) -> str:
    """Drafts a complaint analysis from available elements in memory."""

    # create formatted pdf
    gen_word_status = create_complaint_analysis_word(write_fn,
                                        plaintiff=mem.get('inputs/plaintiff', "specify plaintiff"),
                                        defendant=mem.get('inputs/defendant', "specify defendant"),
                                        factual_allegations=mem.get('drafted_sections/fa_sum_final', ''),
                                        elements=mem.get('drafted_sections/legal_elements', ""),
                                        recomendations=mem.get('drafted_sections/recommendation_markdown', ''),
                                        side = mem.get("inputs/side", "specify side")
                                        )
    return gen_word_status

MARKDOWN_PROMPT = '''\nFor the above text, output them as markdown that can be converted into pdf, only output the markdown, no other text.
# Markdown Formatting Rules for PDF Conversion:
0. Do not and Never change the content of the original text. Just format it. 
1. Use `#` for main headings (Count titles, e.g., "Count 1: First Claim for Relief").  
2. Use `##` for element-level headings (e.g., "1. Ownership of a valid copyright").  
3. Use **bold** for labels like "Status:", "Source:", "Analysis:", "Focus of Challenge:", and "Analysis citations:", etc.
4. Use bullet points for case citations.  
5. Leave one blank line between sections to improve spacing.  
6. Do not use inline HTML or custom CSS—only plain Markdown for compatibility.  
7. Keep font size uniform—headings auto-scale in the PDF, so no manual font sizing.  
8. Separate with a horizontal rule (`---`) for clear visual breaks only when needed. Do not over-use it.
Only output the markdown, no other text. Stick to the original context that is provided. Just format it.
Again DO NOT add or re-arrange any text or content. Just format it with markdowns.
'''

LINES_PER_CHUNK_DOCX = 60
LINES_PER_CHUNK_PDF = 40

@shared_task(bind=True, ignore_result=False)
@guided_step_operation(DOC_TYPE, 0, lambda self, filename, draftId, side: draftId)
def state0(self, filename: str, draftId: str, side:str) -> dict:
    """
    - Given a complaint, check plausibility of each claim (COA)
    """
    memory = DBMemory(db=db, doc_id=Drafts.resolve_id(draftId))
    print("state0 draftId = ", draftId)

    # parse text from submitted PDF, txt, .docx or .doc
    complaint_text, _, _, line_metadata = get_complaint_text_with_line_metadata_from_file(STATIC_FOLDER + "/" + filename)
    ocr_warning = get_last_ocr_warning()
    # ocr_warning = True   # for testing error message
    if ocr_warning:
        try:
            self.update_state(state='PROGRESS', meta={
                'status': 'ocr_warning',
                'ocr_warning': ocr_warning,
                'ocr_warning_message': "Some scanned pages could not be read. Your document is still processed. Press OK to continue.",
            })
        except Exception as exc:
            print(f"Warning: failed to push OCR warning state: {exc}")
    
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

    memory["inputs/side"] = side
    memory["inputs/filename"] = filename
    # memory['inputs/case_number'] = case_number

    # extract plaintiff and defendant from complaint
    plaintiff_l, defendant_l, venue = extract_parties_venue(
        memory['complaint_cover_text'],
        model=UPLOAD_ANALYSIS_MODEL,
        reasoning_effort=UPLOAD_ANALYSIS_EFFORT,
    )
    print("plaintiff: ", plaintiff_l)
    print("# plaintiff = ", len(plaintiff_l))
    print("defendant: ", defendant_l)
    print("# defendant = ", len(defendant_l))
    print("venue: ", venue)

    memory['inputs/plaintiff'] = ";".join([each for each in plaintiff_l])
    print("memory plaintiff saved as: ", memory['inputs/plaintiff'])
    memory['inputs/venue'] = venue
    memory['inputs/defendant'] = ";".join([each for each in defendant_l])

    # defendant_l = []
    # Check if parties extraction failed (empty lists)
    if len(plaintiff_l) == 0 or len(defendant_l) == 0:
        return {"message": "error", "error_message": "Something went wrong when extracting parties from the complaint, please try again."}

    # extract FA from complaint as text lines
    # start_i, end_i = get_fa_loc(text_by_lines) # replaced by next line
    fa_start_to_end_i = get_headers_before_coa(
        text_by_lines,
        model=UPLOAD_ANALYSIS_MODEL,
        reasoning_effort=UPLOAD_ANALYSIS_EFFORT,
    )   # each split in start_end_i corresponds to a 'heading' line
    print("fa_start_to_end_i: \n", fa_start_to_end_i)
    # for each_fa_hd_loc in fa_start_to_end_i:
    #     print(text_by_lines[each_fa_hd_loc])

    # Guard against empty or insufficient fa_start_to_end_i
    if len(fa_start_to_end_i) < 2:
        print("Warning: fa_start_to_end_i has insufficient elements, using first 500 lines of complaint text")
        # Use first 500 lines of the complaint as a single FA summary
        first_500_lines = text_by_lines[:500]
        fa_sums = ['\n'.join(first_500_lines)]
    else:
        # fa_sums = []
        fa_headers_sums = [["", []] for _ in range(len(fa_start_to_end_i) - 1)]
        for j in range(len(fa_start_to_end_i)-1):
            print(f"j = {j}")
            head_line = ""
            for k in range(4):
                curr_loc = min(fa_start_to_end_i[j]+k, fa_start_to_end_i[j+1])
                print(f"fa_start_to_end_i[j]: {fa_start_to_end_i[j]}, +k {k};  fa_start_to_end_i[j+1]: {fa_start_to_end_i[j+1]}; curr_loc = {curr_loc}")
                # Guard against IndexError
                if curr_loc >= len(text_by_lines):
                    print(f"Warning: curr_loc {curr_loc} out of bounds, skipping")
                    continue
                # safely print the text_by_lines[curr_loc]
                if k == 0 or text_by_lines[curr_loc].startswith('<B>'):
                    print(f"add {text_by_lines[curr_loc]} to header {head_line}")
                    head_line += text_by_lines[curr_loc].replace('<B>', '').replace('</B>', '') + ' '

            print(f"append head_line >{head_line}< to fa_headers_sums list")        
            # fa_sums.append(head_line)
            fa_headers_sums[j][0] = head_line

        print("fa_headers_sums after adding headers: ", fa_headers_sums)    

        fa_tasks = []
        summarize_command = open("backend/ai/prompts/element_checker/FA_SUM").read()
        for j in range(len(fa_start_to_end_i)-1):
            fa_lines = text_by_lines[fa_start_to_end_i[j]: fa_start_to_end_i[j+1]]
            # split fa lines into chunk of fa lines, with 5 overlapping lines
            fa_chunks = split_into_chunks(fa_lines, chunk_len=10000)  # default 5000
            fa_headers_sums[j][1] = [[] for _ in range(len(fa_chunks))]
            print(f"establish placeholders for {len(fa_chunks)} chunks for this fa blocks j = {j}  from line {fa_start_to_end_i[j]} to line {fa_start_to_end_i[j+1]}")
            for chunk_i, chunk in enumerate(fa_chunks):
                filled_in_prompt = summarize_command.format(fa_sub=' '.join(chunk))
                fa_tasks.append({"fa_sums_j":j, "chunk_i": chunk_i, "prompt": filled_in_prompt})
            
        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
            future_map = {
                submit_with_context(
                    pool, chat_completion_j, t['prompt'],
                    model=UPLOAD_ANALYSIS_MODEL,
                    reasoning_effort=UPLOAD_ANALYSIS_EFFORT,
                ): t for t in fa_tasks}

            # pause briefly so threads have a chance to start
            sleep(1)
            # pool._threads is a set of Thread objects
            actual_threads = len(pool._threads)
            print(f"-- FA_SUM -- actually created {actual_threads} threads")

            for fut in concurrent.futures.as_completed(future_map):
                task = future_map[fut]
                fa_sums_j = task['fa_sums_j']   # int , should -> str
                chunk_i = task['chunk_i']
                # safely print the two for debugging
                print(f"Collecting fut result for fa_sums_j: {fa_sums_j}, chunk_i: {chunk_i}")
                try:
                    returned = fut.result()
                    chunk_sums = extract_from_json("summary", returned)
                except Exception as exc:
                    chunk_sums = []
                    print(f"FA sums Task for (fa_sums_j, chunk_i) = ({fa_sums_j}, {chunk_i}) Failed: {exc}")
                
                # Guard against IndexError: validate indices before assignment
                if fa_sums_j < len(fa_headers_sums) and chunk_i < len(fa_headers_sums[fa_sums_j][1]):
                    fa_headers_sums[fa_sums_j][1][chunk_i] = chunk_sums
                else:
                    print(f"Warning: Index out of bounds for fa_sums_j={fa_sums_j}, chunk_i={chunk_i}. Skipping assignment.")

        # flatten fa_headers_sums into fa_sums
        fa_sums = []
        for fa_header, fa_sums_l in fa_headers_sums:
            fa_sums.append(fa_header)
            for each_sums in fa_sums_l:
                fa_sums.extend(each_sums)

    # let llm generate a succint short summary of the fa_sums
    fa_sum_summary_comand = """You are a legal text summarizer. The user has provided a list of facts summaries of a legal complaint. Your task:
    1. Generate a succint short summary of the facts. 
    2. Do not add commentary, disclaimers, or new citations. 
    3. This summary only focuses on the factual allegations, Do not organize by cause of action.
    4. Output only the summary.
    
    {fa_sums}"""
    filled_in_prompt = fa_sum_summary_comand.format(fa_sums='\n'.join(fa_sums))
    final_sum_str = chat_completion(
        filled_in_prompt,
        model=UPLOAD_ANALYSIS_MODEL,
        reasoning_effort=UPLOAD_ANALYSIS_EFFORT,
    )
    print("final_sum_str: ", final_sum_str)
    # save final_sum_str to memory
    memory["drafted_sections/fa_sum_final"] = final_sum_str


    # ========  extract COA from complaint as list of text lines  ======
    # start_to_end_i = get_coa_loc(complaint_text)
    start_to_end_i = get_coa_loc(
        text_by_lines,
        model=GPT_6_1_SOL_MODEL,
        reasoning_effort=UPLOAD_ANALYSIS_EFFORT,
    )
    # start_to_end_i = [1383, 1437, 1488, 1515, 1554, 1585, 1633, 1808, 1900]
    coa_lines_blocks = []

    for i in range(len(start_to_end_i)-1):
        start_i = start_to_end_i[i]
        end_i = start_to_end_i[i+1]
        print("start_i: ", start_i, " end_i: ", end_i)
        coa_lines_blocks.append(text_by_lines[start_i: end_i])
        # print("append coa block to list: \n", text_by_lines[start_i], "....")

    print("number of coa_blocks: ", len(coa_lines_blocks))

    coas_sum = {}  # dictionary {0: "...", 1:"..."}
    coa_sum_comand = open("backend/ai/prompts/element_checker/COA_SUM").read()
    processes = []
    for i, block in enumerate(coa_lines_blocks):
        filled_in_prompt = coa_sum_comand.format(coa='\n'.join(block))
        if i == 0:
            print("*** coa sum filled in prompt ***\n", filled_in_prompt)
        processes.append({"coa_id": i, "prompt": filled_in_prompt})

    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
        future_map = {
            submit_with_context(
                pool, chat_completion_parse, proc['prompt'], CauseOfActionSummary,
                model=UPLOAD_ANALYSIS_MODEL,
                reasoning_effort=UPLOAD_ANALYSIS_EFFORT,
            ): proc for proc in processes}
        
        sleep(1)
        print(f"--COA_SUM-- actually created {len(pool._threads)} threads")
        for fut in concurrent.futures.as_completed(future_map):
            proc = future_map[fut]
            coa_id = proc["coa_id"]
            coa_title = None
            description_l = None
            try:
                returned = fut.result()
                if returned is not None:
                    coa_title = returned.cause_of_action
                    description_l = returned.description
            except Exception as exc:
                print(f"Task COA_SUM failed for coa_id = ({coa_id}: {exc}")

            if description_l:
                # Guard against non-list or non-string elements
                if isinstance(description_l, list):
                    coa_description = '\n'.join(str(item) for item in description_l)
                else:
                    coa_description = str(description_l)
            else:
                coa_description = "No description provided"
                
            if coa_title:
                coas_sum[coa_id] = f'{coa_title}\n{coa_description}\n'
            else:
                coas_sum[coa_id] = f'No cause of action title found.\n{coa_description}\n'

    # coas_sum = {}   # for testing error message
    if len(coas_sum) == 0:
        return {"message": "error", "error_message": "Something went wrong when analyzing the complaint, please try again."}

    memory["fa_loc"] = ','.join(map(str, fa_start_to_end_i))
    memory["coa_loc"] = ','.join(map(str, start_to_end_i))
    memory["drafted_sections/fa_sum"] = '\n'.join(fa_sums)
    sorted_coa_sum = dict(sorted(coas_sum.items()))
    # print("analysis/coa_sum saving data: \n", sorted_coa_sum)
    print("their keys are: ", sorted_coa_sum.keys())
    memory["analysis/coa_sum"] = json.dumps(sorted_coa_sum)        # contains complaint citation ((Compl. ¶40) )
    
    return {"message": "Form processed successfully",
            "fa_short": final_sum_str,
            "fas": fa_sums,
            "coas": list(sorted_coa_sum.values()),
            'file_name': f'{API_URL}static/{filename}'}


def handle_state0(request):
    """State 0 is to simply upload a file, and generate fa summary and coa summary."""
    filename, error_message = handle_file_upload(request, STATIC_FOLDER)
    if error_message:
        print(error_message)
        return {'message': error_message}, 401

    doc_id = Drafts.resolve_id(request.form['draftId'])
    memory = DBMemory(db=db, doc_id=doc_id)
    clear_memory_keys(memory, ELEMENT_CHECKER_RESET_KEYS)
    memory['inputs/filename'] = filename

    # Call state0 as a Celery task to process the file
    result = state0.delay(filename=filename, draftId=request.form['draftId'], side=request.form['represent'])
    return {"result_id": result.id}, 200  # Return the result ID of the task


@shared_task(
    ignore_result=False,
    soft_time_limit=ANALYSIS_SOFT_TIME_LIMIT,
    time_limit=ANALYSIS_TIME_LIMIT,
)
@guided_step_operation(DOC_TYPE, 1, lambda data: data['draftId'])
def state1(data: dict) -> dict:
    # 'file_name': f'{API_URL}static/{filename}'
    for k,v in data.items():
        print(f"key={k}; value={v}")

    doc_id=Drafts.resolve_id(data['draftId'])
    
    mem = DBMemory(db=db, doc_id=doc_id)

    text_by_lines = json.loads(mem["complaint_text"])
    print("retrieved text_by_lines: ", len(text_by_lines))
    # retrieve side
    side = mem["inputs/side"]
    print("Retrieved side: ", side)

    # load the aggregation of fa summaries
    fa_sum = mem["drafted_sections/fa_sum"]
    coas_sum = json.loads(mem["analysis/coa_sum"])
     # it's okay to keep key as str, already in order after state0
    coas_sum = {str(k): v for k, v in coas_sum.items()}

    # load each coa
    start_to_end_i = [int(each) for each in mem["coa_loc"].split(',')]
    print("retrieved COA start_to_end_i: ", start_to_end_i)
    coa_blocks = {}  # key: i+1
    for i in range(len(start_to_end_i)-1):
        _from = start_to_end_i[i]
        _to = start_to_end_i[i+1]
        coa_blocks[str(i)] = ' '.join(text_by_lines[_from:_to])

    # ====  Initialize variables for storing element analysis llm result  ===== 
    # id_elements = {} # dictionary with integer index as key
    coas_w_elements = {} # dictionary with integer index as key
    for coa_id, each_coa_sum in coas_sum.items():
        title = each_coa_sum.split('\n')[0]
        description = each_coa_sum.split('\n')[1:]   # description is a list of strings
        # initialize the sub-dict first:
        coas_w_elements[coa_id] = {}
        print(f"coas_w_elements initialize sub dict with key coa_id = {coa_id}, type = {type(coa_id)}")
        coas_w_elements[coa_id]['title'] = title
        coas_w_elements[coa_id]['description'] = description

    # element_analysis = {}  # dictionary {0:[{"element":, "sufficiency":, "analysis":},..], 1:[{"element":, "sufficiency":, "analysis":}...]}
    # =====
    # first step: only ask for legal elements for each coa (for best accuracy)
    # processes = []
    comand_prompt = open("backend/ai/prompts/element_checker/ELEMENT_CHECKER1").read()
    tasks = []
    for coa_id, coa in coa_blocks.items():   # coa_id is a str
        print(coa[:50])
        # now form the prompt to llm call
        filled_in_prompt = comand_prompt.format(coa=coa)
        if coa_id == '0': print("*** ELEMENT_CHECKER1 filled in prompt ***\n", filled_in_prompt)
        tasks.append({
                    'coa_id': coa_id,
                    'prompt': filled_in_prompt
                })
    # spin up a pool of threads  ->   doing # chat_completion_j(prompt) for each coa
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
        # submit each raw_citation to filter_raw_citations
        future_to_task = {
            submit_with_context(
                pool, chat_completion_j, t['prompt'],
                model=ANALYSIS_MODEL, reasoning_effort=ANALYSIS_EFFORT,
            ): t
            for t in tasks}  
        sleep(1)
        # pool._threads is a set of Thread objects
        actual_threads = len(pool._threads)
        print(f"ELEMENT_CHECKER1 actually created {actual_threads} threads")

        # we'll collect back into coas_w_elements:  a structure keyed by coa_id
        for future in concurrent.futures.as_completed(future_to_task):
            task = future_to_task[future]   # fut(result) as key, task as val
            coa_id = task['coa_id']   # still a str
            print("collect fut result for: ", coa_id)
            try:
                result = future.result()
                llm_elements = extract_from_json("legal_elements", result)
                if llm_elements:
                    element_data_l = []
                    for element_id, element_j in enumerate(llm_elements, start=1):
                        element_data_l.append({
                            'element_id': element_id,
                            'name': element_j.get('element', ''),
                            'raw_source': element_j.get('source', [])
                        })
                    
                    coas_w_elements[coa_id]['elements'] = element_data_l

            except SoftTimeLimitExceeded:
                raise
            except Exception as exc:
                print(f"Task coa_id {coa_id} failed: {exc}")

    #  first llm call is done, let's filter each cit in raw_source,  for each element   
    tasks = []
    for coa_id, coa_val in coas_w_elements.items():   # coa_id is a str
        for el_data in coa_val['elements']:
            el_data.setdefault('source', [])
            el_data.setdefault('source_info', [])
            el_data.setdefault('satisfied', True), 
            el_data.setdefault('complaint_references', "")
            el_data.setdefault('analysis', ""),
            el_data.setdefault('raw_analysis_citations', [])
            el_data.setdefault("filtered_citations", []),
            el_data.setdefault("filtered_citations_info", [])
            el_data.setdefault("selected_citations", [])
            
            for raw_cit in el_data['raw_source']:
                print("raw_cit: ", raw_cit)
                tasks.append({'coa_id': coa_id, 'element_id': el_data.get('element_id'),
                                'raw_cit': raw_cit
                                })
   # Add this just before filtered_tasks = [...]
    bad_tasks = [t for t in tasks if not (isinstance(t.get('raw_cit'), dict) and t['raw_cit'].get('citation'))]
    if bad_tasks:
        print(f"Skipping {len(bad_tasks)} raw_cit entries without citation. Sample:", bad_tasks[:3])

    # Guard against malformed raw_cit entries missing 'citation'
    filtered_tasks = [t for t in tasks if isinstance(t.get('raw_cit'), dict) and t['raw_cit'].get('citation')]

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        # submit each raw_citation to filter_raw_citations
        future_to_task = {
            pool.submit(filter_raw_citations,  t['raw_cit'].get('citation')): t      # result as key, task as val
            for t in filtered_tasks} 

        # we'll collect back into coas_w_elements:  a structure keyed by coa_id
        for future in concurrent.futures.as_completed(future_to_task):
            task = future_to_task[future]   # fut(result) as key, task as val
            coa_id = task['coa_id']   # still a str
            element_id = task['element_id']
            raw_cit = task['raw_cit']  # a dict {'case_name', 'citation', 'year', 'parenthetical'}
            print(f"collect fut result for {raw_cit}  in coa_id, element_id: {coa_id}, {element_id}")
            try:
                before_case_filtered_citations_info = future.result()

                # further filter on zero_common_word case names
                filtered_citations_info = filter_on_casename(raw_cit, before_case_filtered_citations_info)
                print("# of filtered citations before filter_on_casename: ", len(before_case_filtered_citations_info))
                print("# of filtered citations after filter_case_name: ", len(filtered_citations_info))

            except SoftTimeLimitExceeded:
                raise
            except Exception as exc:
                filtered_citations_info = []
                print(f"Task (coa_id, element_id) = ({coa_id}, {element_id}), raw_citation = {raw_cit.get('citation', '<missing>')}  Failed: {exc}")

            if filtered_citations_info:
                plain_citation_str = f"{raw_cit.get('case_name', '')}, {raw_cit.get('citation', '')} {raw_cit.get('parenthetical', '')}" if raw_cit.get('case_name', '') else f"{raw_cit.get('citation', '')} {raw_cit.get('parenthetical', '')}"
                element_idx = _get_element_index_by_id(coas_w_elements[coa_id]['elements'], element_id)
                if element_idx is None:
                    print(f"Could not find element_id {element_id} in coa_id {coa_id} while storing source citations")
                    continue
                coas_w_elements[coa_id]['elements'][element_idx]['source'].append(plain_citation_str)
                coas_w_elements[coa_id]['elements'][element_idx]['source_info'].extend(filtered_citations_info)

    # now 2nd llm call for element analysis  -> some but not all element will be returned
    # look for element index i 
    analysis_comand = open("backend/ai/prompts/element_checker/ELEMENT_CHECKER2.txt").read()
    if side.lower() == "plaintiff":
        citations_recom_instr = "support your recommendation in terms of how to improve and cure cure potential pleading deficiencies.\n"
    else:
        citations_recom_instr = "support your recommendation in terms of how to dismiss or partially dismiss the claim.\n"
    tasks = []
    for coa_id, coa_data in coas_w_elements.items():   # coa_id is a str
        # now form the prompt to llm call
        elements_w_id = '\n'.join([f"element: {ele_val['name']}, element_id: {ele_val['element_id']}" for ele_val in coa_data['elements']])
        filled_in_prompt = analysis_comand.format(
                fa=fa_sum,  # pass on fa summary
                coa='\n'.join([coa_data['title'], ' '.join(coa_data['description'])]),  # pass on coa description/summary
                elements = elements_w_id,
                citations_recom_instr = citations_recom_instr
            )
        if coa_id == '0': print("*** ELEMENT_CHECKER2 filled in prompt ***\n", filled_in_prompt)
        tasks.append({
                    'coa_id': coa_id,
                    'prompt': filled_in_prompt
                })
    # spin up a pool of threads  ->   doing # chat_completion_j(prompt) for each coa
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
        # submit each raw_citation to filter_raw_citations
        future_to_task = {
            submit_with_context(
                pool, chat_completion_j, t['prompt'],
                model=ANALYSIS_MODEL, reasoning_effort=ANALYSIS_EFFORT,
            ): t
            for t in tasks} 
        
        sleep(1)
        # pool._threads is a set of Thread objects
        actual_threads = len(pool._threads)
        print(f"ELEMENT_CHECKER2 actually created {actual_threads} threads")

        # we'll collect back into coas_w_elements:  a structure keyed by coa_id
        for future in concurrent.futures.as_completed(future_to_task):
            task = future_to_task[future]   # fut(result) as key, task as val
            coa_id = task['coa_id']   # still a str
            print("collect fut result for: ", coa_id)
            try:
                result = future.result()
                element_analysis = extract_from_json("element_analysis", result)
                if element_analysis:
                    for element_j in element_analysis:
                        ele_id = int(element_j['element_id'])
                        element_idx = _get_element_index_by_id(coas_w_elements[coa_id]['elements'], ele_id)
                        if element_idx is None:
                            print(f"Could not find analyzed element_id {ele_id} in coa_id {coa_id}")
                            continue
                        ele_data = coas_w_elements[coa_id]['elements'][element_idx]
                        status = str(element_j.get('status', 'Not Met')).strip()
                        status_norm = status.lower()
                        complaint_references = element_j.get('complaint_references', '')

                        ele_data['complaint_references'] = complaint_references

                        if status_norm == "met":
                            ele_data['satisfied'] = True
                            ele_data['analysis'] = ""
                            ele_data['raw_analysis_citations'] = []
                            ele_data['filtered_citations'] = []
                            ele_data['filtered_citations_info'] = []
                            ele_data['selected_citations'] = []
                        else:
                            analysis = f"Weakness: {element_j.get('weakness', '')}\nFocus of Challenge: {element_j.get('defense', '')}\n"
                            raw_analysis_citations = element_j.get('citations', [])
                            print("len of raw_analysis_citations: ", len(raw_analysis_citations))
                            print(raw_analysis_citations)
                            ele_data['raw_analysis_citations'] = raw_analysis_citations
                            ele_data['analysis'] = analysis
                            ele_data['satisfied'] = False

            except SoftTimeLimitExceeded:
                raise
            except Exception as exc:
                print(f"Task coa_id {coa_id} failed: {exc}")
    
    # now verify citations from the above llm - filter each citation in analysis citations list for each weak element
    tasks = []
    for coa_id, coa_val in coas_w_elements.items():   # coa_id is a str
        for el_data in coa_val['elements']:
            if el_data['raw_analysis_citations']:    
                for raw_cit in el_data['raw_analysis_citations']:
                    tasks.append({'coa_id': coa_id, 'element_id': el_data.get('element_id'),
                                    'raw_cit': raw_cit
                                    })

    # Guard against malformed raw_cit entries missing 'citation'
    bad_tasks = [t for t in tasks if not (isinstance(t.get('raw_cit'), dict) and t['raw_cit'].get('citation'))]
    if bad_tasks:
        print(f"Skipping {len(bad_tasks)} raw_analysis_citations without citation. Sample:", bad_tasks[:3])

    filtered_tasks = [t for t in tasks if isinstance(t.get('raw_cit'), dict) and t['raw_cit'].get('citation')]

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        # submit each raw_citation to filter_raw_citations
        future_to_task = {
            pool.submit(filter_raw_citations, str(t['raw_cit'].get('citation'))): t    
            for t in filtered_tasks}  
        # filter and set up value for el_data["filtered_citations"]
        for future in concurrent.futures.as_completed(future_to_task):
            task = future_to_task[future]   # fut(result) as key, task as val
            coa_id = task['coa_id']
            element_id = task['element_id']
            raw_cit = task['raw_cit']  # a dict {'case_name', 'citation', 'year', 'parenthetical'}
            print(f"collect fut result for {raw_cit}  in coa_id, element_id: {coa_id}, {element_id}")
            try:
                before_case_filtered_citations_info = future.result()
                
                # further filter on zero_common_word case names
                filtered_citations_info = filter_on_casename(raw_cit, before_case_filtered_citations_info)
                print("# of filtered citations before filter_on_casename: ", len(before_case_filtered_citations_info))
                print("# of filtered citations after filter_case_name: ", len(filtered_citations_info))
                
            except SoftTimeLimitExceeded:
                raise
            except Exception as exc:
                filtered_citations_info = []
                print(f"Task (coa_id, element_id) = ({coa_id}, {element_id}), raw_citation = {raw_cit}  Failed: {exc}")

            if filtered_citations_info:
                # Store plain text citation without HTML tags (frontend will add links)
                plain_citation_str = f"{raw_cit.get('case_name', '')}, {raw_cit.get('citation', '')} {raw_cit.get('parenthetical', '')}" if raw_cit.get('case_name', '') else f"{raw_cit.get('citation', '')} {raw_cit.get('parenthetical', '')}"
                print("plain citation string: ", plain_citation_str)
                element_idx = _get_element_index_by_id(coas_w_elements[coa_id]['elements'], element_id)
                if element_idx is None:
                    print(f"Could not find element_id {element_id} in coa_id {coa_id} while storing analysis citations")
                    continue
                coas_w_elements[coa_id]['elements'][element_idx]['filtered_citations'].append(plain_citation_str)
                coas_w_elements[coa_id]['elements'][element_idx]['filtered_citations_info'].extend(filtered_citations_info)

    mem["analysis/element_analysis"] = json.dumps(coas_w_elements)
 
    return {"message": "Form processed successfully",
            "el_data": [(each_coa_ele_data['title'], {"description": each_coa_ele_data['description'], "elements":each_coa_ele_data['elements']}) for _, each_coa_ele_data in coas_w_elements.items()]} 


def handle_state1(request):
    result = state1.delay(request.form)
    return {"result_id": result.id}, 200


@shared_task(
    ignore_result=False,
    soft_time_limit=ANALYSIS_SOFT_TIME_LIMIT,
    time_limit=ANALYSIS_TIME_LIMIT,
)
@guided_step_operation(DOC_TYPE, 2, lambda data: data['draftId'])
def state2(data: dict) -> dict:
    doc_id=Drafts.resolve_id(data['draftId'])
    mem = DBMemory(db=db, doc_id=doc_id)

    # retrieve side
    side = mem["inputs/side"]
    # load the aggregation of fa summaries
    fa_sum = mem["drafted_sections/fa_sum"]
    coas_sum = json.loads(mem["analysis/coa_sum"])

    coa_element_analysis = json.loads(mem["analysis/element_analysis"])   # json loads key as string 0 -> '0'
    # print(f"loaded element_analysis: {len(coa_element_analysis)}\n")  
    # recall element_analysis structure: el_data[id] = {"title": title, "description": description, "elements":elements}

    # customize recommendation based on side
    instr = ""
    if side.lower() == 'plaintiff':
        instr = open("backend/ai/prompts/element_checker/RECOM_P").read()
    else:
        recom_d_prompt = open("backend/ai/prompts/element_checker/RECOM_D").read()
        instr = recom_d_prompt.format(venue=mem["inputs/venue"])

    recom_comand = open("backend/ai/prompts/element_checker/RECOM").read()
    processes = []  
    for coa_id, coa_data in coa_element_analysis.items():
        coa_analysis_str = f"Cause of Action:\n{coa_data['title']}\nDescription: {coa_data['description']}\nElement Analysis:\n"

        for ele_data in coa_data['elements']:  # ['name', 'raw_source', 'source', 'source_info', 'satisfied', 'analysis', 'raw_analysis_citations', 'filtered_citations', 'filtered_citations_info', 'selected_citations']
            each_ele_str = '\n'.join([
                f"Element: {ele_data.get('name', 'Unknown')}", 
                "- element citations: \n", 
                '\n'.join(ele_data.get('source', []))
            ])
            if ele_data.get('satisfied', True):
                each_ele_str += "- satisfied: True"
                if ele_data.get('complaint_references', ''):
                    each_ele_str += f"\n- complaint support: {ele_data.get('complaint_references', '')}"
            else:
                # Use selected_citations if available, otherwise use filtered_citations
                citations_to_use = ele_data.get('selected_citations', ele_data.get('filtered_citations', []))
                each_ele_str += "\n".join([
                    "\n- analysis of Weakness and Focus of challenge:\n",
                    ele_data.get('analysis', ''),
                "== Relevant Citations to the above element ==",
                '\n'.join(citations_to_use) if citations_to_use else "No citations provided.",
                "** In your drafted recommendation for this element, USE and ONLY USE the above legal citations, if no citation provided, Do not cite any citations.**\n"
                ])
            coa_analysis_str +=  each_ele_str + '\n'
        
        filled_in_prompt = recom_comand.format(instr=instr, 
                                                   fa=fa_sum, 
                                                   analysis=coa_analysis_str)
        if coa_id == '0':
            print("== Recommendation filled in prompt for the first coa ====\n", filled_in_prompt), 
        processes.append({'coa_id': coa_id, 'prompt':filled_in_prompt})

    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
        future_map = {
            submit_with_context(
                pool, chat_completion_j, str(proc['prompt']),
                model=ANALYSIS_MODEL, reasoning_effort=ANALYSIS_EFFORT,
            ): proc for proc in processes
        }
        sleep(1)
        print(f"Recommendation actually created {len(pool._threads)} threads")
        for fut in concurrent.futures.as_completed(future_map):
            proc = future_map[fut]
            coa_id = proc['coa_id']
            try:
                llm_output = fut.result()
                llm_recoms = extract_from_json('recommendations', llm_output)
            except SoftTimeLimitExceeded:
                raise
            except Exception as ex:
                llm_recoms = []
            coa_element_analysis[coa_id]['recommendations'] =  llm_recoms    # list
            print(f"saved {len(llm_recoms)} recoms to coa id = {coa_id}")


    coa_elements_str = ""
    recom_str = ""
    for coa_id, coa_val in coa_element_analysis.items():
        coa_title = coa_val['title'].upper()
        all_ele_str = ""
        all_ele_citations_list = []
        for ele_i, ele_data in enumerate(coa_val['elements']):
            all_ele_citations_list.extend(ele_data.get('source_info', []))
            all_ele_citations_list.extend(ele_data.get("filtered_citations_info", []))
        
            if not ele_data.get('satisfied', True):
                ele_str = f"\n\n{ele_i+1}.\t{ele_data.get('name', 'Unknown')} - (Not Met)\nSource:\n"
                ele_str += '\n'.join(ele_data.get('source', []))
                ele_str += f"\nAnalysis:\n{ele_data.get('analysis', '')}\n"
                # Use selected_citations if available, otherwise fallback to filtered_citations
                selected_cits = ele_data.get('selected_citations', ele_data.get('filtered_citations', []))
                ele_str += "Analysis citations:\n" + '\n'.join(selected_cits) + '\n\n'
            else:
                ele_str = f"\n\n{ele_i+1}.\t{ele_data.get('name', 'Unknown')} - (Met)\nSource:\n"
                ele_str += '\n'.join(ele_data.get('source', []))
                if ele_data.get('complaint_references', ''):
                    ele_str += f"\nComplaint Support:\n{ele_data.get('complaint_references', '')}"
                ele_str += '\n\n'
            all_ele_str += ele_str

        coa_elements_str += f"\n\nCOUNT {str(int(coa_id)+1)}: {coa_title}\n{all_ele_str}\n\n"

        recom_str += f"\n\nCause of Action: {coa_val['title']}\nRecommendations:\n"

        annotated_recoms = []
        
        for rec_i, each_recom in enumerate(coa_val['recommendations']):
            # print("raw recommendation paragraph: ", each_recom)
            matched_ele_citations_list = []
            unique_matched_ele_citation_forms = set()
            # update location
            for cit in all_ele_citations_list:
                print("cit: ", cit)
                cit_form = cit['form']
                if cit_form in unique_matched_ele_citation_forms:
                    continue
                else:
                    unique_matched_ele_citation_forms.add(cit_form)
                    print("search_citation_form: ", cit_form)
                    idx = each_recom.find(cit_form)
                    if idx != -1:
                        # record where we found it
                        cit['start_index'] = idx
                        print(f"Found citation {cit_form} at position {idx}")
                        cit['end_index'] = idx + len(cit_form)
                        matched_ele_citations_list.append(cit)
                    else:
                        print(f"No match substring found for {cit_form}")
            
            matched_ele_citations_list.sort(key=lambda x: x['start_index'])
            print("sorted matched_ele_citations_list: ", matched_ele_citations_list)
            annotated_result = each_recom
            offst = 0
            for citation_info in matched_ele_citations_list:  
                print(f"citations_info: {citation_info['form']}, start_index: {citation_info['start_index']}, end_index: {citation_info['end_index']}, offset: {offst}")
                annotated_result, next_offset = insert_tags(annotated_result, citation_info, offst)
                offst += next_offset
            
            annotated_recoms.append(annotated_result)
            recom_str += f"{rec_i+1}. {each_recom}\n"
        
        coa_val['recommendations_html'] = annotated_recoms
    mem["drafted_sections/recommendation"] = recom_str

    # save coa_element_analysis with recommendation data
    mem["analysis/element_analysis"] = json.dumps(coa_element_analysis)
    coa_elements_str_markdown = chat_completion(coa_elements_str + MARKDOWN_PROMPT).replace('```markdown', '').replace('```', '')
    mem["drafted_sections/legal_elements"] = coa_elements_str_markdown


    # instr =mem.get("drafted_sections/fa_sum_final", "")
    # prompt = instr + MARKDOWN_PROMPT
    # print("length of prompt: ", len(prompt))
    # fa_str_markdown = chat_completion(prompt)
    # fa_str_markdown = fa_str_markdown.replace('```markdown', '').replace('```', '')
    # print("length of fa_str_markdown: ", len(fa_str_markdown))
    # print("fa_str_markdown: ", fa_str_markdown[:200])
    # mem["drafted_sections/fa_sum_final"] = fa_str_markdown

    # construct pdf
    pdf_filename = Drafts.pdf_filename(data['draftId'])
    mem['inputs/generated_pdf_filename'] = pdf_filename
    gen_fn = create_analysis_pdf(
        fn=f"backend/static/{pdf_filename}", 
                            plaintiff=mem.get('inputs/plaintiff', "specify plaintiff"),
                            defendant=mem.get('inputs/defendant', "specify defendant"),
                            attorneys_contact_info=mem.user.contact_info or "No Attorneys_contact_info",
                            venue=mem.get('inputs/venue', "Venue not yet specified"),
                            factual_allegations = mem["drafted_sections/fa_sum_final"],
                            elements = coa_elements_str_markdown,
                            recomendations = "",
                            side = mem.get("inputs/side", "specify side")
                            )
    return {"message": "Form processed successfully",
            "recomendations": [(coa_data['title'], coa_data['recommendations_html']) for _, coa_data in coa_element_analysis.items()],
            "file_name": f'{API_URL}static/{pdf_filename}'
            }


def handle_state2(request):
    result = state2.delay(request.form)
    return {"result_id": result.id}, 200

@shared_task(ignore_result=False)
@guided_step_operation(DOC_TYPE, 3, lambda data: data['draftId'])
def state3(data:dict) -> str:
    for k, v in data.items():
        print(f"key={k}, value={v}")

    # recom_str =data['recomendations']
    # print("data recomd_str: ", recom_str)  # these are not available fron frontend for now TODO
    doc_id=Drafts.resolve_id(data['draftId'])
    mem = DBMemory(db=db, doc_id=doc_id)
    # create pdf version of complaint analysis
    pdf_filename = Drafts.pdf_filename(data['draftId'])
    mem['inputs/generated_pdf_filename'] = pdf_filename

    instr = mem.get('drafted_sections/recommendation', '')
    print("instr: ", instr)
    prompt = instr +MARKDOWN_PROMPT
    recom_str_markdown = chat_completion(prompt).replace('```markdown', '').replace('```', '')
    print("recom_str_markdown: ", recom_str_markdown)
    mem["drafted_sections/recommendation_markdown"] = recom_str_markdown
    # create formatted pdf
    gen_pdf_status = create_analysis_pdf(
                                        fn=f"backend/static/{pdf_filename}",
                                        plaintiff=mem.get('inputs/plaintiff', "specify plaintiff"),
                                        defendant=mem.get('inputs/defendant', "specify defendant"),
                                        attorneys_contact_info=mem.user.contact_info or "No Attorneys_contact_info",
                                        venue="UNITED STATES DISTRICT COURT\n" + mem.get('inputs/venue', "Venue not yet specified"),
                                        factual_allegations=mem.get('drafted_sections/fa_sum_final', ''),
                                        elements=mem.get('drafted_sections/legal_elements', ""),
                                        recomendations=recom_str_markdown,
                                        side = mem.get('inputs/side', 'specify side')
                                        )
    # parse the just-drafted answer pdf file,
    drafted_text, _, _ = get_text_from_pdf_fitz(f"backend/static/{pdf_filename}")
    # print(answer_text)
    print("number of tokens in answer_text: ", num_tokens_from_string(drafted_text))
    comand_prompt = open('backend/ai/prompts/answer/HALLUCINATION_GUARD').read()
    filled_in_prompt = comand_prompt.format(text=drafted_text)
    llm_output = chat_completion_j(
        filled_in_prompt,
        model=ANALYSIS_MODEL,
        reasoning_effort=ANALYSIS_EFFORT,
    )
    # print(llm_output)
    halls = _normalize_hallucination_findings(extract_from_json("Findings", llm_output))
    mem['hallucination_check'] = json.dumps(halls)
    return {"file_name": f'{API_URL}static/{pdf_filename}', 'halls': halls}


def handle_state3(request):
    result = state3.delay(request.form)
    return {"result_id": result.id}, 200


                # coas_w_elements[coa_id]['elements'][ele_i]['citations_html'].append(annotated_full_str)
                # coas_w_elements[coa_id]['elements'][ele_i]['filtered_citations'].append(' '.join([raw_cit['case_name'], raw_cit['citation'], raw_cit['parenthetical']]))        
                # coas_w_elements[coa_id]['elements'][ele_i]['filtered_citations_info'].extend(filtered_citations_info)
@shared_task(
    ignore_result=False,
    soft_time_limit=ANALYSIS_SOFT_TIME_LIMIT,
    time_limit=ANALYSIS_TIME_LIMIT,
)
@attach_step_usage(DOC_TYPE, 1, lambda data: data['draftId'])
def regenerate_citations(data: dict) -> dict:
    """
    Regenerate citations for a specific element in a COA.
    Expected data keys:
    - draftId: int
    - coa_id: str (the COA identifier)
    - element_index: int (the element index within the COA)
    - element_name: str
    - weakness: str
    - focus_challenge: str
    """
    doc_id = Drafts.resolve_id(data['draftId'])
    coa_id = str(data['coa_id'])
    element_index = int(data['element_index'])
    for k, v in data.items():
        print(f"key={k}, value={v}")
    
    mem = DBMemory(db=db, doc_id=doc_id)
    
    # Load the element analysis to get context
    coa_element_analysis = json.loads(mem["analysis/element_analysis"])
    
    # Get COA data
    coa_data = coa_element_analysis[coa_id]
    
    # Get side for appropriate instructions
    side = mem["inputs/side"]
    if side.lower() == "plaintiff":
        citations_recom_instr = "support your recommendation in terms of how to improve and cure potential pleading deficiencies.\n"
    else:
        citations_recom_instr = "support your recommendation in terms of how to dismiss or partially dismiss the claim.\n"
    
    # Prepare the prompt for regenerating citations
    requested_element_id = data.get('element_id')
    element_name = data['element_name']
    weakness = data.get('weakness', '')
    focus_challenge = data.get('focus_challenge', '')
    
    # Use ELEMENT_CHECKER2 prompt but focused on just this one element
    regenerate_prompt = f"""Given the following cause of action and legal analysis, provide additional legal citations to support the weakness and focus of challenge.

Cause of Action:
{coa_data['title']}
{' '.join(coa_data['description'])}

Element to analyze:
{element_name}

Previous analysis:
Weakness: {weakness}
Focus of Challenge: {focus_challenge}

Please provide additional and different legal authorities and cases, preferably recent, controlling authority from the same jurisdiction and that stand for the above asserted proposition in weakness and focus of challenge, and also related to the issue of the above case. 
Output format:
Return a JSON object with a "citations" array. Each citation should have:
- "case_name": the full case name
- "citation": the legal citation (e.g., "123 F.3d 456")
- "parenthetical": a brief explanation of relevance

Example:
{{
  "citations": [
    {{
      "case_name": "Smith v. Jones",
      "citation": "123 F.3d 456",
      "parenthetical": "(holding that...)"
    }}
  ]
}}
"""
    
    print(f"*** Regenerate citations prompt for coa_id={coa_id}, element_index={element_index} ***")
    print("regenerate_prompt: ", regenerate_prompt)
    
    # Call LLM to get new citations
    try:
        llm_result = chat_completion_j(
            regenerate_prompt,
            model=ANALYSIS_MODEL,
            reasoning_effort=ANALYSIS_EFFORT,
        )
        raw_citations = extract_from_json("citations", llm_result)
        print(f"Got {len(raw_citations) if raw_citations else 0} raw citations from LLM\n{raw_citations}")
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        print(f"Failed to get citations from LLM: {exc}")
        return {"message": "Failed to generate new citations", "filtered_citations": []}
    
    if not raw_citations:
        return {"message": "No new citations generated", "filtered_citations": []}
    
    # Filter the raw citations using the existing filtering logic
    tasks = []
    for raw_cit in raw_citations:
        print("raw_cit: ", raw_cit)
        tasks.append({'raw_cit': raw_cit})
    
    new_filtered_citations = []
    new_filtered_citations_info = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        future_to_task = {
            pool.submit(filter_raw_citations, str(t['raw_cit']['citation'])): t
            for t in tasks
        }
        
        for future in concurrent.futures.as_completed(future_to_task):
            task = future_to_task[future]
            raw_cit = task['raw_cit']
            
            try:
                before_case_filtered_citations_info = future.result()
                # Further filter on case name
                filtered_citations_info = filter_on_casename(raw_cit, before_case_filtered_citations_info)
                print(f"Filtered {len(before_case_filtered_citations_info)} -> {len(filtered_citations_info)} citations")
            except SoftTimeLimitExceeded:
                raise
            except Exception as exc:
                filtered_citations_info = []
                print(f"Failed to filter citation {raw_cit.get('citation', '')}: {exc}")
            
            if filtered_citations_info:
                # Store plain text citation without HTML tags (frontend will add links)
                plain_citation_str = ' '.join([raw_cit['case_name'], raw_cit['citation'], raw_cit['parenthetical']])
                new_filtered_citations.append(plain_citation_str)
                new_filtered_citations_info.extend(filtered_citations_info)
    
    # Update the element in memory with the new citations (appending to existing)
    # make sure there is no key error when accessing coa_element_analysis
    if coa_id not in coa_element_analysis:
        return {"message": "COA ID not found in element analysis", "filtered_citations": [], "filtered_citations_info": [], "coa_id": coa_id, "element_index": element_index}
    
    element_idx = _get_element_index_by_id(coa_element_analysis[coa_id]['elements'], requested_element_id)
    if element_idx is None:
        if element_index >= len(coa_element_analysis[coa_id]['elements']):
            return {"message": "Element index out of range", "filtered_citations": [], "filtered_citations_info": [], "coa_id": coa_id, "element_index": element_index}
        element_idx = element_index

    element_data = coa_element_analysis[coa_id]['elements'][element_idx]
    if 'filtered_citations' not in element_data:
        element_data['filtered_citations'] = []
    if 'filtered_citations_info' not in element_data:
        element_data['filtered_citations_info'] = []
    element_data['filtered_citations'].extend(new_filtered_citations)
    element_data['filtered_citations_info'].extend(new_filtered_citations_info)

    # Save updated analysis back to memory
    mem["analysis/element_analysis"] = json.dumps(coa_element_analysis)
    
    return {"message": "Successfully regenerated citations", 
    "filtered_citations": new_filtered_citations,
     "filtered_citations_info": new_filtered_citations_info,
     "coa_id": coa_id,
     "element_index": element_idx}


def update_citation_selections(data: dict) -> dict:
    """
    Update the stored element analysis to keep only selected citations.
    Expected data keys:
    - draftId: int
    - citationSelections: dict with keys like "0-0" mapping to arrays of booleans
    """
    doc_id = Drafts.resolve_id(data['draftId'])
    for k, v in data.items():
        print(f"key={k}, value={v}")
    print("=== update_citation_selections for draft {doc_id} ===")
    
    citation_selections = data.get('citationSelections', {})
    # print("citation_selections: ", citation_selections)
    
    mem = DBMemory(db=db, doc_id=doc_id)
    
    # Load the element analysis
    coa_element_analysis = json.loads(mem["analysis/element_analysis"])
    # print(f"loaded coa_element_analysis: \n{json.dumps(coa_element_analysis, indent=4)}")
    
    # Update each element's citations based on selections
    for key, selections in citation_selections.items():
        print(f"key={key}")
        # check if selections is empty list, if so, skip
        if not selections:
            continue

        # Parse the key "coaIndex-elementIndex"
        parts = key.split('-')
        if len(parts) != 2:
            continue
        
        coa_id = parts[0]  # String like "0", "1", etc.
        element_index = int(parts[1])
        
        if coa_id not in coa_element_analysis:
            continue
        
        if element_index >= len(coa_element_analysis[coa_id]['elements']):
            continue
        
        element = coa_element_analysis[coa_id]['elements'][element_index]

        
        # Store selected citations in a new field 'selected_citations' (subset of filtered_citations)
        if 'filtered_citations' in element and isinstance(element['filtered_citations'], list):
            original_filtered_citations = element['filtered_citations']
            print("original_filtered_citations: ", original_filtered_citations)
            selected_filtered_citations = [
                cit for i, cit in enumerate(original_filtered_citations) 
                if i < len(selections) and selections[i]
            ]
            print("selected_filtered_citations: ", selected_filtered_citations)
            element['selected_citations'] = selected_filtered_citations
            print("User selected citations stored in selected_citations: ", selected_filtered_citations)
        
        # Filter filtered_citations_info based on selections
        # Note: filtered_citations_info might have multiple entries per citation
        # For simplicity, we'll rebuild it based on which citations are selected
        # This is a bit complex, so we'll keep all citation_info that corresponds to selected citations
        # Actually, looking at the code, filtered_citations_info can have multiple entries per citation
        # For now, let's just clear it and let it be rebuilt if needed, or keep all
        # Better approach: keep the structure but we need to track which info belongs to which citation
        # For safety, let's leave filtered_citations_info as is for now since it's used for linking

    # Save updated analysis back to memory
    # print(f"saving coa_element_analysis: \n{json.dumps(coa_element_analysis, indent=4)}")
    mem["analysis/element_analysis"] = json.dumps(coa_element_analysis)
    
    print(f"Updated citation selections for draft {doc_id}")
    return {"message": "Citation selections updated successfully"}


def route_element_checker(request):
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
    else:
        raise Exception("Invalid App State in Element Checker")
