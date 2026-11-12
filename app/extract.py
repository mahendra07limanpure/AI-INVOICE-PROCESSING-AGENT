import os,re,json,hashlib,logging,time
from pathlib import Path
try: import pdfplumber
except Exception: pdfplumber=None
ROOT=Path(__file__).resolve().parent.parent
logger=logging.getLogger(__name__)
LABELS=r"(GSTIN:|Bank A/C:|Invoice No:|Invoice Date:|Due Date:|PO:|Subtotal:|GST Amount:|(?<!Sub)Total:)"
LABEL_NAMES={
    "invoice_number":r"invoice\s*(?:no\.?|number|#)|bill\s*(?:no\.?|number)|inv\s*no\.?",
    "invoice_date":r"invoice\s*date|date\s*of\s*issue|bill\s*date",
    "due_date":r"due\s*date|payment\s*due|pay\s*by",
    "po_reference":r"purchase\s*order\s*ref(?:erence)?|po\s*ref|po\s*(?:no\.?|number)|p\.?\s*o\.?",
    "vendor_tax_id":r"seller\s*gst\s*reg\.?\s*no\.?|gst\s*reg\.?\s*no\.?|gstin|gst\s*no\.?",
    "vendor_bank_account":r"bank\s*a/c|bank\s*account|a/c\s*no\.?|account\s*no\.?",
    "subtotal":r"sub\s*total|net\s*amount|taxable\s*(?:value|amount)",
    "tax_amount":r"gst\s*amount|tax\s*amount|tax\s*@\s*\d+(?:\.\d+)?%|gst\s*@\s*\d+(?:\.\d+)?%|igst|cgst",
    "total":r"grand\s*total|total\s*due|amount\s*payable|amount\s*due|(?<!sub)total",
}
LABEL_ALTERNATION=re.compile("|".join(f"(?P<{k}>{v})" for k,v in LABEL_NAMES.items()),re.I)
PROMPT_VERSION="v4"
gemini_failures=0
gemini_open_until=0
def _num(s): return float(s.replace(",",""))
def env(name,default=""):
    return os.getenv(name,default).strip().strip("\"'")
def gemini_disabled(): return env("DISABLE_GEMINI")=="1"
def gemini_model(): return env("GEMINI_MODEL","gemini-3.5-flash-lite")
def gemini_key(): return env("GEMINI_API_KEY")
def extraction_mode():
    try:
        from .db import conn
        with conn() as c:
            row=c.execute("SELECT value FROM app_settings WHERE key='extraction_mode'").fetchone()
        value=row[0] if row else env("EXTRACTION_MODE","auto")
    except Exception:value=env("EXTRACTION_MODE","auto")
    return value if value in ("auto","gemini_first","offline") else "auto"
def breaker_state():
    return {"state":"open" if time.monotonic()<gemini_open_until else "closed","seconds_remaining":max(0,int(gemini_open_until-time.monotonic()))}
def reset_breaker():
    global gemini_failures,gemini_open_until
    gemini_failures=0;gemini_open_until=0
def breaker_failure(kind):
    global gemini_failures,gemini_open_until
    if kind in ("gemini_timeout","gemini_rate_limited","gemini_service_unavailable") or kind.startswith("gemini_failed"):
        gemini_failures+=1
        if gemini_failures>=2:gemini_open_until=time.monotonic()+float(env("GEMINI_COOLDOWN_S","120"))
def breaker_success():reset_breaker()
def safe_error(error):
    message=str(error)
    key=gemini_key()
    if key: message=message.replace(key,"[REDACTED]")
    return message[:160]
def _empty(quality="poor"):
    keys=["vendor_name","vendor_tax_id","vendor_bank_account","invoice_number","invoice_date","due_date","po_reference","currency","subtotal","tax_amount","tax_included_in_line_items","total","line_items"]
    x={k:None for k in keys};x.update(currency="INR",line_items=[],field_confidence={k:None for k in ["invoice_number","invoice_date","total","po_reference","vendor_name"]},document_quality=quality,missing_fields=["invoice_number","invoice_date","total"],engine="offline")
    return x
def split_labels(text):
    return LABEL_ALTERNATION.sub(lambda m:"\n"+m.group(0),text)
