import pytest

pytestmark = pytest.mark.skip(reason="Legacy exploratory AI script; not suitable for CI unit tests.")

from backend.ai.base import get_text_from_pdf_fitz, split_into_chunks, get_fa_text, get_coa_text, get_headers_before_coa
from backend.ai.base import chat_completion, chat_completion_j, deepseek_completion_j, extract_openai_retry_in_seconds
from backend.ai.base import get_rfp_section_text, client, SYSTEM_PROMPT
from backend.util.free_law_tools import find_citations

from backend.ai.base import grok_completion


def test_split_into_chunks():
    filename = "COMPLAINT-Waymo.pdf"
    # filename = "Tarantino_v_Gawker.pdf"
    complaint_text, case_number, is_image = get_text_from_pdf_fitz("backend/static/" + filename)
    first_page_text = complaint_text.split("<CoverPage>")[0]
    complaint_body_text = complaint_text.split("<CoverPage>")[1]
    fa_text_lines = get_fa_text(complaint_body_text)
    # print("fa_text_lines: ", fa_text_lines)
    # print("********")
    fa_text_lines_chunks = split_into_chunks(fa_text_lines)
    for chunk in fa_text_lines_chunks:
        print(' '.join(chunk))
        print("********")

    assert(len(fa_text_lines_chunks) > 3)


def test_chat_completion(user_prompt: str):
    # raw_citation = "Conservatorship of Sanderson, "
    # # raw_citation = "Alameda Newspapers, Inc. v. City of Oakland"
    # raw_citation = "Fong v. East West Bancorp, Inc., 19 Cal. App. 5th 181 (Cal. Ct. App. 2018) (elements: ownership, plaintiff’s right to possession, defendant’s wrongful act of possession"
    # user_prompt = f"The following string includes a case name a possible incorrect citation (unverified):\n{raw_citation}\n Based on this, list the most likely reporter citations for the above case. Each candidate should be formatted fully.\n"
    # user_prompt = """I've brought a personal injury case in Idaho. Idaho Code § 6-1603 caps non-economic damages in personal injury (including wrongful death) cases, but provides an exception: the cap does not apply where the cause of action arises out of “willful or reckless misconduct.” Please find cases where the plaintiff showed “reckless” or “willful and wanton” misconduct sufficient to avoid/reach over Idaho’s cap on non-economic damages under Idaho Code § 6-1603. Give as much detail as possible on the specific facts that supported the finding of “reckless” or “willful and wanton” misconduct. Ensure all cases are real cases. Provide citations in bluebook format. Focus on post-2003 cases (after the 2003 amendment to § 6-1603) that involve personal injury or wrongful death. You can include appellate decisions as well as trial court opinions and jury verdicts.
# In your response, please also provide legal citations, like case names, published reporter citations, and years filed. i want to know the Relevant Cases & Statutes. Please cite them. if you can not, please explain why.
#  """
    print("Chat completion: ")
    returned = chat_completion(user_prompt, model='gpt-5')
    if returned is None:
        print("No response from chat_completion.")
    else:
        print(returned)
        print("========  Showing FL API result. ========")
        result = find_citations(returned)
        for each in result:
            print(each)
            print()




def test_deepseek_completion_j():
    filled_prompt = '''Find legal elements for the following Cause of Action. You are also provided with description of this claim based on the content of complaint.
    ('Federal Trademark Infringement 15 U.S.C. § 1114', {'description': ['The use of the HomeLight Logo has been consistent in advertising and sales of real estate services since at least 2011, and it possesses federal trademark registrations, establishing its distinctiveness and acquired secondary meaning (Compl. ¶51-53).', 'Defendants use a similar logo in their real estate services, leading to consumer confusion regarding the source of those services and potential affiliation or sponsorship with HomeLight, which they are not authorized to do (Compl. ¶55-56).', "Such actions are willful, causing confusion, mistake, and deception among consumers, resulting in Defendants' unjust enrichment and monetary damages to HomeLight (Compl. ¶58-60).", 'The violation leads to irreparable harm, including loss of goodwill, justifying an injunction to cease further infringement and corrective advertising to mitigate market damage (Compl. ¶61-65).'''
    deepseek_completion_j(filled_prompt)



def test_extract_openai_retry_in_seconds():
    error_message = ''' Error code: 429 - {'error': {'message': 'Rate limit reached for gpt-4o in organization org-KLYfJdXhQHujCXt6DspdAGfu on tokens per min (TPM): Limit 30000, Used 28801, Requested 1360. Please try again in 322ms. Visit https://platform.openai.com/account/rate-limits to learn more.', 'type': 'tokens', 'param': None, 'code': 'rate_limit_exceeded'}}'''
    retry_seconds = extract_openai_retry_in_seconds(error_message)
    print(retry_seconds)
    error_message = '''Please try again in 2.31s.'''
    retry_seconds = extract_openai_retry_in_seconds(error_message)
    print(retry_seconds)


def test_get_rfp_section_text():
    filename = 'WPengine v. Automatticc RFP.pdf'

    complaint_text, case_number, is_image = get_text_from_pdf_fitz("backend/static/" + filename)
    first_page_text = complaint_text.split("<CoverPage>")[0]
    complaint_body_text = complaint_text.split("<CoverPage>")[1]
    text_by_lines = complaint_body_text.split('\n')   # main body text by lines
    requests_start_end_i = get_rfp_section_text(text_by_lines, section="specific requests for production of Documents")
    print("REQUESTS requests_start_end_i: ", requests_start_end_i)


