import os
from backend.util.data_tools import get_complaint_text_from_file, extract_from_json
from backend.ai.base import get_rfp_section_text, chat_completion_j


def manual_test_items_extraction(filename):
    """Inspect a sample PDF with a live model; not an automated pytest test."""

    rfp_text, case_number, _ = get_complaint_text_from_file("backend/static/" + filename)
    text_by_lines = rfp_text.split('\n')

    requests_start_end_i = get_rfp_section_text(text_by_lines, section="specific requests for production")
    requests_textlines = text_by_lines[requests_start_end_i[0]:requests_start_end_i[-1]]
    print("REQUESTS: ")
    print(requests_textlines)
    print("# request blocks: ", len(requests_textlines))
    print("===")

    # before_requests_textlines = text_by_lines[:requests_start_end_i[0]]
    # for section_name in ['definition', 'instruction']:
    #     print("section name: ", section_name)
    #     comand = open("backend/ai/prompts/rfp_response/LIST_DEF_INSTRUCT").read()
    #     filled_in_prompt = comand.format(section=section_name, context='\n'.join(before_requests_textlines))
    #     print("filled_in_prompt: \n", filled_in_prompt)
    #     output = chat_completion_j(filled_in_prompt)
    #     llm_list = extract_from_json(f"{section_name}s", output)
    #     print(f"# {section_name}_list: {len(llm_list)}")
    #     print(llm_list)
    #     print("=====")


# =============== RFP documents for testing  =================#
filename = "RFP (Uber).pdf"
# filename = "Generated_RFP_Gawker.pdf"
# filename = "walmart rfp.pdf"
# filename = "Bayside-RFP.pdf"

if __name__ == "__main__":
    if not os.path.exists("backend/static/" + filename):
        raise SystemExit(f"Test file not found: backend/static/{filename}")
    manual_test_items_extraction(filename)
