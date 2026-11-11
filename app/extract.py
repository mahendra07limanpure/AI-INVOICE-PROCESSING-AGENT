import os,re,json,hashlib
from pathlib import Path
try: import pdfplumber
except Exception: pdfplumber=None
ROOT=Path(__file__).resolve().parent.parent
LABELS=r"(GSTIN:|Bank A/C:|Invoice No:|Invoice Date:|Due Date:|PO:|Subtotal:|GST Amount:|(?<!Sub)Total:)"
def _num(s): return float(s.replace(",",""))
def extract_offline(path):
    text=""
    if pdfplumber:
        try:
            with pdfplumber.open(path) as p:text="\n".join(x.extract_text() or "" for x in p.pages)
        except Exception: pass
    fields={"vendor_name":None,"vendor_tax_id":None,"vendor_bank_account":None,"invoice_number":None,"invoice_date":None,"due_date":None,"po_reference":None,"currency":"INR","subtotal":None,"tax_amount":None,"tax_included_in_line_items":None,"total":None,"line_items":[]}
    text=re.sub(LABELS,r"\n\1",text,flags=re.I)
    if len(text.strip())<40:
        fields.update(field_confidence={k:None for k in ["invoice_number","invoice_date","total","po_reference","vendor_name"]},document_quality="scanned",missing_fields=["invoice_number","invoice_date","total"],engine="offline");return fields
    lines=[x.strip() for x in text.splitlines() if x.strip()]; fields["vendor_name"]=lines[0] if lines else None
    pats={"vendor_tax_id":r"\b(\d{2}[A-Z]{5}\d{4}[A-Z]\dZ\d)\b","vendor_bank_account":r"\b([A-Z]{4}0\d{6}-\d{4})\b","invoice_number":r"invoice\s*(?:no\.?|number|#)\s*[:\-]?\s*([A-Z0-9\-\/]+)","invoice_date":r"invoice\s*date\s*[:\-]?\s*(\d{4}-\d{2}-\d{2})","due_date":r"due\s*date\s*[:\-]?\s*(\d{4}-\d{2}-\d{2})","po_reference":r"\b(PO-\d+)\b"}
    for k,p in pats.items():
        m=re.search(p,text,re.I);fields[k]=m.group(1) if m else None
    for k,p in {"subtotal":r"sub\s*-?\s*total\s*[:\-]?\s*(?:INR)?\s*([\d,]+(?:\.\d+)?)","tax_amount":r"(?:gst|tax|vat) amount\s*[:\-]?\s*(?:INR)?\s*([\d,]+(?:\.\d+)?)","total":r"(?<!sub)(?:grand\s+total|total\s+due|amount\s+due|total)\s*[:\-]?\s*(?:INR)?\s*([\d,]+(?:\.\d+)?)"}.items():
        ms=list(re.finditer(p,text,re.I));fields[k]=_num(ms[-1].group(1)) if ms else None
    req=["invoice_number","invoice_date","total","po_reference","vendor_name"]
    fields.update(field_confidence={k:.9 if fields[k] else None for k in req},document_quality="clean",missing_fields=[k for k in req if not fields[k]],engine="offline")
    return fields
def extract_gemini(path):
    from google import genai
    from google.genai import types
    model=os.getenv("GEMINI_MODEL","gemini-2.5-flash-lite")
    schema=["vendor_name","vendor_tax_id","vendor_bank_account","invoice_number","invoice_date","due_date","po_reference","currency","subtotal","tax_amount","tax_included_in_line_items","total","line_items","field_confidence","document_quality","missing_fields"]
    prompt="Return only JSON with keys "+", ".join(schema)+". Use null for anything not clearly visible. Never guess."
    data=open(path,"rb").read()
    client=genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    response=client.models.generate_content(
        model=model,
        contents=[types.Part.from_bytes(data=data,mime_type="application/pdf"),prompt],
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )
    raw=response.text.strip()
    if raw.startswith("```"):
        raw=raw.split("\n",1)[1].rsplit("```",1)[0].strip()
    result=json.loads(raw)
    result["engine"]="gemini"
    return result
def extract(path):
    h=hashlib.sha256(open(path,"rb").read()).hexdigest(); cache=ROOT/"cache";cache.mkdir(exist_ok=True)
    cp=cache/(h+".json")
    if os.getenv("USE_CACHE","1")=="1" and cp.exists(): return json.loads(cp.read_text())
    if os.getenv("GEMINI_API_KEY"):
        try:
            x=extract_gemini(path)
            cp.write_text(json.dumps(x))
            return x
        except Exception:
            pass
    return extract_offline(path)
