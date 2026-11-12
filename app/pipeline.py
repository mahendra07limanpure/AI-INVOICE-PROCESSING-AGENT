import json,hashlib,time,logging
from .db import conn
from .events import emit
from .extract import extract,safe_error
from .rules import normalize_extracted,check_completeness,check_vendor,find_po,check_duplicates,check_amount,same_bank,net_total,policy
STAGES=["ingest","extract","confidence_gate","vendor_check","po_match","duplicate_check","financial_validation","decision"]
logger=logging.getLogger(__name__)
class Halt(Exception): pass
def run(run_id,path):
    status="FAILED";codes=[];explain="";email=None;x={};v=None;p=None;amount=None;h=None;done=set();evidence=[]
    def begin(s): emit(run_id,s,"running",{});return time.time()
    def end(s,t,d): done.add(s);emit(run_id,s,"done",d,int((time.time()-t)*1000))
    def text(value):
        if value is None or value=="": return "none"
        if isinstance(value,list): return ", ".join(text(v) for v in value) or "none"
        if isinstance(value,dict): return "; ".join(f"{k}: {text(v)}" for k,v in value.items())
        return str(value)
    def money(value):
        if value is None:return "none"
        return "INR {:,.2f}".format(float(value))
    def add(check,invoice,reference,result): evidence.append({"check":check,"invoice":text(invoice),"reference":text(reference),"result":result})
    def halt(st,cs,ex,em=None):
        nonlocal status,codes,explain,email
        status,codes,explain,email=st,cs,ex,em
        raise Halt
    try:
        t=begin("ingest");h=hashlib.sha256(open(path,"rb").read()).hexdigest();end("ingest",t,{"hash":h[:12],"file":path})
        t=begin("extract");extract_started=time.monotonic();x=normalize_extracted(extract(path),path);extract_ms=int((time.monotonic()-extract_started)*1000);x["extract_ms"]=extract_ms
        with conn() as c:c.execute("UPDATE runs SET invoice_total=?,engine=?,engine_note=?,extract_ms=? WHERE id=?",(x.get("total"),x.get("engine"),x.get("engine_note"),extract_ms,run_id))
        end("extract",t,{"mode":x.get("mode"),"escalated":x.get("escalated",False),"escalation_reasons":x.get("escalation_reasons",[]),"engine":x.get("engine"),"engine_note":x.get("engine_note"),"extract_ms":extract_ms})
        t=begin("confidence_gate");missing,low=check_completeness(x);bad=missing+low
        d={"missing":missing,"low":low,"engine":x.get("engine"),"engine_note":x.get("engine_note"),"grounded":x.get("grounded"),"math_ok":x.get("math_ok")}
        end("confidence_gate",t,d)
        add("Required fields","All required fields present" if not bad else bad, "Required invoice fields", "pass" if not bad else "warn")
        if bad:
            msg=", ".join(bad)
            halt("ON_HOLD",["MISSING_FIELDS"]+["FIELD_"+k.upper() for k in bad],f"We could not verify {msg} from the invoice. Please provide a clearer document with those fields.",f"Subject: Information needed for invoice {x.get('invoice_number') or 'submission'}\n\nPlease provide a clearer invoice showing: {msg}.")
        t=begin("vendor_check");v,score=check_vendor(x);matched_tax=bool(v and x.get("vendor_tax_id") and x["vendor_tax_id"]==v["tax_id"].replace(" ","").upper())
        bank_raw=x.get("vendor_bank_account_raw");master_bank=v["bank_account"] if v else None
        vd={"vendor":v["name"] if v else None,"score":score,"matched_by_tax_id":matched_tax,"vendor_status":v["status"] if v else None,"invoice_bank":bank_raw,"master_bank":master_bank,"match":bool(v and score>=policy("vendor_match_min",75))}
        if not v or score<policy("vendor_match_min",75) or v["status"]!="approved":
            vd["result"]="rejected";end("vendor_check",t,vd);add("Vendor approval",v["name"] if v else None, "approved vendor master", "fail")
            known=bool(v) and score>=policy("vendor_match_min",75)
            if not known:v=None
            halt("REJECTED",["VENDOR_NOT_APPROVED"],"The invoice vendor was not approved in the vendor master. Payment is blocked.",None)
        if bank_raw and not same_bank(bank_raw,master_bank):
            vd["result"]="bank_changed";end("vendor_check",t,vd);add("Bank details",bank_raw,master_bank,"warn")
            halt("NEEDS_REVIEW",["BANK_DETAILS_CHANGED"],"The invoice bank account differs from the approved vendor master. An approver must verify the change with the vendor before payment.",f"Internal review: verify the invoice bank account {bank_raw} against the vendor master account {master_bank} by phone before payment.")
        vd["result"]="matched";end("vendor_check",t,vd);add("Vendor approval",v["name"],v["status"],"pass")
        t=begin("po_match");p,method=find_po(x,v);pd={"po":p["po_number"] if p else x.get("po_reference"),"method":method,"po_total":p["total"] if p else None,"billed":p["billed"] if p else None,"remaining":p["total"]-p["billed"] if p else None,"basis":p["basis"] if p else None}
        if not p:
            pd["result"]="not_found";end("po_match",t,pd);add("Purchase order",x.get("po_reference"),None,"warn")
            msg={"not_found":f"Invoice cites {x.get('po_reference')}, which is not on file for this vendor.","ambiguous":"Several open purchase orders could fit this invoice."}.get(method,"No PO cited on the invoice")
            halt("ON_HOLD",["PO_AMBIGUOUS" if method=="ambiguous" else "PO_NOT_FOUND"],msg,f"Subject: Purchase order confirmation needed\n\nPlease confirm the purchase order number for invoice {x.get('invoice_number')}.")
        pd["result"]="matched";end("po_match",t,pd);add("Purchase order",x.get("po_reference"),p["po_number"],"pass")
        amount=net_total(x) if p["basis"]=="net" else x["total"]
        t=begin("duplicate_check");dup=check_duplicates(run_id,x,v,amount,h)
        old=None;kind="none"
        if dup:old,kind=dup
        od={"result":kind,"earlier_run":old["id"] if old else None,"invoice_number":x.get("invoice_number"),"earlier_invoice_number":json.loads(old["extracted"] or "{}").get("invoice_number") if old else None,"amount":amount,"earlier_amount":old["amount"] if old else None,"invoice_date":x.get("invoice_date"),"earlier_date":json.loads(old["extracted"] or "{}").get("invoice_date") if old else None}
        end("duplicate_check",t,od)
        if dup:
            add("Duplicate check",f"invoice {x.get('invoice_number')}, amount {money(amount)}, date {x.get('invoice_date')}",f"invoice {od['earlier_invoice_number']}, amount {money(od['earlier_amount'])}, date {od['earlier_date']}","fail" if kind=="exact" else "warn")
            if kind=="exact":halt("REJECTED",["DUPLICATE_EXACT"],f"This is an exact file duplicate of run {old['id']}. The invoice was rejected to prevent paying twice.")
            halt("NEEDS_REVIEW",["DUPLICATE_SUSPECTED"],f"This invoice resembles earlier run {old['id']} by invoice number or amount/date. An approver must confirm whether it is a corrected resend.",f"Internal review: compare invoice {x.get('invoice_number')} with earlier run {old['id']} before payment.")
        add("Duplicate check",x.get("invoice_number"),None,"pass")
        t=begin("financial_validation");amount,rem,over,result,pct=check_amount(x,v,p);soft=rem*v["tolerance_pct"]/100;soft=min(soft,v["tolerance_abs"]) if v["tolerance_abs"] else soft;hard=rem*policy("hard_pct",5)/100
        fd={"comparable_amount":amount,"remaining":rem,"over":over,"soft_threshold":soft,"hard_threshold":hard,"variance_pct":pct,"basis":p["basis"],"result":result}
        end("financial_validation",t,fd)
        add("Financial validation",f"{money(amount)} ({p['basis']} basis)",f"remaining {money(rem)}; soft limit {money(soft)}; hard limit {money(hard)}; variance {pct}%","pass" if result=="ok" else "warn" if result=="soft" else "fail")
        if result=="soft":halt("NEEDS_REVIEW",["VARIANCE_SOFT"],f"The comparable invoice amount is {amount:,.2f}, which is {over:,.2f} above the remaining PO balance of {rem:,.2f} but within the review tolerance. An approver must review it.",f"Internal review: invoice amount {amount:,.2f} exceeds remaining PO balance {rem:,.2f} by {over:,.2f}; please approve or reject.")
        if result=="hard":halt("REJECTED",["OVER_BILLING"],f"The comparable invoice amount is {amount:,.2f}, exceeding the remaining PO balance of {rem:,.2f} beyond the hard tolerance. Payment is blocked.")
        status="APPROVED";codes=["VENDOR_OK","PO_MATCHED_"+method.upper(),"WITHIN_TOLERANCE"];explain=f"We checked the approved vendor, matched PO {p['po_number']} on a {p['basis']} basis, found no duplicate, and compared {amount:,.2f} with the remaining balance of {rem:,.2f}. The invoice is within tolerance and is approved. The remaining PO balance will be {rem-amount:,.2f}."
    except Halt:pass
    except Exception as e:status="FAILED";explain=f"The invoice could not be processed because of an unexpected system error: {safe_error(e)}";codes=["PIPELINE_ERROR"]
    for s in STAGES[:-1]:
        if s not in done:emit(run_id,s,"skipped",{"reason":"not needed"})
    dd={"status":status,"codes":codes,"explanation":explain,"evidence":evidence};emit(run_id,"decision","running",{"status":status})
    with conn() as c:
        c.execute("UPDATE runs SET file_hash=?,status=?,reason_codes=?,explanation=?,extracted=?,po_number=?,amount=?,vendor_id=?,email_draft=?,evidence=?,engine=?,engine_note=?,extract_ms=? WHERE id=?",(h,status,json.dumps(codes),explain,json.dumps(x),p["po_number"] if p else None,amount,v["id"] if v else None,email,json.dumps(evidence),x.get("engine"),x.get("engine_note"),locals().get("extract_ms"),run_id))
        if status=="APPROVED" and p:c.execute("UPDATE pos SET billed=billed+? WHERE po_number=?",(amount,p["po_number"]))
    logger.info("run=%s file=%s engine=%s engine_note=%s extract_ms=%s",run_id,path,x.get("engine"),x.get("engine_note"),locals().get("extract_ms"))
    emit(run_id,"decision","done",dd)
