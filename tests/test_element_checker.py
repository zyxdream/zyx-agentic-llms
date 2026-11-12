import re
import os
import pytest
from backend.ai.base import split_into_chunks
from backend.ai.base import get_coa_loc, get_headers_before_coa, extract_parties_venue, chat_completion_j
from backend.util.data_tools import handle_file_upload, get_complaint_text_from_file, extract_from_json


def test_flexible_pattern():
    pytest.skip("flexible_pattern was removed from backend.element_checker")


STATIC_FOLDER = 'backend/static'

def test_fa_chunks():
    filename = "JC.v.Catholic.Health.Systems.pdf"
    filepath = STATIC_FOLDER + "/" + filename
    if not os.path.exists(filepath):
        pytest.skip(f"Test file not found: {filepath}")
    complaint_text, _, _ = get_complaint_text_from_file(filepath)
    f_type = filename.rsplit('.', 1)[1].lower()
    if f_type == 'pdf':
        first_page_text = complaint_text.split("<CoverPage>")[0]
        complaint_body_text = complaint_text.split("<CoverPage>")[1]
    else:
        LINES_PER_CHUNK_DOCX = 8
        first_page_text = ' '.join(complaint_text.split('\n')[:LINES_PER_CHUNK_DOCX])
        complaint_body_text = complaint_text

    text_by_lines = complaint_body_text.split('\n')
    print("total # of lines in body_text: ", len(text_by_lines))
    fa_start_to_end_i = get_headers_before_coa(text_by_lines)
    print("fa_start_to_end_i: \n", fa_start_to_end_i)
    fa_sums = []

    for j in range(len(fa_start_to_end_i)-1):
        print(f"j = {j}")
        head_line = ""
        for k in range(4):
            curr_loc = min(fa_start_to_end_i[j]+k, fa_start_to_end_i[j+1])
            if text_by_lines[curr_loc].startswith('<B>'):
                head_line += text_by_lines[curr_loc] + ' '
        fa_sums.append(head_line)

        fa_lines = text_by_lines[fa_start_to_end_i[j]: fa_start_to_end_i[j+1]]
        fa_chunks = split_into_chunks(fa_lines)
