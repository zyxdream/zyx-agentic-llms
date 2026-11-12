
from celery import shared_task
from backend.ai.base import chat_completion, multichat_completion_text
from backend.util.free_law_tools import filter_raw_citations, annotate_text_with_citations_multiple
from backend.util.account_access import account_execution
import json


DOC_TYPE = "research"
SYSTEM_PROMPT = "You are a legal researcher. When answering user's questions. following the instructions:\n" + \
        "1. As a litigation assistant, you should be familiar with the law and the legal system. In your response, prefer jurisdiction appropriate authorities. \n" + \
        "2. In your response, be very cautious about the legal references, you should only cite cases for which there is published reporter citations identified.\n" + \
        "3. When citing cases, always ensure the case name followed by the official published reporter citation in Bluebook form. and they are in one line. there is no new line or line breaks between the case name and the citation.\n" + \
        "4. Whenever you mention or citea case, Regardless the position of the case in your response, even inside a header,  **ALWAYS** prefix the case name with [[CASE_START]], No need to add anything at the end. e.g., [[CASE_START]]Hatfield v. Max Rouse & Sons Northwest, 606 P.2d 944.\n" + \
        "5. It is essential to include [[CASE_START]] at the beginning of any case mentioned or cited in your response Regardless of whether there are already markdowns for it.  so we can easily identify and check on them.\n"
   

@shared_task(ignore_result=False)
def research_task(data: dict) -> str:
    with account_execution(data.get('user_id')):
        return _research_task(data)


def _research_task(data: dict) -> str:
    """
    This function handles the research function which does two things:
    1. search for cases using an LLM
    2. verify and link those cases to a known database.  

    data should contain a query.
    """

    # 0. unpack the query
    query = data['conversation'][-1]['input']
    prev_queries = []
    for q in data['conversation']:
        prev_queries.append((q['input'], q['response']))
    del(prev_queries[-1])

    # 0.5. get the model selection (default to gpt-5.5 if not provided)
    selected_model = data.get('model', 'gpt-5.5')
    if selected_model == 'gpt-6-sol':
        selected_model = 'gpt-6.1-sol'
    print(f"Using model: {selected_model}")

    # 1. call the LLM
    command = open("backend/ai/prompts/research/DEFAULT").read()
    filled_in_prompt = command.format(user_query=query)
    print(f"filled_in_prompt: {filled_in_prompt}")

    #data['conversation'] is the history, this is an array of {'input': "...", 'response': "....."}
    # llm_reply = chat_completion(filled_in_prompt)
    llm_reply = multichat_completion_text(
        prev_queries,
        filled_in_prompt,
        model=selected_model,
        reasoning_effort="high" if selected_model in {"gpt-6-sol", "gpt-6.1-sol"} else None,
        sys_prompt=SYSTEM_PROMPT,
    )
    print(f"llm_reply: {llm_reply}")
    if llm_reply is None:
        raise RuntimeError("Research answer generation failed")


    # step 1.5 check for hallucinations
    # print("  *****  call hallucination filter  ****")
    # command = open("backend/ai/prompts/research/FILTER").read()
    # filled_in_prompt = command.format(user_query=llm_reply)
    # llm_reply = chat_completion(filled_in_prompt)

    # 2. annotate the results with any cases
    # citations = find_citations(llm_reply)
    cite_info = filter_raw_citations(llm_reply)
    print(f"found {len(cite_info)} citations")
    print("cite_info: ", cite_info)

    annotated_result = llm_reply
    if cite_info:
        annotated_result = annotate_text_with_citations_multiple(annotated_result, cite_info)

    annotated_result = annotated_result.replace("[[CASE_START]]", "")
    return {"response": annotated_result}
