import re,json
from datetime import date
from rapidfuzz.fuzz import token_set_ratio
from .db import conn
def norm_inv(x): return re.sub(r"[^A-Z0-9]","",re.sub(r"\bREVISED\b","",str(x or "").upper()))
def net_total(x): return x.get("subtotal") if x.get("subtotal") is not None else (x.get("total") or 0)-(x.get("tax_amount") or 0)
def check_completeness(x):
    with conn() as c:m=c.execute("SELECT value FROM policy WHERE key='min_confidence'").fetchone()[0]
    req=["invoice_number","invoice_date","total"];missing=[k for k in req if not x.get(k)];low=[k for k in req if x.get("field_confidence",{}).get(k) is not None and x["field_confidence"][k]<m]
    return missing,low
def check_vendor(x):
    with conn() as c: vs=[dict(r) for r in c.execute("SELECT * FROM vendors")]
    exact=next((v for v in vs if x.get("vendor_tax_id")==v["tax_id"]),None)
    v=exact or max(vs,key=lambda z:token_set_ratio((x.get("vendor_name") or "").lower(),z["name"].lower()))
    score=100 if exact else token_set_ratio((x.get("vendor_name") or "").lower(),v["name"].lower())
    return v,score
def find_po(x,v):
    with conn() as c: ps=[dict(r) for r in c.execute("SELECT * FROM pos WHERE vendor_id=?",(v["id"],))]
    if x.get("po_reference"):
        p=next((p for p in ps if p["po_number"].upper()==x["po_reference"].upper()),None)
        if p:return p,"explicit"
    cs=[]
    for p in ps:
        amount=net_total(x) if p["basis"]=="net" else x.get("total") or 0
        if p["total"]-p["billed"]>=.98*amount: cs.append(p)
    return (cs[0],"inferred") if len(cs)==1 else (None,"ambiguous" if cs else "none")
def check_duplicates(run_id,x,v,amount,file_hash):
    with conn() as c:
        rows=c.execute("SELECT * FROM runs WHERE id<>? AND vendor_id=? AND status IN ('APPROVED','NEEDS_REVIEW')",(run_id,v["id"])).fetchall()
    for r in rows:
        old=json.loads(r["extracted"] or "{}")
        same_number=norm_inv(old.get("invoice_number"))==norm_inv(x.get("invoice_number"))
        same_amount=abs((r["amount"] or 0)-amount)<1
        try: within=abs((date.fromisoformat(old.get("invoice_date"))-date.fromisoformat(x.get("invoice_date"))).days)<=7
        except Exception: within=False
        if r["file_hash"]==file_hash: return r,"exact"
        if same_number or (same_amount and within):
            return r,"fuzzy"
    return None
def check_amount(x,v,p):
    amount=net_total(x) if p["basis"]=="net" else x.get("total") or 0
    remaining=p["total"]-p["billed"];over=amount-remaining
    soft=min(remaining*v["tolerance_pct"]/100,v["tolerance_abs"]) if v["tolerance_abs"] else remaining*v["tolerance_pct"]/100
    return amount,remaining,over,("ok" if over<=soft else "soft" if over<=remaining*0.05 else "hard")
