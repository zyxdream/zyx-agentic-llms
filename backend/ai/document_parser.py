import tiktoken
from pypdf import PdfReader
import re
import os
from PIL import Image   # 10.3.0
import fitz  # 1.24.0
import pytesseract   # 0.3.10
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial
from backend.util.pdf_ocr_heuristic import page_needs_ocr

_LAST_OCR_WARNING = False

def get_last_ocr_warning(reset: bool = True) -> bool:
    """
    Returns whether the last OCR run skipped pages due to errors/timeouts.
    Optionally resets the flag.
    """
    global _LAST_OCR_WARNING
    val = _LAST_OCR_WARNING
    if reset:
        _LAST_OCR_WARNING = False
    return val


def extracted2kv(instr: str, expected_keys: set) -> dict:
    """Function for extracting known keys from text.  To be used with prompting an LLM to
    extract certain values."""

    def strip_non_alphanumeric(string):
        return re.sub(r"^\W+|\W+$", "", string)

    d = {}
    for line in instr.split('\n'):
        print(f">{line}<")
        if line == "":
            continue
        la = line.split(':')
        print("la split: ", la)
        if len(la) == 2:
            la = [strip_non_alphanumeric(l) for l in la]
            if la[0] in expected_keys:
                d[la[0]] = la[1].strip()
    print("d inside extracted2kv: ", d)
    # if they keys were not found, fill in with None
    for key in expected_keys:
        if key not in d:
            d[key] = None

    return d


def count_doc_tokens(doc_body: str) -> int:
    """counts the number of tokens"""

    enc = tiktoken.encoding_for_model("gpt-4")
    encoded_body = enc.encode(doc_body)
    # print(encoded_body)
    return len(encoded_body)


def find_last_match_index(pattern, in_text: str):
    matches = list(re.finditer(pattern, in_text))
    if matches:
        return matches[-1].start()  # Return the last match
    else:
        return None  # No match found


def has_leading_num(intext: str) -> int:
    """If the intext starts with an integer plus ., return the integer,
    else return None."""
    all_matches = list(re.finditer(r"^\d+.", intext))
    if len(all_matches) > 0:
        end = all_matches[0].span()[1]
        return int(intext[:end-1])
    else:
        return None


def mark_line(line_raw: str) -> str:
    # Check if the stripped line is empty or numeric
    if not line_raw.strip() or line_raw.strip().isnumeric() or all(not c.isalpha() for c in line_raw):
        return line_raw
    else:
        # print(f"Bold Text line Found:")
        # print(repr(line_raw))
        return f"<B>{line_raw.strip()}</B>"  # it's single line


def starts_with_page_number(line) -> bool:
    return re.match(r"^\s-\d+-\s", line) is not None


def make_pdf_line_record(text: str, page_index: int, bold: bool | None, ocr: bool) -> dict:
    return {
        "text": text,
        "page_number": page_index + 1,
        "bold": bold,
        "ocr": ocr,
    }


def ocr_text_to_pdf_line_records(page_text: str, page_index: int, case_stamp: str) -> list[dict]:
    page_text = re.sub(case_stamp, "", page_text)
    return [
        make_pdf_line_record(line, page_index, None, True)
        for line in page_text.splitlines()
    ]


