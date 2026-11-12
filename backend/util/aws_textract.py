from backend.credentials import textract_client

AWS_REGION = "us-east-1"


def detect_text_from_image_bytes(image_bytes: bytes) -> str:
    """Send PNG/JPEG bytes to Textract DetectDocumentText (synchronous).
    Returns extracted text (LINE blocks joined by newlines).
    Max 5 MB per image.
    """
    client = textract_client()
    response = client.detect_document_text(Document={"Bytes": image_bytes})
    lines = [
        b["Text"]
        for b in response.get("Blocks", [])
        if b["BlockType"] == "LINE"
    ]
    return "\n".join(lines)
