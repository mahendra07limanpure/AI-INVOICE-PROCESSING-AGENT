# Invoice Decision Agent

DEPLOYMENT LINK: https://ai-invoice-processing-agent-t68r.onrender.com/

An offline-first FastAPI and SQLite workflow that reads invoice PDFs, matches vendors and purchase orders, detects duplicates, validates amounts, and produces deterministic payment decisions.

## Architecture

```text
PDF upload -> Gemini or pdfplumber extractor -> normalization
          -> confidence -> vendor -> PO -> duplicate -> financial rules
          -> decision + evidence + audit events -> dashboard
```

The extractor only reads fields. Statuses, reason codes, thresholds, evidence, and explanations are produced by deterministic Python rules.

## Local run

```powershell
python -m pip install -r requirements.txt
python gen_invoices.py
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Open `http://127.0.0.1:8000`.

For development tools and PDF generation dependencies:

```powershell
python -m pip install -r requirements-dev.txt
```

## Gemini

Create a free Gemini API key in Google AI Studio and set it before starting:

```powershell
$env:GEMINI_API_KEY="your-key"
```

The application retries Gemini once and falls back to the offline parser if the key or service is unavailable.

## Environment

`GEMINI_API_KEY`, `GEMINI_MODEL`, `USE_CACHE`, `EXPLAIN_WITH_LLM`, and `DATABASE_PATH` are optional. Without a Gemini key, the offline parser is used.

## Demo order

The full demo runs: 01, 04, 05, 02, 03, 06, 07, 08, 09, 10.

## Edge cases

The samples demonstrate split billing, exact and fuzzy duplicates, scanned invoices, net versus gross tax, blocked vendors, inferred POs, and changed bank details.

## Assumptions and limitations

The workflow uses INR, a two-way invoice-to-PO match, SQLite vendor and PO masters, partial billing, and synthetic sample invoices. OCR is not bundled; scanned or unreadable documents are held for review. Production deployment should use durable database storage and authenticated access.

## Deployment

Render can use `render.yaml`. Set `GEMINI_API_KEY` in the Render dashboard Environment tab, never in the repo. Runtime dependencies are in `requirements.txt`; PDF generation and test dependencies are in `requirements-dev.txt`.
