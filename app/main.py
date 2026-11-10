import os,shutil,json,threading,hashlib
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
@app.post("/api/runs")
async def create_run(file:UploadFile=File(...)):
    name=Path(file.filename or "invoice.pdf").name
    if not name.lower().endswith(".pdf"): raise HTTPException(422,"PDF required")
    p=UPLOAD/name;p.write_bytes(await file.read())
    with conn() as c:
        r=c.execute("INSERT INTO runs(filename,stored_path,status) VALUES(?,?,?)",(name,str(p),"RUNNING"));i=r.lastrowid
    threading.Thread(target=pipeline,args=(i,str(p)),daemon=True).start();return {"id":i}
@app.post("/api/runs/sample")
def sample(body:dict):
    n=body.get("name","")
    if Path(n).name!=n or not n.lower().endswith(".pdf"): raise HTTPException(422,"invalid sample")
    src=SAMPLES/n
    if not src.exists():raise HTTPException(404)
    p=UPLOAD/n;shutil.copyfile(src,p)
    with conn() as c:i=c.execute("INSERT INTO runs(filename,stored_path,status) VALUES(?,?,?)",(n,str(p),"RUNNING")).lastrowid
    threading.Thread(target=pipeline,args=(i,str(p)),daemon=True).start();return {"id":i}
@app.get("/api/samples")
def samples(): return sorted([p.name for p in SAMPLES.glob("*.pdf")])
@app.get("/api/runs")
def runs():
    with conn() as c:return [dict(r) for r in c.execute("SELECT id,filename,status,reason_codes,po_number,amount,created_at FROM runs ORDER BY id DESC")]
@app.get("/api/runs/{i}")
def get_run(i:int):
    with conn() as c:
        row=c.execute("SELECT * FROM runs WHERE id=?",(i,)).fetchone()
        if not row: raise HTTPException(404,"run not found")
        return {"run":dict(row),"events":[dict(r) for r in c.execute("SELECT * FROM events WHERE run_id=?",(i,))]}
@app.get("/api/runs/{i}/events")
def get_events(i:int,after:int=0):
    with conn() as c:
        if not c.execute("SELECT 1 FROM runs WHERE id=?",(i,)).fetchone(): raise HTTPException(404,"run not found")
        return [dict(r) for r in c.execute("SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id",(i,after))]
@app.get("/api/runs/{i}/file")
def get_file(i:int):
    with conn() as c:p=c.execute("SELECT stored_path FROM runs WHERE id=?",(i,)).fetchone()
    if not p or not Path(p[0]).exists(): raise HTTPException(404,"file not found")
    return FileResponse(p[0],media_type="application/pdf")
@app.post("/api/runs/{i}/action")
def action(i:int,b:dict):
    a=b.get("action")
    if a not in ("approve","reject","request_info"): raise HTTPException(422,"invalid action")
    with conn() as c:
        r=c.execute("SELECT * FROM runs WHERE id=?",(i,)).fetchone()
        if not r or r["status"] not in ("NEEDS_REVIEW","ON_HOLD"):raise HTTPException(400)
        s={"approve":"APPROVED","reject":"REJECTED","request_info":"ON_HOLD"}.get(a)
        po_number=r["po_number"]
        amount=r["amount"]
        if a=="approve" and not po_number:
            extracted=json.loads(r["extracted"] or "{}")
            po_number=extracted.get("po_reference")
            if po_number:
                po=c.execute("SELECT * FROM pos WHERE po_number=? AND vendor_id=?",(po_number,r["vendor_id"])).fetchone()
                if po:
                    amount=net_total(extracted) if po["basis"]=="net" else extracted.get("total")
        c.execute("UPDATE runs SET status=?,explanation=?,po_number=?,amount=? WHERE id=?",(s,b.get("note") or r["explanation"],po_number,amount,i))
        if a=="approve" and po_number and amount is not None:c.execute("UPDATE pos SET billed=billed+? WHERE po_number=?",(amount,po_number))
        c.execute("INSERT INTO events(run_id,stage,state,detail) VALUES(?,?,?,?)",(i,"human_review","done",json.dumps(b)))
    return {"ok":True}
@app.get("/api/pos")
def pos():
    with conn() as c:return [dict(r) for r in c.execute("SELECT *,total-billed remaining FROM pos")]
@app.get("/api/vendors")
def vendors():
    with conn() as c:return [dict(r) for r in c.execute("SELECT * FROM vendors")]
@app.get("/api/policy")
def policy():
    with conn() as c:return {"policy":[dict(r) for r in c.execute("SELECT * FROM policy")],"assumptions":["Two-way match only","Single currency (INR)","Vendor master and PO ledger live in SQLite","Partial billing is allowed","Rules are deterministic"]}
@app.get("/api/stats")
def stats():
    with conn() as c:
        rows=[dict(r) for r in c.execute("SELECT status,amount FROM runs")];by={}
        for r in rows:by[r["status"]]=by.get(r["status"],0)+1
        ap=[r["amount"] or 0 for r in rows if r["status"]=="APPROVED"]
        return {"total":len(rows),"by_status":by,"approved_value":sum(ap),"stp_rate":len(ap)/len(rows)*100 if rows else 0,"avg_ms":0}
@app.post("/api/reset")
def reset():
    with conn() as c:
        c.execute("DELETE FROM events");c.execute("DELETE FROM runs")
    seed(reset=True)
    return {"ok":True}
app.mount("/",StaticFiles(directory=ROOT/"app"/"static",html=True),name="static")
