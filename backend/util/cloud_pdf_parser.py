import re
import fitz
from concurrent.futures import ThreadPoolExecutor, as_completed
from backend.util.aws_textract import detect_text_from_image_bytes
from backend.util.pdf_ocr_heuristic import page_needs_ocr
from backend.ai.document_parser import (
    clean_pdf_page_records_to_full_text,
    make_pdf_line_record,
    ocr_text_to_pdf_line_records,
)

_LAST_OCR_WARNING = False
MAX_CLOUD_PDF_OCR_TOTAL_TIMEOUT_SECONDS = 600
PER_PAGE_TEXTRACT_TIMEOUT_SECONDS = 30


def get_last_ocr_warning_cloud(reset: bool = True) -> bool:
    """
    Returns whether the last cloud OCR run skipped pages due to errors/timeouts.
    Optionally resets the flag.
    """
    global _LAST_OCR_WARNING
    val = _LAST_OCR_WARNING
    if reset:
        _LAST_OCR_WARNING = False
    return val


def mark_line(line_raw: str) -> str:
    if not line_raw.strip() or line_raw.strip().isnumeric() or all(not c.isalpha() for c in line_raw):
        return line_raw
    else:
        return f"<B>{line_raw.strip()}</B>"


def _process_page_ocr_cloud(page, page_num: int, case_stamp: str) -> tuple[int, str]:
    """
    OCR a single page via AWS Textract (replaces pytesseract path).
    Returns (page_num, page_text).
    """
    try:
        pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), colorspace=fitz.csGRAY, alpha=False)
        png_bytes = pix.tobytes("png")
        page_text = detect_text_from_image_bytes(png_bytes)
        page_text = re.sub(case_stamp, "", page_text)
        return page_num, page_text
    except Exception as e:
        print(f"Textract OCR error for page {page_num}: {e}")
        return page_num, ""


