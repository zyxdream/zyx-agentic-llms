from backend.answer_drafter import is_consecutive_or_expand, longest_inc_sequence, add_citations
from backend.ai.base import chat_completion_j, extract_headings, get_headers_from_llm, num_tokens_from_string
from backend.complaint_drafter import launch_llm_call_process
from backend.util.subprocess_tools import any_process_remaining
from backend.util.data_tools import get_complaint_text_from_file
from backend.util.free_law_tools import generate_citation_url, decode_citation_url
from time import sleep
import json
import pytest



def test_is_consecutive_or_expand():
    llm_output_list =[
    {"paragraph number":"3","line number":"55"},
    {"paragraph number":"5","line number":"75"},
    {"paragraph number":"6","line number":"82"},
    {"paragraph number":"7","line number":"94"},
    {"paragraph number":"10","line number":"125"}]

    para_2_ln = {}
    for each in llm_output_list:
        para_2_ln[int(each["paragraph number"])] = int(each["line number"])

    checked_para_2_ln = is_consecutive_or_expand(para_2_ln)
    print(checked_para_2_ln)
    assert isinstance(checked_para_2_ln, dict)




def test_longest_inc_sequence():
    nums = [10, 22, 9, 33, 21, 50, 41, 60, 80]
    result = longest_inc_sequence(nums)
    assert result == [10, 22, 33, 50, 60, 80]



def extract_citations_llm(ref_text:str):
    citation_llm = chat_completion_j(f"extract citations from the legal text: {ref_text} == output in JSON {{\"citations\":[{{\"citation\":\"...\",\"link\":\"...\"}}, ...]}}")
    print("citations llm output:\n", citation_llm)

