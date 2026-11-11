from pathlib import Path
import pdfplumber
p = Path("samples/01_happy.pdf")
print("exists", p.exists(), "size", p.stat().st_size if p.exists() else None)
with pdfplumber.open(p) as pdf:
    for i, page in enumerate(pdf.pages):
        t = page.extract_text() or ""
        print("---PAGE", i, "len", len(t))
        print(repr(t))
        chars = page.chars[:8]
        print("nchars", len(page.chars))
