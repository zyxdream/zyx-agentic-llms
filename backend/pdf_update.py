
from pypdf import PdfReader, PdfWriter

# mapping from form field to PDF field name
UD105_MAPPING = {
    'caseNumber': 'CaseNumber[0]', # CaseNumber[0].2, CaseNumber[0].3, CaseNumber[0].4
    'barNumber': 'AttyBarNo[0]',
    'courtCounty': 'CrtCounty[0]',
    'courtAddress': 'CrtStreet[0]',
    'plaintiff': 'Party1[0]', # also Party1[0].2, Party1[0].3, Party1[0].4
    'defendant': 'Party2[0]', # also Party2[0].2, Party2[0].3, Party2[0].4
    'additionalNotes': 'JJ',
    'file': 'PP',  # TODO: handle unknown keys
    'fileUpload': 'PP2',
    'service': 'PP3',
}

def update_form(fn: str, out_name: str, field_pairs: list):
    reader = PdfReader(fn)
    writer = PdfWriter()

    page = reader.pages[0]
    fields = reader.get_fields()

    for i in range(len(reader.pages)):  # copy the whole document
        writer.add_page(reader.pages[i])

    for pr in field_pairs:
        writer.update_page_form_field_values(
            writer.pages[0], {pr[0]: pr[1]},
        )

    # write "output" to PyPDF2-output.pdf
    with open(out_name, "wb") as output_stream:
        writer.write(output_stream)

def generate_pdf(data_d: dict) -> str:

    # determine the service
    if data_d['service'] == 'Unlawful Detainer/Eviction Defense':
        mapping = UD105_MAPPING
        file_name = 'docs/ud105.pdf'
    else:
        return "service not supported"

    # remove 'service' key from data_d
    mapped_pairs = list(map(lambda x: (mapping[x[0]], x[1]), data_d.items()))
    print('mapped_pairs: ', mapped_pairs)
    update_form(file_name, "backend/static/filled-out.pdf", mapped_pairs)

    return "Form processed successfully"

if __name__ == "__main__":

    generate_pdf({'service': 'Unlawful Detainer/Eviction Defense',
                  'caseNumber': '5784574985',
                  'barNumber': '1000023'
                  })