def get_text_from_pdf_cloud(fn: str) -> tuple[str, str, bool]:
    """
    Cloud version of get_text_from_pdf_fitz.

    Same hybrid approach:
    - First pass: fitz text extraction + bold detection + header/footer removal
    - Image pages (< 3 non-empty lines): OCR via AWS Textract
    - Post-processing: cover page separation, line number removal, etc.

    Returns: (parsed_text, case_number, is_image)
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
        MIN_NONEMPTY_LINES = 3
        is_image = False
        jury_trial_signal = False
        ocr_warning = False

        # First pass: Extract text and identify image-based pages
        page_texts = {}
        image_pages = []

        for i, page in enumerate(reader):
            orig_page_text = page.get_text("text")
            page_data = page.get_text("dict")

            page_text = ""
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

                    if all_bold and has_valid_content:
                        marked_line = mark_line(line_raw)
                        page_text += marked_line + "\n"
                    else:
                        page_text += line_raw + "\n"

            page_text = re.sub(case_stamp, "", page_text)
            nonempty_lines = sum(1 for ln in page_text.splitlines() if ln.strip())

            if page_needs_ocr(page, nonempty_lines):
                is_image = True
                print(f"Page {i} is image-based, will process with cloud OCR (Textract).")
                image_pages.append((i, page))
                page_texts[i] = ""
            else:
                page_texts[i] = page_text

        # Parallel OCR processing for image-based pages via Textract
        if image_pages:
            print(f"Processing {len(image_pages)} image-based pages via AWS Textract...")
            max_workers = min(5, len(image_pages))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_page = {
                    executor.submit(_process_page_ocr_cloud, page, page_num, case_stamp): (page_num, page)
                    for page_num, page in image_pages
                }

                total_timeout = min(
                    MAX_CLOUD_PDF_OCR_TOTAL_TIMEOUT_SECONDS,
                    (
                        PER_PAGE_TEXTRACT_TIMEOUT_SECONDS * len(image_pages)
                        if len(image_pages) > 0
                        else PER_PAGE_TEXTRACT_TIMEOUT_SECONDS
                    ),
                )
                try:
                    for future in as_completed(future_to_page, timeout=total_timeout):
                        try:
                            page_num, ocr_text = future.result(timeout=PER_PAGE_TEXTRACT_TIMEOUT_SECONDS)
                            page_texts[page_num] = ocr_text
                            print(f"Completed Textract OCR for page {page_num}")
                        except TimeoutError:
                            page_num, _ = future_to_page[future]
                            print(f"Timeout processing page {page_num} with Textract (exceeded {PER_PAGE_TEXTRACT_TIMEOUT_SECONDS} seconds)")
                            page_texts[page_num] = ""
                            ocr_warning = True
                        except Exception as e:
                            page_num, _ = future_to_page[future]
                            print(f"Error processing page {page_num} with Textract: {e}")
                            page_texts[page_num] = ""
                            ocr_warning = True
                except TimeoutError:
                    print(f"Timeout: Textract OCR processing exceeded {total_timeout} seconds total")
                    for future, (page_num, _) in future_to_page.items():
                        if not future.done():
                            print(f"Marking page {page_num} as failed due to overall timeout")
                            page_texts[page_num] = ""
                            future.cancel()
                            ocr_warning = True
                        elif page_num not in page_texts:
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
                    last_demand_index = page_text.rfind("DEMAND FOR JURY TRIAL")
                    docket_to_add = page_text[last_demand_index + len("DEMAND FOR JURY TRIAL"):]
                    cover_page_text += page_text.replace('<B>', '').replace('</B>', '')
                    docket_text += docket_to_add
                else:
                    index_demanded = page_text.find("JURY TRIAL DEMAND")
                    if index_demanded != -1:
                        jury_trial_signal = True
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
                        last_demand_index = page_text.rfind("DEMAND FOR JURY TRIAL")
                        docket_to_add = page_text[last_demand_index + len("DEMAND FOR JURY TRIAL"):]
                        cover_page_text += page_text.replace('<B>', '').replace('</B>', '')
                        docket_text += docket_to_add
                    else:
                        index_demanded = page_text.find("JURY TRIAL DEMAND")
                        if index_demanded != -1:
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
        filtered_text = ""
        filtered_cover_text = ""
        case_number = ""

        splitter_pattern = r'(?<!\w\.\w.)(?<![A-Z]\.)(?<![A-Z][a-z]\.)(?<!\d\.)(?<=\.|\?|!)\s'
        splitter_pattern2 = r'\n|\s(?=\d+\.\s+[A-Z])|\s(?=[A-Z]\.\s+[A-Z])'
        line_number_pattern = r'(\d+)(?:[ \t]*\s[ \t]*\d+){10,}'
        rightparen_pattern = r'(\))(?:[ \t]*\s[ \t]*\)+)+'

        for chunk in re.split(splitter_pattern, cover_page_text):
            chunk = re.sub(line_number_pattern, " ", chunk)
            chunk = re.sub(rightparen_pattern, " ", chunk)
            for line in re.split(splitter_pattern2, chunk):
                filtered_cover_text += f'{line}\n'
                if line.lower().startswith("case no. "):
                    case_number = line[8:]

        for chunk in re.split(splitter_pattern, docket_text):
            chunk = re.sub(line_number_pattern, " ", chunk)
            chunk = re.sub(rightparen_pattern, " ", chunk)
            for line in re.split(splitter_pattern2, chunk):
                if line.startswith("Case No. "):
                    case_number = line[8:]
                elif starts_with_page_number(line):
                    continue
                elif line.strip().isnumeric() or line.strip() == "":
                    continue
                else:
                    filtered_text += f'{line}\n'

        global _LAST_OCR_WARNING
        _LAST_OCR_WARNING = ocr_warning
        return "<CoverPage>".join([filtered_cover_text, filtered_text]), case_number, is_image

    finally:
        reader.close()


def get_text_with_line_metadata_from_pdf_cloud(fn: str) -> tuple[str, str, bool, list[dict]]:
    """
    Extract full parsed PDF text plus line-aligned metadata using AWS Textract for OCR pages.

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
                print(f"Page {i} is image-based, will process with cloud OCR (Textract).")
                image_pages.append((i, page))
                page_records[i] = []
            else:
                page_records[i] = page_line_records

        if image_pages:
            print(f"Processing {len(image_pages)} image-based pages via AWS Textract...")
            max_workers = min(5, len(image_pages))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_page = {
                    executor.submit(_process_page_ocr_cloud, page, page_num, case_stamp): (page_num, page)
                    for page_num, page in image_pages
                }

                total_timeout = min(
                    MAX_CLOUD_PDF_OCR_TOTAL_TIMEOUT_SECONDS,
                    (
                        PER_PAGE_TEXTRACT_TIMEOUT_SECONDS * len(image_pages)
                        if len(image_pages) > 0
                        else PER_PAGE_TEXTRACT_TIMEOUT_SECONDS
                    ),
                )
                try:
                    for future in as_completed(future_to_page, timeout=total_timeout):
                        try:
                            page_num, ocr_text = future.result(timeout=PER_PAGE_TEXTRACT_TIMEOUT_SECONDS)
                            page_records[page_num] = ocr_text_to_pdf_line_records(
                                ocr_text,
                                page_num,
                                case_stamp,
                            )
                            print(f"Completed Textract OCR for page {page_num}")
                        except TimeoutError:
                            page_num, _ = future_to_page[future]
                            print(f"Timeout processing page {page_num} with Textract (exceeded {PER_PAGE_TEXTRACT_TIMEOUT_SECONDS} seconds)")
                            page_records[page_num] = []
                            ocr_warning = True
                        except Exception as e:
                            page_num, _ = future_to_page[future]
                            print(f"Error processing page {page_num} with Textract: {e}")
                            page_records[page_num] = []
                            ocr_warning = True
                except TimeoutError:
                    print(f"Timeout: Textract OCR processing exceeded {total_timeout} seconds total")
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