def clean_pdf_page_records_to_full_text(
    page_records: dict[int, list[dict]],
    page_count: int,
) -> tuple[str, str, list[dict]]:
    """Clean parser records into saved text lines and aligned metadata."""
    splitter_pattern = r'(?<!\w\.\w.)(?<![A-Z]\.)(?<![A-Z][a-z]\.)(?<!\d\.)(?<=\.|\?|!)\s'
    splitter_pattern2 = r'\n|\s(?=\d+\.\s+[A-Z])|\s(?=[A-Z]\.\s+[A-Z])'
    line_number_pattern = r'(\d+)(?:[ \t]*\s[ \t]*\d+){10,}'
    rightparen_pattern = r'(\))(?:[ \t]*\s[ \t]*\)+)+'

    text_lines = []
    line_metadata = []
    page_line_counts = {}
    case_number = ""

    for page_index in range(page_count):
        for record in page_records.get(page_index, []):
            text = re.sub(line_number_pattern, " ", record.get("text", ""))
            text = re.sub(rightparen_pattern, " ", text)
            for chunk in re.split(splitter_pattern, text):
                for line in re.split(splitter_pattern2, chunk):
                    if starts_with_page_number(line):
                        continue
                    if line.strip().isnumeric() or line.strip() == "":
                        continue

                    page_number = record.get("page_number")
                    if page_number is None:
                        page_line_number = None
                    else:
                        page_line_number = page_line_counts.get(page_number, 0)
                        page_line_counts[page_number] = page_line_number + 1

                    line_number = len(text_lines)
                    text_lines.append(line)
                    line_metadata.append({
                        "line_number": line_number,
                        "page_number": page_number,
                        "page_line_number": page_line_number,
                        "bold": record.get("bold"),
                        "ocr": record.get("ocr"),
                    })

                    line_for_case = line.replace('<B>', '').replace('</B>', '').strip()
                    if line_for_case.lower().startswith("case no. "):
                        case_number = line_for_case[8:].strip()

    return "\n".join(text_lines) + ("\n" if text_lines else ""), case_number, line_metadata

def get_pagenum_from_pdf_fitz(fn:str):
     """Returns the number of pages in the input PDF file."""
     with fitz.open(fn) as doc:
        return doc.page_count

def _process_page_ocr(page, page_num: int, case_stamp: str) -> tuple[int, str]:
    """
    Process a single page with OCR. Returns (page_num, page_text).
    Optimized for speed with reduced resolution and faster OCR settings.
    """
    try:
        # Use lower resolution for OCR (200 DPI is usually sufficient for text recognition)
        # Matrix: 200/72 = ~2.78x scaling factor for 200 DPI (default is 72 DPI)
        # Using 2.0x for ~144 DPI which is a good balance between speed and accuracy
        pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), colorspace=fitz.csGRAY, alpha=False)
        img = Image.frombytes("L", (pix.width, pix.height), pix.samples)

        # Configure Tesseract for faster processing:
        # - PSM 6: Assume uniform block of text (faster than auto-detect)
        # - OEM 3: Default OCR engine mode
        # - Reduce character whitelist if you know the content type
        custom_config = r'--oem 3 --psm 6'
        # Add timeout to prevent hanging (60 seconds per page should be sufficient)
        page_text = pytesseract.image_to_string(img, config=custom_config, timeout=60)
        page_text = re.sub(case_stamp, "", page_text)

        return page_num, page_text
    except Exception as e:
        # Log the error and return empty text to prevent hanging
        print(f"OCR error for page {page_num}: {e}")
        return page_num, ""


