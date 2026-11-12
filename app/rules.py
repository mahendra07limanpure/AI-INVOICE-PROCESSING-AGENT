import re,json
from datetime import date,datetime
from rapidfuzz.fuzz import token_set_ratio
from .db import conn
try: import pdfplumber
except Exception: pdfplumber=None
NULLS={"","null","none","n/a","na","-","nil","not available"}
DATE_FORMATS=["%Y-%m-%d","%d/%m/%Y","%d-%m-%Y","%d.%m.%Y","%d/%m/%y","%d-%m-%y","%d %b %Y","%d %B %Y","%b %d, %Y","%B %d, %Y","%d-%b-%Y","%d-%b-%y"]
def policy(key,default):
    with conn() as c: r=c.execute("SELECT value FROM policy WHERE key=?",(key,)).fetchone()
    return r[0] if r else default
def clean(v):
    if v is None: return None
    s=re.sub(r"\s+"," ",str(v)).strip()
    return None if s.lower() in NULLS else s
def squash(s): return re.sub(r"[^A-Z0-9]","",str(s or "").upper())
def to_num(v):
    if v is None or isinstance(v,bool): return None
    if isinstance(v,(int,float)): return float(v)
    try: return float(re.sub(r"[^\d.\-]","",str(v)))
    except ValueError: return None
def to_iso(v):
    s=clean(v)
    if not s: return None
    s=re.sub(r"(\d)(st|nd|rd|th)\b",r"\1",s)
    for f in DATE_FORMATS:
        try: return datetime.strptime(s,f).date().isoformat()
        except ValueError: pass
    return None
def norm_po(v):
    s=clean(v)
    if not s: return None
    m=re.search(r"(?:P\.?\s?O\.?|purchase\s*order)[\s\-#:_/.]*(\d+)",s,re.I)
    if m:return f"PO-{m.group(1)}"
    return f"PO-{s}" if s.isdigit() else s.upper()
def same_bank(a,b): return bool(a) and squash(a)==squash(b)
def conf(v,val):
    if val is None: return None
    n=to_num(v)
    if n is None: return .8
    if n>1: n/=100
    return max(0.0,min(1.0,n))
def ground(x,path):
    if not pdfplumber: return
    try:
        with pdfplumber.open(path) as p: t="\n".join(pg.extract_text() or "" for pg in p.pages)
    except Exception: return
    if len(t.strip())<40: return
    s=squash(t);fc=x["field_confidence"];ok=True
    if x.get("invoice_number") and squash(x["invoice_number"]) not in s: fc["invoice_number"]=.3;ok=False
    g=x.get("total")
    if g is not None and not any(squash(f) in s for f in (f"{g:.2f}",f"{g:.0f}")): fc["total"]=.3;ok=False
    x["grounded"]=ok
def normalize_extracted(x,path=None):
    x=dict(x or {})
    for k in ("vendor_name","invoice_number","currency"): x[k]=clean(x.get(k))
    x["vendor_tax_id"]=squash(x.get("vendor_tax_id")) or None
    x["vendor_bank_account_raw"]=clean(x.get("vendor_bank_account"))
    x["vendor_bank_account"]=squash(x.get("vendor_bank_account")) or None
    x["invoice_date"]=to_iso(x.get("invoice_date"))
    x["due_date"]=to_iso(x.get("due_date"))
    x["po_reference"]=norm_po(x.get("po_reference"))
    for k in ("subtotal","tax_amount","total"): x[k]=to_num(x.get(k))
    x["currency"]=(x["currency"] or "INR").upper()
    x["line_items"]=x["line_items"] if isinstance(x.get("line_items"),list) else []
    q=(clean(x.get("document_quality")) or "").lower()
    x["document_quality"]=q if q in ("clean","scanned","poor") else "clean"
    fc=x.get("field_confidence") if isinstance(x.get("field_confidence"),dict) else {}
    x["field_confidence"]={k:conf(fc.get(k),x.get(k)) for k in ["invoice_number","invoice_date","total","po_reference","vendor_name"]}
    s,t,g=x.get("subtotal"),x.get("tax_amount"),x.get("total")
    x["math_ok"]=not(s is not None and t is not None and g is not None and not x.get("tax_included_in_line_items") and abs(s+t-g)>1)
    if not x["math_ok"] and x["field_confidence"]["total"] is not None: x["field_confidence"]["total"]=min(x["field_confidence"]["total"],.4)
    x["grounded"]=None
    if x.get("engine")=="gemini" and path: ground(x,path)
    return x
def norm_inv(x): return re.sub(r"[^A-Z0-9]","",re.sub(r"\bREVISED\b","",str(x or "").upper()))
def net_total(x): return x.get("subtotal") if x.get("subtotal") is not None else (x.get("total") or 0)-(x.get("tax_amount") or 0)
def check_completeness(x):
    m=policy("min_confidence",.7)
    req=["invoice_number","invoice_date","total"]
    fc=x.get("field_confidence") or {}
    missing=[k for k in req if not x.get(k)]
    low=[k for k in req if fc.get(k) is not None and fc[k]<m]
    return missing,low
def check_vendor(x):
    with conn() as c: vs=[dict(r) for r in c.execute("SELECT * FROM vendors")]
    if not vs: return None,0
    name=(x.get("vendor_name") or "").lower()
    exact=next((v for v in vs if x.get("vendor_tax_id") and x["vendor_tax_id"]==squash(v["tax_id"])),None)
    v=exact or max(vs,key=lambda z:token_set_ratio(name,z["name"].lower()))
    return v,(100 if exact else token_set_ratio(name,v["name"].lower()))
def find_po(x,v):
    with conn() as c: ps=[dict(r) for r in c.execute("SELECT * FROM pos WHERE vendor_id=?",(v["id"],))]
    if x.get("po_reference"):
        p=next((p for p in ps if p["po_number"].upper()==x["po_reference"].upper()),None)
        return (p,"explicit") if p else (None,"not_found")
    cs=[p for p in ps if p["total"]-p["billed"]>=.98*(net_total(x) if p["basis"]=="net" else x.get("total") or 0)]
    return (cs[0],"inferred") if len(cs)==1 else (None,"ambiguous" if cs else "none")
def check_duplicates(run_id,x,v,amount,file_hash):
    with conn() as c:
        rows=[dict(r) for r in c.execute("SELECT * FROM runs WHERE id<>? AND vendor_id=? AND status IN ('APPROVED','NEEDS_REVIEW')",(run_id,v["id"]))]
    for r in rows:
        if r["file_hash"]==file_hash: return r,"exact"
    num=norm_inv(x.get("invoice_number"));window=policy("dup_window_days",7)
    for r in rows:
        old=json.loads(r["extracted"] or "{}")
        same_number=bool(num) and norm_inv(old.get("invoice_number"))==num
        same_amount=abs((r["amount"] or 0)-amount)<1
        try: within=abs((date.fromisoformat(old["invoice_date"])-date.fromisoformat(x["invoice_date"])).days)<=window
        except Exception: within=False
        if same_number or (same_amount and within): return r,"fuzzy"
    return None
def check_amount(x,v,p):
    amount=net_total(x) if p["basis"]=="net" else x.get("total") or 0
    remaining=p["total"]-p["billed"];over=amount-remaining
    soft=remaining*v["tolerance_pct"]/100
    if v["tolerance_abs"]: soft=min(soft,v["tolerance_abs"])
    hard=remaining*policy("hard_pct",5)/100
    variance_pct=round(over/remaining*100,2) if remaining else (0 if over<=0 else None)
    return amount,remaining,over,("ok" if over<=soft else "soft" if over<=hard else "hard"),variance_pct