def extract_offline_text(text):
    fields=_empty("clean")
    if len(text.strip())<40:
        fields["document_quality"]="scanned"
        return fields
    text=split_labels(text)
    lines=[x.strip() for x in text.splitlines() if x.strip() and "{" not in x and "}" not in x and not x.startswith("@") and not re.match(r"^(?:[.#]?[a-z-]+\s*\{|[a-z-]+\s*:)",x,re.I)]
    first=next((line for line in lines if not LABEL_ALTERNATION.match(line) and not re.match(r"^(description|qty|rate|amount)\b",line,re.I)),None)
    fields["vendor_name"]=re.split(r"(?i)(?:gstin|gst\s*reg|bank\s*a/c|invoice\s*(?:no|date)|bill\s*(?:no|date))\s*:?",first)[0].strip(" :-,.;") if first else None
    if fields["vendor_name"] and (":" in fields["vendor_name"] or fields["vendor_name"].isdigit()):fields["vendor_name"]=None
    patterns={
        "vendor_tax_id":r"(?:gstin|gst\s*reg\.?\s*no\.?|seller\s*gst\s*reg\.?\s*no\.?|gst\s*no\.?)\s*:?\s*([0-9A-Z]{15})",
        "vendor_bank_account":r"(?:bank\s*a/c|bank\s*account|a/c\s*no\.?|account\s*no\.?)\s*:?\s*([A-Z0-9-]+)",
        "invoice_number":r"(?:invoice\s*(?:no\.?|number|#)|bill\s*(?:no\.?|number)|inv\s*no\.?)\s*:?\s*([A-Z0-9][A-Z0-9()/ -]*)",
        "invoice_date":r"(?:invoice\s*date|date\s*of\s*issue|bill\s*date)\s*:?\s*([^:\n]+)",
        "due_date":r"(?:due\s*date|payment\s*due|pay\s*by)\s*:?\s*([^:\n]+)",
        "po_reference":r"(?:purchase\s*order\s*ref(?:erence)?|po\s*ref|po\s*(?:no\.?|number)?|p\.?\s*o\.?)\s*[:# -]*([A-Z]*-?\d+)",
    }
    for key,pattern in patterns.items():
        match=re.search(pattern,text,re.I)
        if match:
            value=match.group(1).strip(" :-,.;")
            fields[key]=f"PO-{value.lstrip('PO-')}" if key=="po_reference" and value.isdigit() else value
    amounts={
        "subtotal":r"(?:subtotal|sub\s*total|net\s*amount|taxable\s*(?:value|amount))\s*:?\s*(?:INR\s*)?([\d,]+(?:\.\d+)?)",
        "tax_amount":r"(?:gst\s*amount|tax\s*amount|tax\s*@\s*\d+(?:\.\d+)?%|gst\s*@\s*\d+(?:\.\d+)?%|igst|cgst)\s*:?\s*(?:INR\s*)?([\d,]+(?:\.\d+)?)",
        "total":r"(?:grand\s*total|total\s*due|amount\s*payable|amount\s*due|(?<!sub)total)\s*:?\s*(?:INR\s*)?([\d,]+(?:\.\d+)?)",
    }
    for key,pattern in amounts.items():
        match=re.search(pattern,text,re.I)
        if match: fields[key]=_num(match.group(1))
    req=["invoice_number","invoice_date","total","po_reference","vendor_name"]
    fields.update(field_confidence={k:.9 if fields[k] else None for k in req},missing_fields=[k for k in req if not fields[k]],engine="offline")
    return fields
def extract_offline(path):
    if not pdfplumber:return _empty()
    try:
        with pdfplumber.open(path) as p:text="\n".join(x.extract_text() or "" for x in p.pages)
    except Exception:return _empty()
    return extract_offline_text(text)
def extract_gemini(path,model=None):
    from google import genai
    from google.genai import types
    model=model or gemini_model()
    schema=["vendor_name","vendor_tax_id","vendor_bank_account","invoice_number","invoice_date","due_date","po_reference","currency","subtotal","tax_amount","tax_included_in_line_items","total","line_items","field_confidence","document_quality","missing_fields"]
    prompt="Return only JSON with keys "+", ".join(schema)+". Use null for anything not clearly visible. Never guess."
    client=genai.Client(api_key=gemini_key(),http_options=types.HttpOptions(timeout=int(float(env("GEMINI_TIMEOUT_S","12"))*1000)))
    response=client.models.generate_content(model=model,contents=[types.Part.from_bytes(data=open(path,"rb").read(),mime_type="application/pdf"),prompt],config=types.GenerateContentConfig(response_mime_type="application/json"))
    raw=response.text.strip()
    if raw.startswith("```"):raw=raw.split("\n",1)[1].rsplit("```",1)[0].strip()
    result=json.loads(raw);result["engine"]="gemini";return result
def gemini_error_kind(error):
    message=str(error).lower()
    if "429" in message or "quota" in message or "rate" in message:return "gemini_rate_limited"
    if "503" in message or "service unavailable" in message:return "gemini_service_unavailable"
    if "timeout" in message or "timed out" in message:return "gemini_timeout"
    if "404" in message or "not found" in message:return "gemini_model_not_found"
    if "401" in message or "403" in message or "api key" in message or "permission" in message:return "gemini_auth_error"
    return "gemini_failed: "+safe_error(error)
