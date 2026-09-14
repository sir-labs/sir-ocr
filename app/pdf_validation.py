"""One bounded process per PDF validation; never share MuPDF across API threads."""
import json
import resource
import sys
from pathlib import Path
import pymupdf as fitz

fitz.TOOLS.mupdf_display_errors(False)
fitz.TOOLS.mupdf_display_warnings(False)

def validate_pdf(path, max_pages, dpi):
    try:
        with path.open('rb') as f:
            if not f.read(1024).lstrip().startswith(b'%PDF-'):
                raise ValueError('Not a PDF.')
        with fitz.open(path) as pdf:
            if pdf.needs_pass or pdf.is_encrypted:
                raise ValueError('Password-protected PDFs are not accepted.')
            if pdf.is_repaired:
                raise ValueError('PDF is damaged. Please export a valid PDF.')
            if not 1 <= len(pdf) <= max_pages:
                raise ValueError('PDF must contain 1–500 pages.')
            for page in pdf:
                if page.rect.width <= 0 or page.rect.height <= 0:
                    raise ValueError('Invalid page dimensions.')
                if page.rect.width * page.rect.height * (dpi/72)**2 > 40_000_000:
                    raise ValueError('A page exceeds the 40 megapixel render safety limit at 200 DPI.')
            return len(pdf)
    except (ValueError, RuntimeError, fitz.FileDataError) as e:
        raise ValueError(str(e))


if __name__ == '__main__':
    resource.setrlimit(resource.RLIMIT_AS, (768*1024**2, 768*1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    try:
        count = validate_pdf(Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]))
        print(json.dumps({'page_count': count}))
    except Exception as error:
        print(json.dumps({'error': str(error) if isinstance(error, ValueError) else 'Invalid or damaged PDF.'}))
        raise SystemExit(1)
