import json,hashlib,time,traceback
from .db import conn
from .events import emit
from .extract import extract
from .rules import normalize_extracted,check_completeness,check_vendor,find_po,check_duplicates,check_amount,same_bank,net_total,policy
STAGES=["ingest","extract","confidence_gate","vendor_check","po_match","duplicate_check","financial_validation","decision"]
class Halt(Exception): pass
def run(run_id,path):
    status="FAILED";codes=[];explain="";email=None;x={};v=None;p=None;amount=None;h=None;done=set()
    def begin(s): emit(run_id,s,"running",{});return time.time()
    def end(s,t,d): done.add(s);emit(run_id,s,"done",d,int((time.time()-t)*1000))
    def halt(st,cs,ex,em=None):
        nonlocal status,codes,explain,email
        status,codes,explain,email=st,cs,ex,em
        raise Halt
    try:
        t=begin("ingest");h=hashlib.sha256(open(path,"rb").read()).hexdigest();end("ingest",t,{"hash":h[:12]})
        t=begin("extract");x=normalize_extracted(extract(path),path);end("extract",t,x)
        t=begin("confidence_gate");missing,low=check_completeness(x);bad=missing+low
        end("confidence_gate",t,{"missing":missing,"low":low,"engine":x.get("engine"),"grounded":x.get("grounded"),"math_ok":x.get("math_ok")})
        if bad: halt("ON_HOLD",["MISSING_FIELDS"]+["FIELD_"+k.upper() for k in bad],"Required fields were missing or could not be verified in the document: "+", ".join(bad)+".","Please provide a clearer invoice showing: "+", ".join(bad))
        t=begin("vendor_check");v,score=check_vendor(x)
        if not v or score<policy("vendor_match_min",75) or v["status"]!="approved":
            end("vendor_check",t,{"vendor":v["name"] if v else None,"score":score,"result":"rejected"})
            known=bool(v) and score>=policy("vendor_match_min",75)
            if not known: v=None
            halt("REJECTED",["VENDOR_NOT_APPROVED"],"Vendor is blocked or not approved in the vendor master." if known else f"Vendor could not be matched to the vendor master (best match score {score}).")
        d={"vendor":v["name"],"score":score}
        if x.get("vendor_bank_account") and not same_bank(x["vendor_bank_account"],v["bank_account"]):
            end("vendor_check",t,{**d,"result":"bank_changed"})
            halt("NEEDS_REVIEW",["BANK_DETAILS_CHANGED"],"Bank details differ from the vendor master; possible payment fraud, verify with the vendor by phone before paying.")
        end("vendor_check",t,d)
        t=begin("po_match");p,method=find_po(x,v)
        if not p:
            end("po_match",t,{"method":method,"result":"not_found"})
            msg={"not_found":f"Invoice cites {x.get('po_reference')}, which is not on file for this vendor.","ambiguous":"Several open purchase orders could fit and none could be chosen confidently."}.get(method,"No purchase order was cited and none could be inferred.")
            halt("ON_HOLD",["PO_AMBIGUOUS" if method=="ambiguous" else "PO_NOT_FOUND"],msg,"Please confirm the purchase order number for this invoice.")
        end("po_match",t,{"po":p["po_number"],"method":method})
        amount=net_total(x) if p["basis"]=="net" else x["total"]
        t=begin("duplicate_check");dup=check_duplicates(run_id,x,v,amount,h)
        if dup:
            old,kind=dup
            end("duplicate_check",t,{"result":kind,"earlier_run":old["id"]})
            if kind=="exact": halt("REJECTED",["DUPLICATE_EXACT"],f"Exact duplicate of run {old['id']}.")
            halt("NEEDS_REVIEW",["DUPLICATE_SUSPECTED"],f"Possible duplicate of run {old['id']} (same vendor, matching invoice number or amount and date). It may be a corrected invoice, so a human must decide.")
        end("duplicate_check",t,{"result":"none"})
        t=begin("financial_validation");amount,rem,over,result,pct=check_amount(x,v,p)
        end("financial_validation",t,{"invoice":amount,"po_remaining":rem,"variance_pct":pct,"basis":p["basis"],"result":result})
        if result=="soft": halt("NEEDS_REVIEW",["VARIANCE_SOFT"],f"Invoice of {amount:,.2f} is {over:,.2f} over the remaining PO balance of {rem:,.2f}. It is inside the review band, so a human must check it.")
        if result=="hard": halt("REJECTED",["OVER_BILLING"],f"Invoice of {amount:,.2f} exceeds the remaining PO balance of {rem:,.2f} by {over:,.2f}, beyond the hard tolerance.")
        status="APPROVED";codes=["VENDOR_OK","PO_MATCHED_"+method.upper(),"WITHIN_TOLERANCE"]
        explain=f"Approved on {p['basis']} basis: vendor verified, PO {p['po_number']} matched ({method}), amount {amount:,.2f} within tolerance. Remaining PO balance after approval: {rem-amount:,.2f}."
    except Halt: pass
    except Exception as e: status="FAILED";explain=str(e);codes=["PIPELINE_ERROR"];traceback.print_exc()
    for s in STAGES[:-1]:
        if s not in done: emit(run_id,s,"skipped",{})
    emit(run_id,"decision","running",{"status":status})
    with conn() as c:
        c.execute("UPDATE runs SET file_hash=?,status=?,reason_codes=?,explanation=?,extracted=?,po_number=?,amount=?,vendor_id=?,email_draft=? WHERE id=?",(h,status,json.dumps(codes),explain,json.dumps(x),p["po_number"] if p else None,amount,v["id"] if v else None,email,run_id))
        if status=="APPROVED" and p: c.execute("UPDATE pos SET billed=billed+? WHERE po_number=?",(amount,p["po_number"]))
    emit(run_id,"decision","done",{"status":status,"codes":codes,"explanation":explain})