def extract_gemini_with_retry(path,model=None):
    deadline=time.monotonic()+float(env("GEMINI_TIMEOUT_S","12"))
    last=None
    for attempt in range(2):
        if time.monotonic()>=deadline: raise TimeoutError("Gemini timeout")
        try:return extract_gemini(path) if model is None else extract_gemini(path,model)
        except Exception as error:
            last=error;kind=gemini_error_kind(error)
            if attempt==1 or kind not in ("gemini_rate_limited","gemini_service_unavailable"):break
            delay=1
            match=re.search(r"(?:retry|delay)[^0-9]*(\d+(?:\.\d+)?)",str(error),re.I)
            delay=min(float(match.group(1)) if match else 1,3)
            if time.monotonic()+delay>=deadline:break
            time.sleep(delay)
    raise last
def _cache_path(path,model):
    h=hashlib.sha256((open(path,"rb").read()+model.encode()+PROMPT_VERSION.encode())).hexdigest()
    cache=ROOT/"cache";cache.mkdir(exist_ok=True);return cache/(h+".json")
def _offline_checks(x):
    failed=[]
    if x.get("document_quality")!="clean":failed.append("document_quality")
    for key in ("invoice_number","invoice_date","total"):
        if not x.get(key):failed.append(key)
    try:
        from .db import conn
        from rapidfuzz.fuzz import token_set_ratio
        with conn() as c:vendors=[dict(r) for r in c.execute("SELECT * FROM vendors")]
        matched=any(x.get("vendor_tax_id") and x["vendor_tax_id"].replace(" ","").upper()==str(v["tax_id"]).replace(" ","").upper() for v in vendors)
        with conn() as c:
            row=c.execute("SELECT value FROM policy WHERE key='vendor_match_min'").fetchone()
        threshold=float(row[0] if row else 75)
        matched=matched or any(token_set_ratio(x.get("vendor_name") or "",v["name"])>=threshold for v in vendors)
    except Exception:matched=False
    if not matched:failed.append("vendor")
    math_ok=not(x.get("subtotal") is not None and x.get("tax_amount") is not None and x.get("total") is not None and abs(x["subtotal"]+x["tax_amount"]-x["total"])>1)
    if not math_ok:failed.append("math_ok")
    if x.get("subtotal") is not None and x.get("total") is not None and x["total"]<x["subtotal"]:failed.append("total")
    if x.get("po_reference") is None and re.search(r"\b(?:po|purchase\s+order)\b",x.get("_text",""),re.I):failed.append("po_reference")
    return failed
def _gemini(path,model,cache_note=None):
    cp=_cache_path(path,model)
    if env("USE_CACHE","1")=="1" and cp.exists():
        x=json.loads(cp.read_text());x["engine"]="gemini";x["engine_note"]=cache_note or "from_cache";return x
    try:
        x=extract_gemini_with_retry(path) if model==gemini_model() else extract_gemini_with_retry(path,model)
    except Exception as error:
        kind=gemini_error_kind(error)
        fallbacks=[x.strip() for x in env("GEMINI_MODEL_FALLBACKS","").split(",") if x.strip()]
        if kind not in ("gemini_timeout","gemini_rate_limited","gemini_model_not_found"):raise
        x=None
        for fallback in fallbacks:
            try:
                x=extract_gemini_with_retry(path,fallback);model=fallback;cp=_cache_path(path,model);break
            except Exception as fallback_error:error=fallback_error
        if x is None:raise error
    x["engine"]="gemini";cp.write_text(json.dumps(x));breaker_success();return x
def extract(path):
    mode=extraction_mode()
    if gemini_disabled():mode="offline"
    offline=extract_offline(path)
    try:
        with pdfplumber.open(path) as p:offline["_text"]="\n".join(page.extract_text() or "" for page in p.pages)
    except Exception:offline["_text"]=""
    if mode=="offline":
        offline["engine_note"]="offline_mode";offline["mode"]=mode;offline["escalated"]=False;offline["escalation_reasons"]=[];offline.pop("_text",None);return offline
    if mode=="auto":
        failed=_offline_checks(offline)
        if not failed:
            offline["engine_note"]="offline_verified";offline["mode"]=mode;offline["escalated"]=False;offline["escalation_reasons"]=[];offline.pop("_text",None);return offline
        reasons=failed
    else:reasons=[]
    if not gemini_key():
        offline["engine_note"]="no_key";offline["mode"]=mode;offline["escalated"]=bool(reasons);offline["escalation_reasons"]=reasons;offline.pop("_text",None);return offline
    if time.monotonic()<gemini_open_until:
        offline["engine_note"]="gemini_skipped_cooldown";offline["mode"]=mode;offline["escalated"]=bool(reasons);offline["escalation_reasons"]=reasons;offline.pop("_text",None);return offline
    try:
        x=_gemini(path,gemini_model());x["mode"]=mode;x["escalated"]=bool(reasons);x["escalation_reasons"]=reasons;x["engine_note"]="escalated: "+", ".join(reasons) if reasons else x.get("engine_note");return x
    except Exception as e:
        note=gemini_error_kind(e);breaker_failure(note);offline["engine_note"]=("gemini_failed_after_escalation: " if reasons else "")+note;offline["mode"]=mode;offline["escalated"]=bool(reasons);offline["escalation_reasons"]=reasons;offline.pop("_text",None);return offline