def test_add_citations():
    in_list = [("Failure to State a Claim", '''COUNT 1:
While Plaintiff alleges breaches of contract and related claims, Plaintiff's complaint lacks the specificity in identifying which precise terms of the contract were breached, detail on how damages were calculated, or concrete proof of damages suffered. Under Federal Rules of Civil Procedure Rule 12(b)(6), this is a valid defense as the complaint does not meet the required pleading standards.
COUNT 2:
This defense could be raised if Dish Network argues that Plaintiff’s complaint, while detailing alleged breaches, fails to establish a specific legal duty that was violated under existing contract or statutory law to justify damages or restitution. This aligns with Federal Rules of Civil Procedure, Rule 12(b)(6), which allows for dismissal when a complaint lacks sufficient legal basis.
COUNT 3:
This defense can be asserted if the Plaintiff has not adequately stated a legal cause of action or a factual basis to support the elements of the legal claims. For example, even if all allegations are assumed to be true, the Plaintiff might have failed to establish the essential elements of a breach of contract or a specific statutory violation. California's Code of Civil Procedure Section 430.10(e) allows a defendant to demur on the grounds that the complaint does not state facts sufficient to constitute a cause of action.
COUNT 5:
This defense asserts that the plaintiff’s complaint does not allege facts sufficient to establish a breach of contract or other causes of action with legal remedies. The defense argues that, even if all allegations are assumed as true, they fail to comply with the necessary legal standards as per California Code of Civil Procedure § 430.10(e).
'''),
("Statute of Limitations", '''COUNT 1:
The implied covenant of good faith and fair dealing claim may be subject to a statute of limitations, which in California is typically four years under California Code of Civil Procedure § 337 for written contracts. If the actions complained of occurred in 2016 or early 2017 and the complaint was not filed until 2021, this could bar the claim.
COUNT 2:
Asserting that the claims are time-barred due to the expiration of the statutory period for filing. For instance, the statute of limitations for breach of contract in California under California Code of Civil Procedure § 337 is four years, and Plaintiff's alleged breaches may fall outside this period.
COUNT 3:
The provisions of California Code of Civil Procedure Sections 337 and 339 set time limits within which a breach of written contract and oral agreements, respectively, must be brought. If the plaintiff filed the complaint after the statutory period expired, this would bar the claims entirely. Considering the contract was terminated in 2018, it's crucial to ascertain the exact dates to determine the timeliness of the filing.
COUNT 4:
For the breach of contract and potentially the recording claims, Defendants may assert that the statute of limitations has expired. For breach of contract in California, under California Code of Civil Procedure § 337, there is a four-year statute of limitations. This has implications given the timeline from when the contract was allegedly breached.
COUNT 5:
This defense argues that the plaintiff's claims are barred because they were not brought within the legally required timeframe. For a breach of written contract under California law, the statute of limitations is typically four years from the date of breach under California Code of Civil Procedure § 337. This is a powerful defense if any alleged breaches occurred outside this period.
'''),
               ("Waiver and Ratification", '''COUNT 1:
If the Plaintiff continued to pay for the service after becoming aware of the alleged breaches and thus impliedly accepted the contract changes, Dish could argue that Plaintiff waived his right to object to the changes and ratified the contract as modified. This defense could mitigate or negate liability based on California Civil Code § 3515, which states that he who can and does not forbid that which is done on his behalf speaks as authorizing it.'''),
("Consent", '''COUNT 1:
Regarding the alleged breach of Penal Code § 632, Dish may claim that Plaintiff consented, either expressly or impliedly, to the recording of the call. Under the same statute, if clear consent was provided, it would serve as an affirmative defense against alleged wrongful action.
COUNT 2:
For the alleged violation under Penal Code § 632, Dish Network may assert that Plaintiff consented to the recording, which serves as a complete defense. Under California law, consent is a recognized exception to prohibited acts under § 632.
COUNT 4:
For the third cause of action under Penal Code § 632, the defense that the plaintiff consented to the recording could be argued if the defendant can provide evidence that some form of consent is typically provided during initial communications with customers. Consent acts as a waiver of the privacy concern Penal Code § 632 aims to protect.
COUNT 5:
This defense counters the allegation of recording without consent by asserting that the plaintiff implicitly or explicitly consented to the recording of the July 30, 2016, call. Under California Penal Code § 632, consent negates claims arising from unauthorized recordings.
'''),
("Failure to State a Claim", '''Defendant argues that Plaintiff's causes of action under the Computer Fraud and Abuse Act and related statutes are insufficient because Plaintiff has not adequately alleged all required elements, such as unauthorized access and the requisite damage or loss as outlined in 18 U.S.C. § 1030(g), nor demonstrated a clear legal basis under the California Uniform Trade Secrets Act, failing to establish that the information qualifies as a trade secret or was misappropriated. Consequently, Plaintiff's claims fail to meet the necessary statutory requirements for relief under Federal Rule of Civil Procedure 12(b)(6).'''),
("Statute of Limitations", '''Plaintiff's claims for the first, third, fourth, fifth, sixth, eighth, ninth, tenth, eleventh, twelfth, thirteenth, and fourteenth causes of action are barred, in whole or in part, due to the expiration of the applicable statute of limitations periods under relevant state and federal laws. Under 18 U.S.C. § 1030(g) for claims under the Computer Fraud and Abuse Act and Cal. Civ. Code § 3426.6 regarding claims for misappropriation, Plaintiff failed to initiate claims within the designated statutory period. Accordingly, these claims should be dismissed as they are time-barred''')]
    ret_l = add_citations(in_list)
    print(ret_l)




def test_generate_citation_url():
    citation_examples = ['93 S. Ct. 705 (1973)', '80 F.4th 1205', '410 U.S. 113', '112 S. Ct. 2791 (1992)', '213 L. Ed. 2d 545']
    for cit in citation_examples:
        citationUrl = generate_citation_url(cit)
        print("citationUrl: ", citationUrl)
        # URL/HTML-safe: no spaces, no path/query delimiters, and crucially no
        # angle brackets (the old '<dot>' token got parsed as HTML and corrupted links).
        assert ' ' not in citationUrl and '/' not in citationUrl and '&' not in citationUrl
        assert '<' not in citationUrl and '>' not in citationUrl
        assert '_dot_' in citationUrl





