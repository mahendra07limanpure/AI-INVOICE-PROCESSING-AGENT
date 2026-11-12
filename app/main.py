import csv,io,os,json,shutil,threading,time,logging,hashlib
from datetime import datetime,timezone
from pathlib import Path
from dotenv import load_dotenv
ROOT=Path(__file__).resolve().parent.parent
load_dotenv(ROOT/".env",override=False)
from fastapi import FastAPI,UploadFile,File,HTTPException,Request
from fastapi.responses import FileResponse,JSONResponse,StreamingResponse
from fastapi.staticfiles import StaticFiles
from .db import conn,init
from .seed import run as seed
from .pipeline import run as pipeline
from .rules import net_total
from .extract import gemini_disabled,gemini_key,gemini_model,safe_error,extract_gemini_with_retry,extraction_mode,breaker_state,reset_breaker,breaker_success
UPLOAD=ROOT/"uploads";SAMPLES=ROOT/"samples";UPLOAD.mkdir(exist_ok=True)
logger=logging.getLogger(__name__)
warm_state={"state":"idle","done":0,"total":0,"errors":[]}
app=FastAPI()
@app.exception_handler(Exception)
async def errors(request:Request,exc:Exception):
    if isinstance(exc,HTTPException):return JSONResponse({"detail":exc.detail},status_code=exc.status_code)
    return JSONResponse({"detail":safe_error(exc)},status_code=500)
def recover_stale():
    with conn() as c:c.execute("UPDATE runs SET status='FAILED',reason_codes=?,explanation=? WHERE status='RUNNING' AND datetime(created_at)<datetime('now','-2 minutes')",(json.dumps(["PIPELINE_TIMEOUT"]),"The server restarted while this run was processing; it was marked failed."))
@app.on_event("startup")
def startup():
    init();seed();recover_stale()
    logger.info("engine_config gemini_configured=%s model=%s disabled=%s",bool(gemini_key()),gemini_model(),gemini_disabled())
def start(name,data):
    if not data.startswith(b"%PDF"):raise HTTPException(422,"File is not a valid PDF")
    if len(data)>10*1024*1024:raise HTTPException(413,"File too large (10 MB max)")
    with conn() as c:
        i=c.execute("INSERT INTO runs(filename,stored_path,status) VALUES(?,?,?)",(name,"","RUNNING")).lastrowid
        p=UPLOAD/f"{i}_{name}";c.execute("UPDATE runs SET stored_path=? WHERE id=?",(str(p),i))
    p.write_bytes(data);threading.Thread(target=pipeline,args=(i,str(p)),daemon=True).start();return {"id":i}
@app.get("/healthz")
def healthz():return {"ok":True}
@app.post("/api/runs")
async def create_run(file:UploadFile=File(...)):
    name=Path(file.filename or "invoice.pdf").name
    if not name.lower().endswith(".pdf"):raise HTTPException(422,"PDF required")
    return start(name,await file.read())
@app.post("/api/runs/sample")
def sample(body:dict):
    n=body.get("name","")
    if Path(n).name!=n or not n.lower().endswith(".pdf"):raise HTTPException(422,"invalid sample")
    src=SAMPLES/n
    if not src.exists():raise HTTPException(404,"sample not found")
    return start(n,src.read_bytes())
@app.get("/api/samples")
def samples():
    manifest=json.loads((SAMPLES/"manifest.json").read_text(encoding="utf-8")) if (SAMPLES/"manifest.json").exists() else {}
    return [dict({"name":p.name,"title":p.stem,"expected":None,"edge_case":False},**manifest.get(p.name,{})) for p in sorted(SAMPLES.glob("*.pdf"),key=lambda x:x.name)]
def run_row(row):
    r=dict(row);r["reason_codes"]=json.loads(r["reason_codes"] or "[]");r["display_amount"]=r["amount"] if r["amount"] is not None else r.get("invoice_total");r["amount"]=r["display_amount"];r["summary"]=(r.get("explanation") or "").split(". ",1)[0].strip(". ")
    with conn() as c:r["duration_ms"]=c.execute("SELECT COALESCE(SUM(ms),0) FROM events WHERE run_id=? AND state='done'",(r["id"],)).fetchone()[0]
    return r
