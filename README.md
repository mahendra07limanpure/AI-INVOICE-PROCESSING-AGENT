# Invoice Decision Agent

Offline-first FastAPI invoice workflow with deterministic extraction and policy rules. It serves a static dashboard, seeded SQLite vendor/PO data, live polling events, review actions, reset, and sample PDF generation.

```powershell
pip install -r requirements.txt
python gen_invoices.py
uvicorn app.main:app --reload
```

Open `http://localhost:8000`. Set `GEMINI_API_KEY` optionally; without it, pdfplumber extraction is used.

On Windows, WeasyPrint may require GTK/Pango native libraries. The generator automatically creates
valid text PDFs as a fallback when those libraries are unavailable, so the demo can run without GTK.
