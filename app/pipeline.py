import json,hashlib,time,os
from .db import conn
from .events import emit
from .extract import extract
from .rules import *
STAGES=["ingest","extract","confidence_gate","vendor_check","po_match","duplicate_check","financial_validation","decision"]
def run(run_id,path):
    status="FAILED";codes=[];explain="";email=None;x={};v=None;p=None;amount=None
    try:
        t=time.time();h=hashlib.sha256(open(path,"rb").read()).hexdigest();emit(run_id,"ingest","running",{"hash":h[:12]});emit(run_id,"ingest","done",{"hash":h[:12]},int((time.time()-t)*1000))
        t=time.time();emit(run_id,"extract","running",{});x=extract(path);emit(run_id,"extract","done",x,int((time.time()-t)*1000))
        emit(run_id,"confidence_gate","running",{});missing,low=check_completeness(x)
        if missing or low:
            emit(run_id,"confidence_gate","done",{"missing":missing,"low":low})
            codes=["MISSING_FIELDS"]+["FIELD_"+k.upper() for k in missing+low];status="ON_HOLD";email="Please provide a clearer invoice showing: "+", ".join(missing+low);explain="Required fields were unreadable: "+", ".join(missing+low);raise StopIteration
        emit(run_id,"confidence_gate","done",{"missing":missing,"low":low})
        emit(run_id,"vendor_check","running",{});v,score=check_vendor(x)
        if score<75 or v["status"]!="approved":
            emit(run_id,"vendor_check","done",{"vendor":v["name"],"score":score,"result":"rejected"})
            codes=["VENDOR_NOT_APPROVED"];status="REJECTED";explain="Vendor is not approved in the vendor master.";raise StopIteration
        if x.get("vendor_bank_account") and x["vendor_bank_account"]!=v["bank_account"]:
            emit(run_id,"vendor_check","done",{"vendor":v["name"],"score":score,"result":"bank_changed"})
            codes=["BANK_DETAILS_CHANGED"];status="NEEDS_REVIEW";explain="Bank details differ from the master; possible payment fraud, verify by phone.";raise StopIteration
        emit(run_id,"vendor_check","done",{"vendor":v["name"],"score":score})
        emit(run_id,"po_match","running",{});p,method=find_po(x,v)
        if not p:
            emit(run_id,"po_match","done",{"method":method,"result":"not_found"})
            codes=["PO_NOT_FOUND"];status="ON_HOLD";email="Please provide the purchase order number for this invoice.";explain="No unambiguous purchase order was found.";raise StopIteration
        emit(run_id,"po_match","done",{"po":p["po_number"],"method":method})
        amount=net_total(x) if p["basis"]=="net" else x["total"]
        emit(run_id,"duplicate_check","running",{});rh=h
        dup=check_duplicates(run_id,x,v,amount,rh)
        if dup:
            old,kind=dup
            if kind=="exact": codes=["DUPLICATE_EXACT"];status="REJECTED";explain=f"Exact duplicate of run {old['id']}."
            else: codes=["DUPLICATE_SUSPECTED"];status="NEEDS_REVIEW";explain=f"Possible duplicate of run {old['id']} requires human review."
            emit(run_id,"duplicate_check","done",{"result":kind,"earlier_run":old["id"]});raise StopIteration
        emit(run_id,"duplicate_check","done",{})
        emit(run_id,"financial_validation","running",{});rem=p["total"]-p["billed"];over=amount-rem
        soft=min(rem*float(v["tolerance_pct"])/100,float(v["tolerance_abs"])) if v["tolerance_abs"] else rem*float(v["tolerance_pct"])/100
        if over<=0 or over<=soft: result="ok"
        elif over<=rem*5/100: result="soft";status="NEEDS_REVIEW";codes=["VARIANCE_SOFT"]
        else: result="hard";status="REJECTED";codes=["OVER_BILLING"]
        emit(run_id,"financial_validation","done",{"invoice":amount,"po_remaining":rem,"variance_pct":round(over/rem*100,2),"basis":p["basis"],"result":result})
        if status=="FAILED": status="APPROVED";codes=["VENDOR_OK","PO_MATCHED_"+method.upper(),"WITHIN_TOLERANCE"];explain=f"Approved on {p['basis']} basis; remaining PO balance after approval: {rem-amount:.2f}."
        elif not explain: explain="Invoice exceeds the permitted tolerance."
    except StopIteration: pass
    except Exception as e: status="FAILED";explain=str(e);codes=["PIPELINE_ERROR"]
    emit(run_id,"decision","running",{"status":status})
    with conn() as c:
        c.execute("UPDATE runs SET file_hash=?,status=?,reason_codes=?,explanation=?,extracted=?,po_number=?,amount=?,vendor_id=?,email_draft=? WHERE id=?",(locals().get("h"),status,json.dumps(codes),explain,json.dumps(x),p["po_number"] if p else None,amount,v["id"] if v else None,email,run_id))
        if status=="APPROVED" and p:c.execute("UPDATE pos SET billed=billed+? WHERE po_number=?",(amount,p["po_number"]))
    emit(run_id,"decision","done",{"status":status,"codes":codes,"explanation":explain})
