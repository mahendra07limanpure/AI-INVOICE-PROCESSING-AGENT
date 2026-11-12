import argparse,json,time,os
from urllib.request import Request,urlopen

FILES=["01_happy.pdf","04_exact_dup.pdf","05_fuzzy_dup.pdf","02_split_2of3.pdf","03_split_overbill.pdf","06_scanned_missing.pdf","07_tax_embedded.pdf","08_blocked_vendor.pdf","09_no_po.pdf","10_bank_change.pdf"]
EXPECTED=[("APPROVED",None),("REJECTED","DUPLICATE_EXACT"),("NEEDS_REVIEW","DUPLICATE_SUSPECTED"),("APPROVED",None),("REJECTED","OVER_BILLING"),("ON_HOLD","MISSING_FIELDS"),("APPROVED",None),("REJECTED","VENDOR_NOT_APPROVED"),("APPROVED","PO_MATCHED_INFERRED"),("NEEDS_REVIEW","BANK_DETAILS_CHANGED")]
def call(base,path,method="GET",body=None):
    data=json.dumps(body).encode() if body is not None else None
    r=urlopen(Request(base+path,data=data,method=method,headers={"content-type":"application/json"}),timeout=15)
    return json.load(r)
def main():
    p=argparse.ArgumentParser(description="Run the deterministic demo. Use DISABLE_GEMINI=1 for stable results because Gemini may read blurred scan 06 differently.")
    p.add_argument("--base-url",default="http://127.0.0.1:8000");p.add_argument("--keep",action="store_true");a=p.parse_args();base=a.base_url.rstrip("/");failed=False
    try:
        call(base,"/api/reset","POST")
        for name,(status,code) in zip(FILES,EXPECTED):
            run=call(base,"/api/runs/sample","POST",{"name":name})["id"];end=time.time()+90
            while time.time()<end:
                data=call(base,f"/api/runs/{run}")["run"]
                if data["status"]!="RUNNING":break
                time.sleep(.5)
            ok=data["status"]==status and (code is None or code in json.loads(data["reason_codes"] or "[]"))
            failed|=not ok
            extracted=json.loads(data.get("extracted") or "{}")
            fast=os.environ.get("EXTRACTION_MODE")!="offline" or int(data.get("extract_ms") or 0)<2000
            ok=ok and fast;failed|=not ok
            print(("PASS" if ok else "FAIL"),name,status,code or "",extracted.get("engine"),extracted.get("engine_note"),data.get("extract_ms"))
        pos={x["po_number"]:x["billed"] for x in call(base,"/api/pos")};stats=call(base,"/api/stats")
        checks=[pos=={"PO-1001":118000.0,"PO-1042":200000.0,"PO-1050":59000.0,"PO-1077":200000.0},stats["total"]==10,stats["by_status"]=={"APPROVED":4,"REJECTED":3,"NEEDS_REVIEW":2,"ON_HOLD":1},stats["approved_value"]==477000,stats["held_value"]==354000,stats["stp_rate"]==40]
        for label,ok in zip(["PO totals","total","statuses","approved value","held value","STP rate"],checks):failed|=not ok;print(("PASS" if ok else "FAIL"),label)
    finally:
        if not a.keep:call(base,"/api/reset","POST")
    return 1 if failed else 0
if __name__=="__main__":raise SystemExit(main())
