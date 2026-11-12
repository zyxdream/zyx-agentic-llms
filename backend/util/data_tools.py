
from backend.ai.complaint_parser import (
    get_text_from_pdf_fitz,
    get_text_with_line_metadata_from_pdf_fitz,
    get_pagenum_from_pdf_fitz,
)
import textract
import json
import logging
import re
from collections import defaultdict
import os
import argparse
from werkzeug.utils import secure_filename
import uuid


logger = logging.getLogger(__name__)


def remove_empty_rows(intext: str) -> list[str]: 
    return [row for row in intext.split('\n') if len(row.strip()) > 0]


def create_ignore_set(d: dict) -> set:
    """Filters the units that should be ignored from the frontend selections."""
    ignores = set()
    for key in d.keys():
        if key.startswith('ignore-'):
            ignores.add(key.split('-')[1])

    return ignores

def selected_answers2text(prefix: str, form_data):
    """Removes ignored values and builds a single string from a list."""
    # for k, v in form_data.items():
    #     print(f"form data key: {k}, value = {v}")
    ignores = create_ignore_set(form_data)
    # print("ignored ads: ", ignores)
    coas = [v for k,v in form_data.items() if k.startswith(prefix) and k not in ignores]
    # print("selected ads: ", coas)
    return "\n".join(coas)