def test_decode_citation_url():
    # New safe '_dot_' token.
    citationUrls = ['93-S_dot_-Ct_dot_-705-(1973)', '80-F_dot_4th-1205',
                    '410-U_dot_S_dot_-113', '112-S_dot_-Ct_dot_-2791-(1992)','213-L_dot_-Ed_dot_-2d-545']

    citation_examples = ['93 S. Ct. 705 (1973)', '80 F.4th 1205', '410 U.S. 113', '112 S. Ct. 2791 (1992)', '213 L. Ed. 2d 545']

    for i in range(len(citationUrls)):
        assert decode_citation_url(citationUrls[i]) == citation_examples[i]

    # Legacy '<dot>' token must still decode for links saved before the change.
    legacyUrls = ['93-S<dot>-Ct<dot>-705-(1973)', '80-F<dot>4th-1205',
                  '410-U<dot>S<dot>-113', '112-S<dot>-Ct<dot>-2791-(1992)','213-L<dot>-Ed<dot>-2d-545']
    for i in range(len(legacyUrls)):
        assert decode_citation_url(legacyUrls[i]) == citation_examples[i]

    print("decoding finished succesfully")




def test_launch_llm_call_process():
    pytest.skip("Legacy live-LLM subprocess test; excluded from CI unit tests.")
    TOTAL_TOKENS = 0
    STATIC_FOLDER = "backend/static"
    filename = "Gallagher_v_Edgewood_1st_Complaint.pdf"
    complaint_text, case_number, is_image = get_complaint_text_from_file(STATIC_FOLDER + "/" + filename)

    first_page_text = complaint_text.split("<CoverPage>")[0]
    complaint_body_text = complaint_text.split("<CoverPage>")[1]
    text_by_lines = complaint_body_text.split('\n')
    # text_by_lines = complaint_text.split('\n')
    # for line in text_by_lines:
    #     print(f"=>{line}<=")

    hd,  line_numbers = extract_headings(text_by_lines)
    print(hd)
    print(line_numbers)

    hd_2_ln = get_headers_from_llm(text_by_lines)
    print("hd_2_ln:\n", hd_2_ln)

    processes = []
    comand_prompt = open('backend/ai/prompts/answer/COMPRESS').read()
    half_len = len(complaint_body_text)//2
    for i in range(2):
        print(f"char loc and len: {half_len*i} : {half_len*(i+1)}")
        filled_in_prompt = comand_prompt + complaint_body_text[half_len*i:half_len*(i+1)]
        TOTAL_TOKENS += num_tokens_from_string(filled_in_prompt)
        print("TOTAL # TOKENS = ", TOTAL_TOKENS)
        processes.append(launch_llm_call_process(filled_in_prompt))

    print(f"finished launching {len(processes)} processes")
    # Wait for all api calls to complete
    while any_process_remaining(processes):
        sleep(1)

    print('all the subprocess LLM calls are done')
    for proc in processes:
        try:
            proc_output = proc.stdout.read()  # launch_llm_call_process_j_id return json object [id, output], but read as string
            print("subproc raw output: ", proc_output)
        except Exception as e:
            print("Error in Retriving subproc result: ", e.message)



def test_get_headers_from_llm():
    pytest.skip("Legacy live-LLM parsing test; excluded from CI unit tests.")
    STATIC_FOLDER = "backend/static"
    filename = "musk_v_openai_complaint.pdf"
    complaint_text, case_number, is_image = get_complaint_text_from_file(STATIC_FOLDER + "/" + filename)

    first_page_text = complaint_text.split("<CoverPage>")[0]
    complaint_body_text = complaint_text.split("<CoverPage>")[1].replace('\r\n', '\n').replace('\r', '\n')
    text_by_lines = complaint_body_text.split('\n')
    print("PDF total # of lines in text_by_lines: ", len(text_by_lines))

    hd,  line_numbers = extract_headings(text_by_lines)
    print(hd)
    print(line_numbers)

    hd_2_ln = get_headers_from_llm(text_by_lines)
    print("hd_2_ln:\n", hd_2_ln)




# test_is_consecutive_or_expand()
# test_longest_inc_sequence()
# test_add_citations()
# test_generate_citation_url()
# test_decode_citation_url()
# test_launch_llm_call_process()

if __name__ == "__main__":
    test_get_headers_from_llm()
