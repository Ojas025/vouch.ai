from vouch.ingestion import AttachmentMetadata

from docling.document_converter import DocumentConverter

def parse_attachments(attachments: list[AttachmentMetadata]):
    converter = DocumentConverter()
    combined_text = ""

    for attachment in attachments:
        result = converter.convert(attachment.file_path)
        combined_text += f"\n{result.document.export_to_markdown()}"

    return combined_text