def test_get_coa_text():
    filename = 'Cellular.South.v.Google.pdf'
    filename = "jl/JCOC v. US (LCCRSF).pdf"
    filename = "jl/JL Complaint - State Court (Ferguson).pdf"
    filename = "Gallagher_v_Edgewood_1st_Complaint.pdf"
    filename = "Meta - COMPLAINT.pdf"
    filename = "AAFER v Fearless (COMPLAINT).pdf"
    filename = "COMPLAINT-DigitalEnvoyv.Google.pdf"
    filename = "COMPLAINT-Waymo.pdf"
    filename = "Tarantino_v_Gawker.pdf"
    # filename = "jl/Goddard Complaint.pdf"
    # filename = "musk v openai complaint.pdf"
    filename = "HomeLight v. Shkipin_orphan_coa.pdf"  # header not all uppercase
    filename = "Griffith_v_Tiktok.pdf"
    filename = "McLaughlin v. HomeLIght - COMPLAINT.pdf"
    filename = "Tires.Antitrust.Complaint.pdf"
    filename = "Feldman Cross_Complaint_2_24_25.pdf"
    filename = "Nordeman_v._Dish_COMPLAINT.pdf"


    complaint_text, case_number, is_image = get_text_from_pdf_fitz("backend/static/" + filename)
    first_page_text = complaint_text.split("<CoverPage>")[0]
    complaint_body_text = complaint_text.split("<CoverPage>")[1]
    coa_lines_blocks = get_coa_text(complaint_body_text)
    for block in coa_lines_blocks:
        print(block[:10])
        print("====")



def test_get_headers_before_coa():
    filename = 'Cellular.South.v.Google.pdf'
    filename = "jl/JCOC v. US (LCCRSF).pdf"
    filename = "jl/JL Complaint - State Court (Ferguson).pdf"
    filename = "Gallagher_v_Edgewood_1st_Complaint.pdf"
    filename = "Meta - COMPLAINT.pdf"
    filename = "AAFER v Fearless (COMPLAINT).pdf"
    filename = "COMPLAINT-DigitalEnvoyv.Google.pdf"
    filename = "COMPLAINT-Waymo.pdf"
    filename = "Tarantino_v_Gawker.pdf"
    # filename = "jl/Goddard Complaint.pdf"
    # filename = "musk v openai complaint.pdf"
    filename = "HomeLight v. Shkipin_orphan_coa.pdf"  # header not all uppercase
    filename = "Griffith_v_Tiktok.pdf"
    filename = "McLaughlin v. HomeLIght - COMPLAINT.pdf"
    filename = "Tires.Antitrust.Complaint.pdf"
    filename = "Feldman Cross_Complaint_2_24_25.pdf"
    filename = "Nordeman_v._Dish_COMPLAINT.pdf"

    complaint_text, case_number, is_image = get_text_from_pdf_fitz("backend/static/" + filename)
    first_page_text = complaint_text.split("<CoverPage>")[0]
    complaint_body_text = complaint_text.split("<CoverPage>")[1]
    text_by_lines = [line for line in complaint_body_text.split('\n') if line]
    bf_coa_start_end_i = get_headers_before_coa(text_by_lines)
    for each in bf_coa_start_end_i:
        if each == len(text_by_lines)-1:
            print(text_by_lines[each])
        else:
            print(text_by_lines[each], text_by_lines[each+1], text_by_lines[each+2])
        print("===")


def test_grok_completion(prompt: str):
    print("GROK completion: ")
    returned = grok_completion(prompt)
    print(returned)


prompts = ["""
Check if the reporter's citation is correct for the following case:
Hayward v. Jack's Pharmacy, Inc., 167 Idaho 70, 467 P.3d 398 (2020) (summary judgment, Idaho Supreme Court)
If the citation is correct, show the source where you found the citation, if not, try to provide the correct citation.
""",
"""
Check if the reporter's citation is correct for the following case:
Rogers v. Smith, 143 Idaho 391, 146 P.3d 258 (Ct. App. 2006) (jury verdict, motion for new trial)
If the citation is correct, show the source where you found the citation, if not, try to provide the correct citation.
""",
"""
Check if the reporter's citation is correct for the following case:
Cenarrusa v. Andrews, No. CV-OC-2013-11363, 2015 WL 13744965 (Idaho Dist. Ct. June 3, 2015) (summary judgment, damages cap applied)
If the citation is correct, show the source where you found the citation, if not, try to provide the correct citation.
""",
"""
Check if the reporter's citation is correct for the following case:
Such evidence has been deemed relevant to the defense of scienter, fair notice, and the
agency&#39;s own understanding of legal standards. See General Elec. Co. v. Johnson, 595 F. Supp. 2d 8, 17 (D.D.C. 2009).
If the citation is correct, show the source where you found the citation, if not, try to provide the correct citation.
"""
]
# for prompt in prompts:
#     print("====")
#     test_grok_completion(prompt)
#     test_chat_completion(prompt)
# test_deepseek_completion_j()
# test_extract_openai_retry_in_seconds()
# test_get_rfp_section_text()
# test_get_coa_text()
# test_get_headers_before_coa()


case_prompt = """To answer the following user's question, Please decide if legal cases are needed. If yes, please provide the cases according to the instructions. If no, return empty list.
User's question:
{user_question}

Please return cases that are directly relevant to the user's question, and is within the jurisdiction of the user's question.
each case should be in the below format:
example 1: 'case_name': 'A & M Records, Inc. v. Napster, Inc.', 'citation':'239 F.3d at 1013', 'parenthetical':'(knowledge of infringment is essential for contributory liability)',
Return the list of cases using the following JSON format, if no cases are needed, return empty list for "cases" key.
{{"cases":[
{{"case_name":"...", "citation":"....", "parenthetical":"....."}}, 
{{"case_name":"...", "citation":"....", "parenthetical":"....."}}, ...
]
}}

"""