@app.get("/api/runs")
def runs():
    with conn() as c:rows=c.execute("SELECT * FROM runs ORDER BY id DESC").fetchall()
    return [run_row(r) for r in rows]
@app.get("/api/runs/export.csv")
def export_csv():
    rows=runs();out=io.StringIO();fields=["id","filename","status","reason_codes","po_number","amount","summary","engine","engine_note","extract_ms","duration_ms","created_at"];w=csv.DictWriter(out,fieldnames=fields);w.writeheader()
    for r in rows:w.writerow({k:json.dumps(r[k]) if k=="reason_codes" else r.get(k) for k in fields})
    return StreamingResponse(iter([out.getvalue()]),media_type="text/csv",headers={"Content-Disposition":"attachment; filename=runs.csv"})
@app.get("/api/runs/{i}")
def get_run(i:int):
    with conn() as c:
        row=c.execute("SELECT * FROM runs WHERE id=?",(i,)).fetchone()
        if not row:raise HTTPException(404,"run not found")
        return {"run":dict(row),"events":[dict(r) for r in c.execute("SELECT * FROM events WHERE run_id=? ORDER BY id",(i,))]}
@app.get("/api/runs/{i}/export")
def export_run(i:int):return get_run(i)
@app.get("/api/runs/{i}/events")
def get_events(i:int,after:int=0):
    with conn() as c:
        if not c.execute("SELECT 1 FROM runs WHERE id=?",(i,)).fetchone():raise HTTPException(404,"run not found")
        return [dict(r) for r in c.execute("SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id",(i,after))]
@app.get("/api/runs/{i}/file")
def get_file(i:int):
    with conn() as c:p=c.execute("SELECT stored_path FROM runs WHERE id=?",(i,)).fetchone()
    if not p or not p[0] or not Path(p[0]).exists():raise HTTPException(404,"file not found")
    return FileResponse(p[0],media_type="application/pdf")
@app.get("/api/runs/{i}/preview.png")
def preview(i:int):
    with conn() as c:row=c.execute("SELECT stored_path,file_hash FROM runs WHERE id=?",(i,)).fetchone()
    if not row:raise HTTPException(404,"run not found")
    path=Path(row["stored_path"] or "")
    if not path.exists():raise HTTPException(404,"file not found")
    cache=ROOT/"cache"/"previews";cache.mkdir(parents=True,exist_ok=True)
    target=cache/((row["file_hash"] or hashlib.sha256(path.read_bytes()).hexdigest())+".png")
    if target.exists():return FileResponse(target,media_type="image/png",headers={"Cache-Control":"max-age=3600"})
    try:
        import pdfplumber
        image=pdfplumber.open(path).pages[0].to_image(resolution=160).original
        image.save(target,format="PNG")
    except Exception as e:raise HTTPException(422,f"preview rendering failed: {safe_error(e)}")
    return FileResponse(target,media_type="image/png",headers={"Cache-Control":"max-age=3600"})
