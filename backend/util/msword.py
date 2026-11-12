
from pdf2docx import Converter

def convert2doc(pdf_name: str, word_fname: str) -> bool:
    cv = Converter(pdf_name)
    # Storing the Document in the variable's initialised path
    cv.convert(word_fname)
    # Conversion closure through the function close()
    cv.close()

    return True

if __name__ == "__main__":
    pdf = 'backend/static/new.pdf'
    word = 'backend/static/word.docx'

    convert2doc(pdf, word)