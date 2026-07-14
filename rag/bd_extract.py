import base64, json, sys

try:
    import pymupdf
except ImportError:
    import fitz as pymupdf

b64 = sys.stdin.read().strip()
pdf_bytes = base64.b64decode(b64)
doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
pages = [page.get_text() for page in doc]
text = "\n\n".join(pages)
print(json.dumps({"text": text, "pages": len(doc)}))