@app.post("/api/runs/{i}/action")
def action(i:int,b:dict):
    a=b.get("action");note=(b.get("note") or "").strip()
    override=b.get("override") is True
    if a not in ("approve","reject","request_info"):raise HTTPException(422,"invalid action")
    with conn() as c:
        r=c.execute("SELECT * FROM runs WHERE id=?",(i,)).fetchone()
        if not r:raise HTTPException(404,"run not found")
        r=dict(r)
        if r["status"] not in ("NEEDS_REVIEW","ON_HOLD"):raise HTTPException(409,"Only NEEDS_REVIEW or ON_HOLD runs can be actioned")
        s={"approve":"APPROVED","reject":"REJECTED","request_info":"ON_HOLD"}[a];po_number=r["po_number"];amount=r["amount"]
        if a=="approve" and not po_number:
            ex=json.loads(r["extracted"] or "{}");ref=str(b.get("po_number") or ex.get("po_reference") or "").strip().upper()
            po=c.execute("SELECT * FROM pos WHERE po_number=? AND vendor_id=?",(ref,r["vendor_id"])).fetchone() if ref and r["vendor_id"] else None
            if po:po_number=po["po_number"];amount=net_total(ex) if po["basis"]=="net" else ex.get("total")
        if a=="approve" and (not po_number or amount is None):raise HTTPException(409,"Cannot approve: no matching PO or amount. Supply a po_number, reject, or request more info.")
        remaining=None
        if a=="approve" and po_number:
            po_row=c.execute("SELECT total,billed FROM pos WHERE po_number=?",(po_number,)).fetchone()
            remaining=po_row["total"]-po_row["billed"] if po_row else None
            if remaining is not None and amount>remaining and not (override and note):raise HTTPException(409,"Approval would exceed the PO balance")
        codes=json.loads(r["reason_codes"] or "[]")+["HUMAN_"+a.upper()]
        c.execute("UPDATE runs SET status=?,reason_codes=?,po_number=?,amount=? WHERE id=?",(s,json.dumps(codes),po_number,amount,i))
        if a=="approve":c.execute("UPDATE pos SET billed=billed+? WHERE po_number=?",(amount,po_number))
        c.execute("INSERT INTO events(run_id,stage,state,detail) VALUES(?,?,?,?)",(i,"human_review","done",json.dumps({"action":a,"note":note,"status":s,"override":override,"timestamp":datetime.now(timezone.utc).isoformat()})))
    return {"ok":True,"status":s}
@app.get("/api/pos")
def pos():
    with conn() as c:return [dict(r) for r in c.execute("SELECT *,total-billed remaining FROM pos")]
@app.get("/api/vendors")
def vendors():
    with conn() as c:return [dict(r) for r in c.execute("SELECT * FROM vendors")]
@app.get("/api/policy")
def policy():
    with conn() as c:pol=[dict(r) for r in c.execute("SELECT * FROM policy")]
    disabled=gemini_disabled();configured=bool(gemini_key())
    return {"policy":pol,"engine":"disabled" if disabled else "gemini" if configured else "offline","engine_note":"disabled by DISABLE_GEMINI" if disabled else None,"gemini_configured":configured,"model":gemini_model(),"extraction_mode":extraction_mode(),"assumptions":["Two-way match only (invoice vs PO)","Single currency (INR)","Vendor master and PO ledger live in SQLite","Invoices are compared on each PO's basis (net or gross)","Partial billing is allowed","The extractor only reads fields; deterministic rules make every decision","All test invoices are synthetic"]}
@app.post("/api/engine/test")
def engine_test():
    if gemini_disabled():return {"ok":False,"error":"disabled"}
    if not gemini_key():return {"ok":False,"error":"no key configured"}
    try:
        from google import genai
        from google.genai import types
        client=genai.Client(api_key=gemini_key(),http_options=types.HttpOptions(timeout=12000))
        client.models.generate_content(model=gemini_model(),contents="Reply OK",config={"max_output_tokens":2})
        breaker_success()
        return {"ok":True,"model":gemini_model()}
    except Exception as e:return {"ok":False,"error":safe_error(e)}
@app.get("/api/engine/status")
def engine_status():
    with conn() as c:
        rows=[dict(r) for r in c.execute("SELECT id,filename,engine,engine_note,extract_ms FROM runs ORDER BY id DESC LIMIT 5")]
    return {"engine":"disabled" if gemini_disabled() else "gemini" if gemini_key() else "offline","model":gemini_model(),"gemini_configured":bool(gemini_key()),"disabled":gemini_disabled(),"extraction_mode":extraction_mode(),"breaker":breaker_state(),"last_runs":rows}
@app.put("/api/engine/mode")
def update_engine_mode(body:dict):
    mode=body.get("mode")
    if mode not in ("auto","gemini_first","offline"):raise HTTPException(422,"mode must be auto, gemini_first, or offline")
    with conn() as c:c.execute("INSERT OR REPLACE INTO app_settings(key,value) VALUES('extraction_mode',?)",(mode,))
    return {"mode":mode}
@app.post("/api/engine/breaker/reset")
def reset_engine_breaker():
    reset_breaker();return {"ok":True,"breaker":breaker_state()}