def get_text_from_pdf_fitz(fn: str) -> tuple[str, str, bool]:
    """
    Gets text body from a docket, Returns: body_text, case_number, is_image
    Things this does:
    [X] removes line numbers (not paragraph numbers)
    [X] strip out page number starts with: " -4-...."
    [X] strip out case number starts with: "Case No. ...."
    [X] strip out header "Case 5:04-cv-01497-RS     Document 45      Filed 09/23/2004     Page 5 of 15"
    which gets appended to the end of every page (without newline)

    Optimized for image-based PDFs with parallel OCR processing.
    """


    def starts_with_page_number(line) -> bool:
        return re.match(r"^\s-\d+-\s", line) is not None

    # Open PDF with error handling
    try:
        reader = fitz.open(fn)
    except fitz.FileDataError as e:
        raise ValueError(f"Invalid or corrupted PDF file: {fn}. Error: {str(e)}")
    except PermissionError as e:
        raise PermissionError(f"Permission denied when opening PDF file: {fn}. Error: {str(e)}")
    except Exception as e:
        raise RuntimeError(f"Failed to open PDF file: {fn}. Error: {str(e)}")

    # Validate PDF has pages
    if reader.page_count == 0:
        reader.close()
        raise ValueError(f"PDF file has no pages: {fn}")

    try:
        docket_text = ""
        cover_page_text = ""
        case_stamp = r"Case [^\n]*?Page [0-9]+ of [0-9]+"
        MIN_NONEMPTY_LINES = 3        # Require at least this many non-empty lines
        is_image = False
        jury_trial_signal = False
        ocr_warning = False

        # First pass: Extract text and identify image-based pages
        page_texts = {}  # Store page text by index
        image_pages = []  # Store (page_index, page_object) for pages needing OCR

        for i, page in enumerate(reader):
            orig_page_text = page.get_text("text")
            # print(f"== page {i} original raw text:\n{orig_page_text}\n===")
            page_data = page.get_text("dict")  # Get text with metadata

            page_text = ""
            # Process the metadata dictionary for bold text
            page_height = page.rect.height
            header_threshold = page_height * 0.05  # Adjust this threshold as needed
            footer_threshold = page_height * 0.95
            for block in page_data["blocks"]:
                if "lines" not in block:  # Skip blocks without 'lines'
                    continue

                for line in block["lines"]:
                    # print("line: ", line)
                    line_raw = ' '.join([span['text'] for span in line["spans"]])
                    # print("raw line: ", line_raw)
                    # task 1: check if line's bbox location is within defined header or footer range, if so, do not add to result string
                    x_left, y_upper, x_right, y_lower = line['bbox']
                    if y_lower < header_threshold:  # most strict, entire block is above the header threshold
                        print("header: ", line_raw)
                        continue   # proceed to next iteration
                    if y_lower > footer_threshold:  # less strict (as footer usually wider); use y_upper > footer_threshold for more conservative choice
                        print("footer: ", line_raw)
                        continue

                    all_bold = True  # Start assuming all spans are bold
                    has_valid_content = False  # Track if we found any alphanumeric content
                    for span in line["spans"]:   # a line can contains multiple spans
                        text = span["text"]
                        if not re.search(r'[a-zA-Z0-9]', text):   # if this spans has no letter or num, ignore it
                            continue
                        has_valid_content = True  # Found at least one span with alphanumeric content
                        font_flags = span["flags"]  # Font style flags (e.g., bold)
                        font_name = span["font"]
                        # print(f"span {str(span_i)} text: {text}; font_flags: {font_flags}; font_name: {font_name}")
                        # Check for bold text (flag value 2 typically represents bold)
                        is_bold_flag = bool(font_flags & 2)
                        is_bold_font = 'Bold' in font_name
                        is_bold = is_bold_flag or is_bold_font
                        if not is_bold:
                            all_bold = False

                    # Only mark as bold if line has valid content AND all valid spans are bold
                    if all_bold and has_valid_content:
                        marked_line = mark_line(line_raw)
                        page_text += marked_line + "\n"
                    else:
                        page_text += line_raw + "\n"

            # print(f"*** reconstruted raw text: \n {page_text}\n****")
            # Strip common case headers/footers from text-extracted pages too
            page_text = re.sub(case_stamp, "", page_text)
            # Heuristic: if very few non-empty lines remain, treat as image-based and OCR
            nonempty_lines = sum(1 for ln in page_text.splitlines() if ln.strip())

            if page_needs_ocr(page, nonempty_lines):
                is_image = True
                print(f"Page {i} is image-based, will process with OCR.")
                image_pages.append((i, page))
                page_texts[i] = ""  # Placeholder, will be filled by OCR
            else:
                page_texts[i] = page_text

        # Parallel OCR processing for image-based pages
        if image_pages:
            print(f"Processing {len(image_pages)} image-based pages in parallel...")
            # Use ThreadPoolExecutor for I/O-bound OCR operations
            # Limit to 4-8 workers to avoid overwhelming the system
            # max_workers = min(8, len(image_pages), os.cpu_count() or 4)
            max_workers = min(4, len(image_pages), max(1, (os.cpu_count() or 2) // 2))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                # Submit all OCR tasks
                future_to_page = {
                    executor.submit(_process_page_ocr, page, page_num, case_stamp): (page_num, page)
                    for page_num, page in image_pages
                }

                # Collect results as they complete
                # Add timeout to prevent indefinite hanging
                # Use 30 seconds timeout per page; hard-cap total OCR time at 120 seconds
                total_timeout = min(120, 30 * len(image_pages) if len(image_pages) > 0 else 30)
                try:
                    for future in as_completed(future_to_page, timeout=total_timeout):
                        try:
                            # Individual future timeout (should complete within 30s)
                            page_num, ocr_text = future.result(timeout=30)
                            page_texts[page_num] = ocr_text
                            print(f"Completed OCR for page {page_num}")
                        except TimeoutError:
                            page_num, _ = future_to_page[future]
                            print(f"Timeout processing page {page_num} with OCR (exceeded 30 seconds)")
                            page_texts[page_num] = ""  # Fallback to empty text
                            ocr_warning = True
                        except Exception as e:
                            page_num, _ = future_to_page[future]
                            print(f"Error processing page {page_num} with OCR: {e}")
                            page_texts[page_num] = ""  # Fallback to empty text
                            ocr_warning = True
                except TimeoutError:
                    # Handle case where as_completed itself times out
                    print(f"Timeout: OCR processing exceeded {total_timeout} seconds total")
                    # Mark any remaining pages as failed
                    for future, (page_num, _) in future_to_page.items():
                        if not future.done():
                            print(f"Marking page {page_num} as failed due to overall timeout")
                            page_texts[page_num] = ""
                            future.cancel()
                            ocr_warning = True
                        elif page_num not in page_texts:
                            # Future completed but result wasn't collected
                            try:
                                page_num, ocr_text = future.result(timeout=0)
                                page_texts[page_num] = ocr_text
                            except:
                                page_texts[page_num] = ""
                            ocr_warning = True

        # Second pass: Process pages in order with extracted text
        for i in range(reader.page_count):
            page_text = page_texts.get(i, "")
            print(f"page {i} text: {page_text}")


            if i == 0:
                index_demand = page_text.find("DEMAND FOR JURY TRIAL")
                if index_demand != -1:
                    jury_trial_signal = True
                    # Find the last occurrence of "DEMAND FOR JURY TRIAL" and include it in cover
                    last_demand_index = page_text.rfind("DEMAND FOR JURY TRIAL")
                    docket_to_add = page_text[last_demand_index + len("DEMAND FOR JURY TRIAL"):]
                    cover_page_text += page_text.replace('<B>', '').replace('</B>', '')  # add whole first page in case 'DEMAND FOR JURY TRIAL' is misplaced, No need to mark bold text before 'DEMAND FOR JURY TRIAL',
                    docket_text += docket_to_add
                else:
                    index_demanded = page_text.find("JURY TRIAL DEMAND")
                    if index_demanded != -1:
                        jury_trial_signal = True
                        # Find the last occurrence of "JURY TRIAL DEMAND" and include it in cover
                        last_demand_index = page_text.rfind("JURY TRIAL DEMAND")
                        docket_to_add = page_text[last_demand_index + len("JURY TRIAL DEMAND"):]
                        cover_page_text += page_text.replace('<B>', '').replace('</B>', '')
                        docket_text += docket_to_add
                    else:
                        print("No jury trial demand found. Add first page to cover")
                        cover_page_text += page_text.replace('<B>', '').replace('</B>', '')

            elif i == 1:
                if jury_trial_signal:
                    docket_text += page_text
                    docket_text += f'\n== End of page {i + 1} ==\n'
                    print("Signal found on first page, add this second page to body.")
                else:
                    index_demand = page_text.find("DEMAND FOR JURY TRIAL")
                    if index_demand != -1:
                        # Find the last occurrence of "DEMAND FOR JURY TRIAL" and include it in cover
                        last_demand_index = page_text.rfind("DEMAND FOR JURY TRIAL")
                        docket_to_add = page_text[last_demand_index + len("DEMAND FOR JURY TRIAL"):]
                        cover_page_text += page_text.replace('<B>', '').replace('</B>', '')
                        docket_text += docket_to_add
                    else:
                        index_demanded = page_text.find("JURY TRIAL DEMAND")
                        if index_demanded != -1:
                            # Find the last occurrence of "JURY TRIAL DEMAND" and include it in cover
                            last_demand_index = page_text.rfind("JURY TRIAL DEMAND")
                            docket_to_add = page_text[last_demand_index + len("JURY TRIAL DEMAND"):]
                            cover_page_text += page_text.replace('<B>', '').replace('</B>', '')
                            docket_text += docket_to_add
                        else:
                            print("No jury trial demand found. Add second page to both cover and body")
                            cover_page_text += page_text.replace('<B>', '').replace('</B>', '')
                            docket_text += page_text
            else:
                docket_text += page_text
                docket_text += f'\n== End of page {i + 1} ==\n'

        print(f"===check first few lines of docket_text ===\n{docket_text[:200]}")
        # Process and clean up extracted docket_text
        # filter out lines with ONLY a line number
        filtered_text = ""
        filtered_cover_text = ""
        case_number = ""

        # Matches: A space character. with conditions: avoid matching spaces that come after sequences like "J.R.R.", "U.S.", "Dr.", "1.", Asserts that what precedes the space is .?!
        splitter_pattern = r'(?<!\w\.\w.)(?<![A-Z]\.)(?<![A-Z][a-z]\.)(?<!\d\.)(?<=\.|\?|!)\s'

        # split the text on occurrences that match either of the following: 1. A newline character. 2. A sequence where digits/A-Z are followed by a period, one or more spaces, and then an uppercase letter.
        splitter_pattern2 = r'\n|\s(?=\d+\.\s+[A-Z])|\s(?=[A-Z]\.\s+[A-Z])'

        # there are at least 10 sequences of digits separated by spaces.
        line_number_pattern = r'(\d+)(?:[ \t]*\s[ \t]*\d+){10,}'
        # match consecutive )))
        rightparen_pattern = r'(\))(?:[ \t]*\s[ \t]*\)+)+'

        for chunk in re.split(splitter_pattern, cover_page_text):
            chunk = re.sub(line_number_pattern, " ", chunk)
            chunk = re.sub(rightparen_pattern, " ", chunk)
            for line in re.split(splitter_pattern2, chunk):
                filtered_cover_text += f'{line}\n'
                if line.lower().startswith("case no. "):
                    case_number = line[8:]

        for chunk in re.split(splitter_pattern, docket_text):
            # print("inside chunk breaks: ", chunk)
            chunk = re.sub(line_number_pattern, " ", chunk)
            chunk = re.sub(rightparen_pattern, " ", chunk)
            # print("after chunk sub: ", chunk)
            for line in re.split(splitter_pattern2, chunk):
                # print("inside line breaks: ", line)
                if line.startswith("Case No. "):
                    case_number = line[8:]
                elif starts_with_page_number(line):
                    continue
                elif line.strip().isnumeric() or line.strip() == "":  # ignore empty lines and line numbers
                    continue
                else:
                    # print("after line sub: ", line)
                    filtered_text += f'{line}\n'
            #         # below is logic to collect paragraphs
            #         para_num = has_leading_num(line.strip())
            #         if para_num is None:
            #             current_paragraph += line
            #         else:
            #             paragraphs.append({'num': current_paragraph_num, 'text': current_paragraph})
            #             current_paragraph = line
            #             current_paragraph_num = para_num
            #
            # # TODO: post filtering of paragraphs

        global _LAST_OCR_WARNING
        _LAST_OCR_WARNING = ocr_warning
        return "<CoverPage>".join([filtered_cover_text, filtered_text]), case_number, is_image

    finally:
        # Clean up resources - always close the PDF reader
        reader.close()


def get_text_with_line_metadata_from_pdf_fitz(fn: str) -> tuple[str, str, bool, list[dict]]:
    """
    Extract the full parsed PDF text starting at page 1 plus line-aligned metadata.

    Returns: full_text, case_number, is_image, line_metadata
    """
    try:
        reader = fitz.open(fn)
    except fitz.FileDataError as e:
        raise ValueError(f"Invalid or corrupted PDF file: {fn}. Error: {str(e)}")
    except PermissionError as e:
        raise PermissionError(f"Permission denied when opening PDF file: {fn}. Error: {str(e)}")
    except Exception as e:
        raise RuntimeError(f"Failed to open PDF file: {fn}. Error: {str(e)}")

    if reader.page_count == 0:
        reader.close()
        raise ValueError(f"PDF file has no pages: {fn}")

    try:
        case_stamp = r"Case [^\n]*?Page [0-9]+ of [0-9]+"
        is_image = False
        ocr_warning = False
        page_records = {}
        image_pages = []

        for i, page in enumerate(reader):
            page_data = page.get_text("dict")
            page_line_records = []

            page_height = page.rect.height
            header_threshold = page_height * 0.05
            footer_threshold = page_height * 0.95
            for block in page_data["blocks"]:
                if "lines" not in block:
                    continue

                for line in block["lines"]:
                    line_raw = ' '.join([span['text'] for span in line["spans"]])
                    x_left, y_upper, x_right, y_lower = line['bbox']
                    if y_lower < header_threshold:
                        print("header: ", line_raw)
                        continue
                    if y_lower > footer_threshold:
                        print("footer: ", line_raw)
                        continue

                    all_bold = True
                    has_valid_content = False
                    for span in line["spans"]:
                        text = span["text"]
                        if not re.search(r'[a-zA-Z0-9]', text):
                            continue
                        has_valid_content = True
                        font_flags = span["flags"]
                        font_name = span["font"]
                        is_bold_flag = bool(font_flags & 2)
                        is_bold_font = 'Bold' in font_name
                        is_bold = is_bold_flag or is_bold_font
                        if not is_bold:
                            all_bold = False

                    bold = all_bold and has_valid_content
                    line_text = mark_line(line_raw) if bold else line_raw
                    line_text = re.sub(case_stamp, "", line_text)
                    page_line_records.append(make_pdf_line_record(line_text, i, bold, False))

            nonempty_lines = sum(
                1 for record in page_line_records
                if str(record.get("text", "")).strip()
            )
            if page_needs_ocr(page, nonempty_lines):
                is_image = True
                print(f"Page {i} is image-based, will process with OCR.")
                image_pages.append((i, page))
                page_records[i] = []
            else:
                page_records[i] = page_line_records

        if image_pages:
            print(f"Processing {len(image_pages)} image-based pages in parallel...")
            max_workers = min(4, len(image_pages), max(1, (os.cpu_count() or 2) // 2))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_page = {
                    executor.submit(_process_page_ocr, page, page_num, case_stamp): (page_num, page)
                    for page_num, page in image_pages
                }

                total_timeout = min(120, 30 * len(image_pages) if len(image_pages) > 0 else 30)
                try:
                    for future in as_completed(future_to_page, timeout=total_timeout):
                        try:
                            page_num, ocr_text = future.result(timeout=30)
                            page_records[page_num] = ocr_text_to_pdf_line_records(
                                ocr_text,
                                page_num,
                                case_stamp,
                            )
                            print(f"Completed OCR for page {page_num}")
                        except TimeoutError:
                            page_num, _ = future_to_page[future]
                            print(f"Timeout processing page {page_num} with OCR (exceeded 30 seconds)")
                            page_records[page_num] = []
                            ocr_warning = True
                        except Exception as e:
                            page_num, _ = future_to_page[future]
                            print(f"Error processing page {page_num} with OCR: {e}")
                            page_records[page_num] = []
                            ocr_warning = True
                except TimeoutError:
                    print(f"Timeout: OCR processing exceeded {total_timeout} seconds total")
                    for future, (page_num, _) in future_to_page.items():
                        if not future.done():
                            print(f"Marking page {page_num} as failed due to overall timeout")
                            page_records[page_num] = []
                            future.cancel()
                            ocr_warning = True
                        elif page_num not in page_records:
                            try:
                                page_num, ocr_text = future.result(timeout=0)
                                page_records[page_num] = ocr_text_to_pdf_line_records(
                                    ocr_text,
                                    page_num,
                                    case_stamp,
                                )
                            except Exception:
                                page_records[page_num] = []
                            ocr_warning = True

        full_text, case_number, line_metadata = clean_pdf_page_records_to_full_text(
            page_records,
            reader.page_count,
        )

        global _LAST_OCR_WARNING
        _LAST_OCR_WARNING = ocr_warning
        return full_text, case_number, is_image, line_metadata

    finally:
        reader.close()


if __name__ == "__main__":
    # Files for testing
    # filename = "AAFER_v_Fearless_COMPLAINT.pdf"
    # filename = "Tarantino_v_Gawker.pdf"
    # filename = "COMPLAINT-DigitalEnvoyv.Google.pdf"
    # filename = "COMPLAINT-Waymo.pdf"
    filename = "Gallagher_v_Edgewood_1st_Complaint.pdf"
    # filename = "Meta - COMPLAINT.pdf"
    # filename = "jl/Goddard_Complaint.pdf"
    # filename = "HomeLight v. Shkipin_orphan_coa.pdf"  # header not all uppercase

    # text = get_text_from_pdf("backend/uploads/COMPLAINT-Waymo.pdf")
    # text, case_num, paragraphs = get_text_from_pdf("backend/uploads/COMPLAINT-DigitalEnvoyv.Google.pdf")
    text, case_num, is_image = get_text_from_pdf_fitz("backend/static/" + filename)
    cover_text, body_text = text.split('<CoverPage>')
    print("**********  cover text start: **********")
    for line in cover_text.split('\n'):
        print(line)
    print("********** cover text end **********")
    print(f'case number: {case_num}')

    print("***** body text start: **********")
    raw_headers = []
    for num, line in enumerate(body_text.split('\n')):
        print(line)
        if line.isupper() or (line.startswith('<B>') and line.endswith('</B>')):
            raw_headers.append(line.replace('<B>', '').replace('</B>', ''))

        # if line.startswith('<b>') or line.endswith('</b>'):
        #     print(line)
    print("body text end: **********")
    print(*raw_headers, sep="\n")


    # print(count_doc_tokens(text))
    """ 31 page Complaint is 18,882 tokens
    GPT-32k can handle this.  Will be $0.06/1k tokens.
    """

    ######### testing #########

#     # Matches: A space character. with conditions: avoid matching spaces that come after sequences like "J.R.R.", "U.S.", "Dr.", "1.", Asserts that what precedes the space is .?!
#     splitter_pattern = r'(?<!\w\.\w.)(?<![A-Z]\.)(?<![A-Z][a-z]\.)(?<!\d\.)(?<=\.|\?|!)\s'
#
#     # split the text on occurrences that match either of the following: 1. A newline character. 2. A sequence where digits/A-Z are followed by a period, one or more spaces, and then an uppercase letter.
#     splitter_pattern2 = r'\n|\s(?=\d+\.\s+[A-Z])|\s(?=[A-Z]\.\s+[A-Z])'
#
#     # there are at least 10 sequences of digits separated by spaces.
#     line_number_pattern = r'(\d+)(?:[ \t]*\s[ \t]*\d+){10,}'
#
#     text1 = '''
#     1
#     2
#     3
#     4
#     5
#     6
#     7
#     8
#     9
#     10
#     11
#     12
#     13
#     14
#     15
#     16
#     17
#     18
#     19
#     20
#     21
#     22
#     23
#     24
#     25
#     26
#     27
#     28
#        Case No. _________
#     COMPLAINT
#      QUINN EMANUEL URQUHART & SULLIVAN, LLP
#        Charles K. Verhoeven (Bar No. 170151)
#        charlesverhoeven@quinnemanuel.com
#        David A. Perlson (Bar No. 209502)
#        davidperlson@quinnemanuel.com
#        Melissa Baily (Bar No. 237649)
#        melissabaily@quinnemanuel.com
#        John Neukom (Bar No. 275887)
#        johnneukom@quinnemanuel.com
#        Jordan Jaffe (Bar No. 254886)
#        jordanjaffe@quinnemanuel.com
#     50 California Street, 22 nd  Floor
#     San Francisco, California 94111-4788
#     Telephone: (415) 875-6600
#     Facsimile: (415) 875-6700
#     Attorneys for WAYMO LLC
#     UNITED STATES DISTRICT COURT
#     NORTHERN DISTRICT OF CALIFORNIA, SAN FRANCISCO DIVI SION
#     WAYMO LLC,
#     Plaintiff,
#     vs.
#     UBER TECHNOLOGIES, INC.;
#     OTTOMOTTO LLC; OTTO TRUCKING
#     LLC,
#     Defendants.
#       CASE NO. _________________
#     COMPLAINT
#     1.   VIOLATION  OF  DEFENSE  OF
#     TRADE  SECRETS  ACT
#     2. VIOLATION  OF  CALIFORNIA
#     UNIFORM  TRADE  SECRET  ACT
#     3. PATENT  INFRINGEMENT
#     4. VIOLATION  OF  CAL.  BUS  & PROF.
#     CODE SECTION 17200
#     DEMAND FOR JURY TRIAL
#       '''
#
#     def test_number_pattern(text: str):
#         line = re.sub(line_number_pattern, " ", text)
#         print(line)
#         return line
#
#     assert test_number_pattern(text1).strip().isnumeric() is False
#
#     def test_splitter(text: str):
#         text_lines = []
#         splitter_pattern = r'(?<!\w\.\w.)(?<![A-Z]\.)(?<![A-Z][a-z]\.)(?<!\d\.)(?<=\.|\?|!)\s'
#         chunks = re.split(splitter_pattern, text)
#         for chunk in chunks:
#             print(f"== chunk ==\n{chunk}")
#         return len(chunks)
#
#     assert test_splitter(text1) > 1
#
#     text2 = '''
#       CASE NO. _________________
#     COMPLAINT
#     1.   VIOLATION  OF  DEFENSE  OF
#     TRADE  SECRETS  ACT
#     2. VIOLATION  OF  CALIFORNIA
#     UNIFORM  TRADE  SECRET  ACT
#     3. PATENT  INFRINGEMENT
#     4. VIOLATION  OF  CAL.  BUS  & PROF.
#     CODE SECTION 17200
#     DEMAND FOR JURY TRIAL
#     '''
#
#     text3 = '''Exhibits A, C.  On April 15, 2021, as part of its
# FACTUAL ALLEGATIONS A. Background on Meta and the Facebook and Instagram Platforms 14. Meta operates Facebook, '''
#     def test_splitter2(text: str):
#         lines = re.split(splitter_pattern2, text)
#         for line in lines:
#             print(f"== line ==\n{line}")
#
#         return len(lines)
#
#     assert test_splitter2(text2) >= 10
#     assert test_splitter2(text3) >= 4 and test_splitter2(text3) < 6
