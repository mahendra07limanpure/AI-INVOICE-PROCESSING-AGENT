import os,shutil,json,threading
from pathlib import Path
from fastapi import FastAPI,UploadFile,File,HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from .db import conn,init
from .seed import run as seed
from .pipeline import run as pipeline
from .rules import net_total
ROOT=Path(__file__).resolve().parent.parent;UPLOAD=ROOT/"uploads";SAMPLES=ROOT/"samples";UPLOAD.mkdir(exist_ok=True)
app=FastAPI()
@app.on_event("startup")
def startup():
    init();seed()
def start(name,data):
    if not data.startswith(b"%PDF"): raise HTTPException(422,"File is not a valid PDF")
    if len(data)>10*1024*1024: raise HTTPException(413,"File too large (10 MB max)")
    with conn() as c:
        i=c.execute("INSERT INTO runs(filename,stored_path,status) VALUES(?,?,?)",(name,"","RUNNING")).lastrowid
        p=UPLOAD/f"{i}_{name}";c.execute("UPDATE runs SET stored_path=? WHERE id=?",(str(p),i))
    p.write_bytes(data)
    threading.Thread(target=pipeline,args=(i,str(p)),daemon=True).start()
    return {"id":i}
@app.post("/api/runs")
async def create_run(file:UploadFile=File(...)):
    name=Path(file.filename or "invoice.pdf").name
    if not name.lower().endswith(".pdf"): raise HTTPException(422,"PDF required")
    return start(name,await file.read())
@app.post("/api/runs/sample")
def sample(body:dict):
    n=body.get("name","")
    if Path(n).name!=n or not n.lower().endswith(".pdf"): raise HTTPException(422,"invalid sample")
    src=SAMPLES/n
    if not src.exists(): raise HTTPException(404,"sample not found")
    return start(n,src.read_bytes())
@app.get("/api/samples")
def samples(): return sorted([p.name for p in SAMPLES.glob("*.pdf")])
@app.get("/api/runs")
def runs():
    with conn() as c: return [dict(r) for r in c.execute("SELECT id,filename,status,reason_codes,po_number,amount,created_at FROM runs ORDER BY id DESC")]
@app.get("/api/runs/{i}")
def get_run(i:int):
    with conn() as c:
        row=c.execute("SELECT * FROM runs WHERE id=?",(i,)).fetchone()
        if not row: raise HTTPException(404,"run not found")
        return {"run":dict(row),"events":[dict(r) for r in c.execute("SELECT * FROM events WHERE run_id=? ORDER BY id",(i,))]}
@app.get("/api/runs/{i}/events")
def get_events(i:int,after:int=0):
    with conn() as c:
        if not c.execute("SELECT 1 FROM runs WHERE id=?",(i,)).fetchone(): raise HTTPException(404,"run not found")
        return [dict(r) for r in c.execute("SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id",(i,after))]
@app.get("/api/runs/{i}/file")
def get_file(i:int):
    with conn() as c: p=c.execute("SELECT stored_path FROM runs WHERE id=?",(i,)).fetchone()
    if not p or not p[0] or not Path(p[0]).exists(): raise HTTPException(404,"file not found")
    return FileResponse(p[0],media_type="application/pdf")
@app.post("/api/runs/{i}/action")
def action(i:int,b:dict):
    a=b.get("action");note=(b.get("note") or "").strip()
    if a not in ("approve","reject","request_info"): raise HTTPException(422,"invalid action")
    with conn() as c:
        r=c.execute("SELECT * FROM runs WHERE id=?",(i,)).fetchone()
        if not r: raise HTTPException(404,"run not found")
        r=dict(r)
        if r["status"] not in ("NEEDS_REVIEW","ON_HOLD"): raise HTTPException(409,"Only NEEDS_REVIEW or ON_HOLD runs can be actioned")
        s={"approve":"APPROVED","reject":"REJECTED","request_info":"ON_HOLD"}[a]
        po_number=r["po_number"];amount=r["amount"]
        if a=="approve":
            if not po_number:
                ex=json.loads(r["extracted"] or "{}")
                ref=str(b.get("po_number") or ex.get("po_reference") or "").strip().upper()
                po=c.execute("SELECT * FROM pos WHERE po_number=? AND vendor_id=?",(ref,r["vendor_id"])).fetchone() if ref and r["vendor_id"] else None
                if po: po_number=po["po_number"];amount=net_total(ex) if po["basis"]=="net" else ex.get("total")
            if not po_number or amount is None: raise HTTPException(409,"Cannot approve: no matching PO or amount. Supply a po_number, reject, or request more info.")
        codes=json.loads(r["reason_codes"] or "[]")+["HUMAN_"+a.upper()]
        expl=(r["explanation"] or "")+" | Human review ("+a.replace("_"," ")+")"+(": "+note if note else "")
        c.execute("UPDATE runs SET status=?,reason_codes=?,explanation=?,po_number=?,amount=? WHERE id=?",(s,json.dumps(codes),expl,po_number,amount,i))
        if a=="approve": c.execute("UPDATE pos SET billed=billed+? WHERE po_number=?",(amount,po_number))
        c.execute("INSERT INTO events(run_id,stage,state,detail) VALUES(?,?,?,?)",(i,"human_review","done",json.dumps({"action":a,"note":note,"status":s})))
    return {"ok":True,"status":s}
@app.get("/api/pos")
def pos():
    with conn() as c: return [dict(r) for r in c.execute("SELECT *,total-billed remaining FROM pos")]
@app.get("/api/vendors")
def vendors():
    with conn() as c: return [dict(r) for r in c.execute("SELECT * FROM vendors")]
@app.get("/api/policy")
def policy():
    with conn() as c: pol=[dict(r) for r in c.execute("SELECT * FROM policy")]
    return {"policy":pol,"engine":"gemini" if os.getenv("GEMINI_API_KEY") else "offline","assumptions":["Two-way match only (invoice vs PO)","Single currency (INR)","Vendor master and PO ledger live in SQLite","Invoices are compared on each PO's basis (net or gross)","Partial billing is allowed","The extractor only reads fields; deterministic rules make every decision","All test invoices are synthetic"]}
@app.get("/api/stats")
def stats():
    with conn() as c:
        rows=[dict(r) for r in c.execute("SELECT status,amount,reason_codes FROM runs")]
        avg=c.execute("SELECT AVG(t) FROM (SELECT SUM(ms) t FROM events WHERE state='done' GROUP BY run_id)").fetchone()[0]
    by={}
    for r in rows: by[r["status"]]=by.get(r["status"],0)+1
    ap=[r for r in rows if r["status"]=="APPROVED"]
    stp=[r for r in ap if "HUMAN_" not in (r["reason_codes"] or "")]
    return {"total":len(rows),"by_status":by,"approved_value":sum(r["amount"] or 0 for r in ap),"stp_rate":len(stp)/len(rows)*100 if rows else 0,"avg_ms":round(avg or 0)}
@app.post("/api/reset")
def reset():
    with conn() as c:
        c.execute("DELETE FROM events");c.execute("DELETE FROM runs")
    for f in UPLOAD.glob("*"): f.unlink(missing_ok=True)
    seed(reset=True)
    return {"ok":True}
app.mount("/",StaticFiles(directory=ROOT/"app"/"static",html=True),name="static")