def warm_worker(files):
    global warm_state
    errors=[];done=0;rate_limits=0
    for path in files:
        h=hashlib.sha256(path.read_bytes()).hexdigest();cp=ROOT/"cache"/(h+".json");cp.parent.mkdir(exist_ok=True)
        if cp.exists():done+=1;warm_state.update(done=done);continue
        try:
            result=extract_gemini_with_retry(str(path));cp.write_text(json.dumps(result));done+=1
        except Exception as e:
            reason=safe_error(e);errors.append({"file":path.name,"reason":reason});rate_limits+=1 if "429" in reason or "rate" in reason.lower() else 0
            if rate_limits>=2:break
        warm_state.update(done=done,errors=errors)
        if path!=files[-1]:time.sleep(6)
    warm_state.update(state="done",done=done,errors=errors)
@app.post("/api/engine/warm")
def engine_warm():
    global warm_state
    if warm_state["state"]=="running":return warm_state
    if not gemini_key() or gemini_disabled():return {"state":"done","done":0,"total":0,"errors":[{"file":"","reason":"Gemini unavailable"}]}
    files=sorted(SAMPLES.glob("*.pdf"));warm_state={"state":"running","done":0,"total":len(files),"errors":[]}
    threading.Thread(target=warm_worker,args=(files,),daemon=True).start()
    return warm_state
@app.get("/api/engine/warm")
def engine_warm_status():return warm_state
@app.put("/api/policy/{key}")
def update_policy(key:str,b:dict):
    try:value=float(b.get("value"))
    except (TypeError,ValueError):raise HTTPException(422,"value must be a positive number")
    if value<=0:raise HTTPException(422,"value must be a positive number")
    with conn() as c:
        if not c.execute("SELECT 1 FROM policy WHERE key=?",(key,)).fetchone():raise HTTPException(404,"unknown policy key")
        c.execute("UPDATE policy SET value=? WHERE key=?",(value,key))
    return {"key":key,"value":value}
@app.get("/api/stats")
def stats():
    with conn() as c:
        rows=[dict(r) for r in c.execute("SELECT status,amount,invoice_total,reason_codes FROM runs")]
        totals=[r[0] for r in c.execute("SELECT SUM(ms) FROM events WHERE state='done' AND stage!='ingest' GROUP BY run_id").fetchall()]
        avg=sum(totals)/len(totals) if totals else 0
        ordered=sorted(totals);median=ordered[len(ordered)//2] if len(ordered)%2 else (ordered[len(ordered)//2-1]+ordered[len(ordered)//2])/2 if ordered else 0
    by={};reasons={}
    for r in rows:
        by[r["status"]]=by.get(r["status"],0)+1
        for code in json.loads(r["reason_codes"] or "[]"):
            if code not in ("VENDOR_OK","PO_MATCHED_EXPLICIT","PO_MATCHED_INFERRED","WITHIN_TOLERANCE") and not code.startswith("HUMAN_"):reasons[code]=reasons.get(code,0)+1
    approved=sum(r["amount"] or 0 for r in rows if r["status"]=="APPROVED");held=sum(r["invoice_total"] or 0 for r in rows if r["status"] in ("ON_HOLD","NEEDS_REVIEW"))
    stp=sum(1 for r in rows if r["status"]=="APPROVED" and "HUMAN_" not in (r["reason_codes"] or ""))
    return {"total":len(rows),"by_status":by,"by_reason":reasons,"approved_value":approved,"held_value":held,"approved_vs_held_value":{"approved":approved,"held":held},"stp_rate":stp/len(rows)*100 if rows else 0,"avg_ms":round(avg),"median_ms":round(median)}
@app.post("/api/reset")
def reset():
    with conn() as c:
        c.execute("DELETE FROM events");c.execute("DELETE FROM runs");c.execute("DELETE FROM sqlite_sequence WHERE name IN ('runs','events')")
    for f in UPLOAD.glob("*"):
        if f.is_file():f.unlink()
    seed(reset=True);return {"ok":True}
app.mount("/",StaticFiles(directory=ROOT/"app"/"static",html=True),name="static")