ALLOWED_EXTENSIONS = {'txt', 'pdf', 'doc', 'docx', 'msg', 'eml', 'png', 'jpeg', 'jpg'}
def allowed_filetypes(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def is_office_lock_filename(path_or_name: str | None) -> bool:
    """True for Microsoft Office lock/temp files (e.g. '~$Document.docx') — not valid documents."""
    if not path_or_name:
        return False
    base = os.path.basename(path_or_name.replace('\\', '/'))
    return base.startswith('~$')


def parse_coas(intext: str) -> list[(str, str)]:
    """Parses text Causes of Action from LLM."""
    full_rows = remove_empty_rows(intext=intext)
    ret_l = []
    for row in full_rows:
        pr = row.split(':')
        ret_l.append((pr[0], pr[1]))

    return ret_l


def reduce_by_coa(data_table: list, reduce_key: str) -> dict:
    grouped_data = defaultdict(list)
    for item in data_table:
        grouped_data[item[1]].append(item)

    return grouped_data


def invert_side(side: str) -> str:
    if side.lower().startswith('defendant'):
        return 'plaintiff'
    else:
        return 'defendant'
    

def remove_biz_type(instr: str) -> str:
    """If the plaintiff or defendant ends in ,LLC or .Inc, remove that."""
    for suffix in [", LLC", "Inc."]:
        if instr.endswith(suffix):
            return instr[:instr.rfind(suffix)].strip()

    return instr


def filter_uploaded_text(intxt: str) -> str:
    filtered_text = ''
    text_by_lines = intxt.split('\n')
    for line in text_by_lines:
        if line.strip().isnumeric() or line.strip() == "": # ignore empty lines and line numbers
            continue
        else:
            filtered_text += f'{line}\n'

    return filtered_text


def build_default_line_metadata(text_by_lines: list[str]) -> list[dict]:
    return [
        {
            "line_number": index,
            "page_number": None,
            "page_line_number": None,
            "bold": None,
            "ocr": None,
        }
        for index, _line in enumerate(text_by_lines)
    ]


def normalize_line_metadata(text_by_lines: list[str], line_metadata: list[dict] | None = None) -> list[dict]:
    if not isinstance(line_metadata, list) or len(line_metadata) != len(text_by_lines):
        if line_metadata is not None:
            print(
                "line_metadata missing or length mismatch; saving default metadata rows. "
                f"text lines={len(text_by_lines)}, metadata rows={len(line_metadata) if isinstance(line_metadata, list) else 'n/a'}"
            )
        return build_default_line_metadata(text_by_lines)

    normalized = []
    for index, row in enumerate(line_metadata):
        if not isinstance(row, dict):
            row = {}
        normalized.append({
            "line_number": index,
            "page_number": row.get("page_number"),
            "page_line_number": row.get("page_line_number"),
            "bold": row.get("bold"),
            "ocr": row.get("ocr"),
        })
    return normalized


def prepare_complaint_text_for_memory(
    filename: str,
    complaint_text: str,
    line_metadata: list[dict] | None = None,
    lines_per_chunk_pdf: int = 40,
    lines_per_chunk_docx: int = 60,
) -> tuple[str, list[str], list[dict]]:
    normalized_text = complaint_text.replace("<CoverPage>", "\n").replace('\r\n', '\n').replace('\r', '\n')
    text_by_lines = normalized_text.splitlines()
    normalized_metadata = normalize_line_metadata(text_by_lines, line_metadata)

    f_type = filename.rsplit('.', 1)[1].lower()
    if f_type == 'pdf':
        cover_text = '\n'.join(text_by_lines[:lines_per_chunk_pdf])
    else:
        cover_text = ' '.join(text_by_lines[:lines_per_chunk_docx]).replace('\r\n', '\n').replace('\r', '\n')

    return cover_text, text_by_lines, normalized_metadata


def log_complaint_text_metadata_preview(
    filename: str,
    text_by_lines: list[str],
    line_metadata: list[dict],
) -> None:
    logger.info(
        "Saving parsed complaint for %s: first 3 text lines=%s",
        filename,
        text_by_lines[:3],
    )
    logger.info(
        "Saving parsed complaint for %s: first 3 line_metadata rows=%s",
        filename,
        line_metadata[:3],
    )
    
def check_valid_file_length(filepath:str) -> tuple[bool, int]:
    filename = filepath.split('/')[-1]
    f_type = filename.rsplit('.', 1)[1].lower()
    # GAWKER 14 pages word document contains 4082 words ~ 285 words per page -> 28k words (100 pages) = 38k tokens vs 30k TPM (tier 1) = 22k words ~ 80 pages
    if f_type == 'pdf':
        try:
            pagenum = get_pagenum_from_pdf_fitz(filepath)
            print(f'this pdf file contains {pagenum} pages')
            return pagenum < 300, pagenum
        except Exception as e:
            print(f"Error reading PDF file: {e}")
            # Return False to indicate invalid file, with 0 pages
            return False, 0
    elif f_type == 'txt':
        complaint_text = filter_uploaded_text(open(filepath).read())
        print(f"txt file contains {len(complaint_text.split())} words")
        return len(complaint_text.split()) < 22000, len(complaint_text.split())
    elif f_type == 'doc' or f_type == 'docx':   
        text = textract.process(filepath)
        complaint_text = filter_uploaded_text(text.decode('utf-8'))
        print(f"word file contains {len(complaint_text.split())} words")
        return len(complaint_text.split()) < 22000, len(complaint_text.split())


def get_complaint_text_from_file(filepath: str) -> tuple[str, str, bool]:
    """Extracts and cleans up text from an uploaded legal complaint."""
    filename = filepath.split('/')[-1]
    f_type = filename.rsplit('.', 1)[1].lower()

    if f_type == 'txt':
        complaint_text = filter_uploaded_text(open(filepath).read())
        return complaint_text, "enter case number", False
    elif f_type == 'pdf':
        from backend.constants import PDF_OCR_BACKEND
        if PDF_OCR_BACKEND == "cloud":
            from backend.util.cloud_pdf_parser import get_text_from_pdf_cloud
            print(f"using cloud OCR backend: {PDF_OCR_BACKEND}")
            complaint_text, case_number, is_image = get_text_from_pdf_cloud(filepath)
        else:
            complaint_text, case_number, is_image = get_text_from_pdf_fitz(filepath)
            print(f"using local OCR backend: {PDF_OCR_BACKEND}")
        return complaint_text, case_number, is_image
    elif f_type == 'doc' or f_type == 'docx':
        text = textract.process(filepath)
        complaint_text = filter_uploaded_text(text.decode('utf-8'))
        return complaint_text, "enter case number", False
    else:
        raise Exception("Unsupported file type")


def get_complaint_text_with_line_metadata_from_file(filepath: str) -> tuple[str, str, bool, list[dict]]:
    """Extract complaint text plus line metadata for metadata-aware guided drafting."""
    filename = filepath.split('/')[-1]
    f_type = filename.rsplit('.', 1)[1].lower()

    if f_type == 'txt':
        complaint_text = filter_uploaded_text(open(filepath).read())
        line_metadata = build_default_line_metadata(complaint_text.splitlines())
        return complaint_text, "enter case number", False, line_metadata
    elif f_type == 'pdf':
        from backend.constants import PDF_OCR_BACKEND
        if PDF_OCR_BACKEND == "cloud":
            from backend.util.cloud_pdf_parser import get_text_with_line_metadata_from_pdf_cloud
            print(f"using cloud OCR backend: {PDF_OCR_BACKEND}")
            complaint_text, case_number, is_image, line_metadata = get_text_with_line_metadata_from_pdf_cloud(filepath)
        else:
            complaint_text, case_number, is_image, line_metadata = get_text_with_line_metadata_from_pdf_fitz(filepath)
            print(f"using local OCR backend: {PDF_OCR_BACKEND}")
        return complaint_text, case_number, is_image, line_metadata
    elif f_type == 'doc' or f_type == 'docx':
        text = textract.process(filepath)
        complaint_text = filter_uploaded_text(text.decode('utf-8'))
        line_metadata = build_default_line_metadata(complaint_text.splitlines())
        return complaint_text, "enter case number", False, line_metadata
    else:
        raise Exception("Unsupported file type")


def get_text_from_extended_formats(filepath: str) -> tuple[str, str, bool]:
    """Extract text from all supported file types.
    Superset of get_complaint_text_from_file — handles txt/pdf/doc/docx plus msg/eml/png/jpeg/jpg.
    Returns (text, metadata_str, is_image). Same convention as get_complaint_text_from_file.
    """
    import extract_msg
    import email
    from email import policy as email_policy
    from backend.util.aws_textract import detect_text_from_image_bytes

    f_type = filepath.rsplit('.', 1)[1].lower()

    if f_type == 'txt':
        text = filter_uploaded_text(open(filepath).read())
        return text, "enter case number", False

    elif f_type == 'pdf':
        from backend.constants import PDF_OCR_BACKEND
        if PDF_OCR_BACKEND == "cloud":
            from backend.util.cloud_pdf_parser import get_text_from_pdf_cloud
            return get_text_from_pdf_cloud(filepath)
        else:
            from backend.ai.complaint_parser import get_text_from_pdf_fitz
            return get_text_from_pdf_fitz(filepath)

    elif f_type in ('doc', 'docx'):
        text = filter_uploaded_text(textract.process(filepath).decode('utf-8'))
        return text, "enter case number", False

    elif f_type == 'msg':
        msg = extract_msg.Message(filepath)
        parts = [
            f"From: {msg.sender or ''}",
            f"To: {msg.to or ''}",
            f"Subject: {msg.subject or ''}",
            f"Date: {msg.date or ''}",
            "",
            msg.body or "",
        ]
        text = filter_uploaded_text("\n".join(parts))
        return text, "enter case number", False

    elif f_type == 'eml':
        with open(filepath, "rb") as f:
            msg = email.message_from_binary_file(f, policy=email_policy.default)
        parts = [
            f"From: {msg.get('From', '')}",
            f"To: {msg.get('To', '')}",
            f"Subject: {msg.get('Subject', '')}",
            f"Date: {msg.get('Date', '')}",
            "",
        ]
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_content()
                if payload:
                    parts.append(payload)
        text = filter_uploaded_text("\n".join(parts))
        return text, "enter case number", False

    elif f_type in ('png', 'jpeg', 'jpg'):
        with open(filepath, "rb") as f:
            image_bytes = f.read()
        text = detect_text_from_image_bytes(image_bytes)
        return filter_uploaded_text(text), "enter case number", True

    else:
        raise ValueError(f"Unsupported file type for extended parser: .{f_type}")


def export_complaints_to_markdown(
    input_folder: str,
    output_folder: str,
    max_files: int | None = None,
) -> list[str]:
    """Extract complaint text from files in a folder and save as .md files."""
    os.makedirs(output_folder, exist_ok=True)
    written_files = []
    processed_count = 0
    for entry in os.listdir(input_folder):
        input_path = os.path.join(input_folder, entry)
        if not os.path.isfile(input_path):
            continue
        if not allowed_filetypes(entry):
            continue
        try:
            complaint_text, _, _ = get_complaint_text_from_file(input_path)
        except Exception as exc:
            print(f"Skipping {input_path}: {exc}")
            continue
        base_name = os.path.splitext(secure_filename(entry))[0]
        output_path = os.path.join(output_folder, f"{base_name}.md")
        with open(output_path, "w", encoding="utf-8") as md_file:
            md_file.write(complaint_text)
        written_files.append(output_path)
        processed_count += 1
        if max_files is not None and processed_count >= max_files:
            break
    return written_files
    

def extract_from_json(expected_key, data_from_llm) -> list|str:
    """Tries to open JSON data from an LLM."""

    if isinstance(data_from_llm, dict):
        if expected_key in data_from_llm:
            return data_from_llm[expected_key]
        else:
            print(f"Expected key '{expected_key}' not found in provided dict. Returning empty list.")
            return []
        
    if isinstance(data_from_llm, bytes):
        data_str = data_from_llm.decode('utf-8')
    elif isinstance(data_from_llm, str):
        data_str = data_from_llm
    else:
        print(f"Unsupported data type for data_from_llm: {type(data_from_llm)}")  # for example: data_from_llm can be None
        return []

    # Strip whitespace and check if data is empty
    data_str = data_str.strip()
    if not data_str:
        print("Data is empty or contains only whitespace. Return Empty List")  # for example: data_from_llm is b''
        return []
    
    try:
        parsed = json.loads(data_str)
        if expected_key in parsed:
            return parsed[expected_key]
    except Exception as e:
        print(f'error decoding LLM output, trying 2nd method ')
        # Allow zero or more spaces after '{' to handle {"key":...} outputs
        pattern = r"(\{\s*\"" + re.escape(expected_key) + r"\"\s*:[\s\S]+\})"
        match = re.search(pattern, data_str)
        if match:
            try:
                return json.loads(match.group(1))[expected_key]
            except:
                print('Error with 2nd method decoding LLM output')
                print("raw output:\n", data_str)
                return []
        else:
            print("Error with 2nd method decoding LLM output: No match found")
            print("raw output:\n", data_str)
            return []


def process_is_federal_output(llmout: str) -> str:
    """Process output from the LLM and return a string for the DB."""
    if llmout.strip().upper() == "STATE":
        return "STATE"
    else:
        return "FEDERAL"


def handle_file_upload(request, static_folder):
    """Handles file upload and length validation."""
    if 'file' in request.files:
        print("Received a file!")
        file = request.files['file']
        original_filename = secure_filename(file.filename)
        if allowed_filetypes(filename=original_filename):
            # Use a unique stored filename so returning to a draft cannot pick up
            # another upload that happened to reuse the same original filename.
            filename = f"{uuid.uuid4()}_{original_filename}"
            filepath = os.path.join(static_folder, filename)
            file.save(filepath)
            # is_valid_length, page_or_word_num = check_valid_file_length(filepath)
            # if is_valid_length:
            return filename, None  # Return filename if valid
            # else:
            #     if os.path.exists(filepath):
            #         try:
            #             os.remove(filepath)
            #         except OSError:
            #             pass
            #     # If page_or_word_num is 0, the file is likely corrupted
            #     if page_or_word_num == 0:
            #         return None, 'File appears to be corrupted or empty. Please upload a valid file.'
            #     else:
            #         return None, 'Exceeds max of 300 pdf pages. To increase the limit, please contact customer support.'
        else:
            return None, 'Unsupported file type. Must be one of: pdf, docx, doc, txt'
    return None, 'No file submitted'



def number_to_letter(n: int) -> str:
    if 1 <= n <= 26:
        return chr(64 + n)  # chr(65) = 'A'
    raise ValueError("Input must be between 1 and 26")


def int_to_roman(num: int) -> str:
    val = [
        1000, 900, 500, 400,
        100, 90,  50,  40,
        10,  9,   5,   4, 1
    ]
    syms = [
        "M",  "CM", "D", "CD",
        "C",  "XC", "L", "XL",
        "X",  "IX", "V", "IV", "I"
    ]
    roman = ""
    for i in range(len(val)):
        count = num // val[i]
        roman += syms[i] * count
        num -= val[i] * count
    return roman


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export complaint text to markdown files.")
    parser.add_argument("input_folder", help="Folder containing complaint files to process.")
    parser.add_argument("output_folder", help="Folder to write markdown files into.")
    parser.add_argument(
        "--max-files",
        type=int,
        default=None,
        help="Maximum number of files to process.",
    )
    return parser.parse_args()


def _main() -> None:
    args = _parse_args()
    written_files = export_complaints_to_markdown(
        args.input_folder,
        args.output_folder,
        max_files=args.max_files,
    )
    print(f"Wrote {len(written_files)} files.")


if __name__ == "__main__":
    _main()
