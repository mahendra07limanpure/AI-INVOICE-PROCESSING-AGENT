import json,time
from pathlib import Path
import subprocess,sys
import pytest
from fastapi.testclient import TestClient
from app import db
from app.rules import normalize_extracted,same_bank,norm_inv,check_amount
from app.extract import extract_offline_text

ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture
def client(tmp_path,monkeypatch):
    db.DB=tmp_path/"data.db"
    monkeypatch.setenv("USE_CACHE","0")
    monkeypatch.setenv("DISABLE_GEMINI","1")
    from app.main import app
    with TestClient(app) as c:
        yield c

def test_rules():
    x=normalize_extracted({"invoice_date":"31/12/2026","po_reference":"po 1001","total":"1,18,000.00","field_confidence":{"total":95},"subtotal":100,"tax_amount":10})
    assert x["invoice_date"]=="2026-12-31"
    assert x["po_reference"]=="PO-1001"
    assert x["total"]==118000.0
    assert normalize_extracted({"total":"118000","field_confidence":{"total":95}})["field_confidence"]["total"]==.95
    assert x["math_ok"] is False and x["field_confidence"]["total"]<.7
    assert same_bank("HDFC-0001234-5566","hdfc0001234-5566")
    assert norm_inv("INV2231 (Revised)")=="INV2231"
    v={"tolerance_pct":2,"tolerance_abs":500}
    assert check_amount({"total":100},v,{"basis":"gross","total":1000,"billed":900})[3]=="ok"
    assert check_amount({"total":105},v,{"basis":"gross","total":1000,"billed":900})[3]=="soft"
    assert check_amount({"total":200},v,{"basis":"gross","total":1000,"billed":900})[3]=="hard"
    assert check_amount({"subtotal":100,"tax_amount":18,"total":118},v,{"basis":"net","total":100,"billed":0})[0]==100

def test_offline_layouts():
    x=extract_offline_text("Apex Office Supplies Pvt LtdGSTIN: 27AAACA1234F1Z5 Bank A/C: HDFC0001234-5566 Invoice No: INV-2231 Invoice Date: 2026-11-02 Due Date: 2026-12-01 PO: PO-1001Subtotal: INR 100,000.00 GST Amount: INR 18,000.00 Total: INR 118,000.00")
    assert (x["vendor_name"],x["po_reference"],x["subtotal"],x["tax_amount"],x["total"])==("Apex Office Supplies Pvt Ltd","PO-1001",100000,18000,118000)
    x=extract_offline_text("Bill No. AOS/26-27/0457 Date of Issue 03 Nov 2026 Payment Due 02 Dec 2026 Purchase Order Ref PO-1001 Net Amount 100,000.00 Tax @18% 18,000.00 Amount Payable 118,000.00")
    assert (x["invoice_number"],x["po_reference"],x["subtotal"],x["tax_amount"],x["total"])==("AOS/26-27/0457","PO-1001",100000,18000,118000)

def test_generated_samples_parse_without_css():
    expected=json.loads((ROOT/"tests"/"expected_fields.json").read_text())
    import pdfplumber
    for name,values in expected.items():
        with pdfplumber.open(ROOT/"samples"/name) as pdf:text="\n".join(page.extract_text() or "" for page in pdf.pages)
        lines=[line.strip() for line in text.splitlines() if line.strip()]
        assert all(token not in text for token in ("@page","{","}"))
        assert lines[0]==values[0]
        labels=("GSTIN:","Bank A/C:","Invoice No:","Invoice Date:","Due Date:","Subtotal:","GST Amount:","Total:")
        assert all(any(line.startswith(label) for line in lines) for label in labels)
        if values[5]: assert any(line.startswith("PO:") for line in lines)
        result=extract_offline_text(text)
        assert [result.get(k) for k in ("vendor_name","vendor_tax_id","vendor_bank_account","invoice_number")]==values[:4]
        assert result["invoice_date"]==values[4]
        assert result["po_reference"]==values[5]
        assert result["subtotal"]==values[6]
        assert result["tax_amount"]==values[7]
        assert result["total"]==values[8]
    with pdfplumber.open(ROOT/"samples"/"06_scanned_missing.pdf") as pdf:
        assert not any((page.extract_text() or "").strip() for page in pdf.pages)

def wait_run(client,run_id):
    for _ in range(80):
        data=client.get(f"/api/runs/{run_id}").json()
        if any(e["stage"]=="decision" and e["state"]=="done" for e in data["events"]):return data
        time.sleep(.05)
    raise AssertionError("run did not finish")

def test_demo_sequence(client):
    assert client.get("/healthz").json()=={"ok":True}
    assert client.post("/api/reset").status_code==200
    names=["01_happy.pdf","04_exact_dup.pdf","05_fuzzy_dup.pdf","02_split_2of3.pdf","03_split_overbill.pdf","06_scanned_missing.pdf","07_tax_embedded.pdf","08_blocked_vendor.pdf","09_no_po.pdf","10_bank_change.pdf"]
    expected=[("APPROVED",""),("REJECTED","DUPLICATE_EXACT"),("NEEDS_REVIEW","DUPLICATE_SUSPECTED"),("APPROVED",""),("REJECTED","OVER_BILLING"),("ON_HOLD","MISSING_FIELDS"),("APPROVED",""),("REJECTED","VENDOR_NOT_APPROVED"),("APPROVED","PO_MATCHED_INFERRED"),("NEEDS_REVIEW","BANK_DETAILS_CHANGED")]
    for name,(status,code) in zip(names,expected):
        r=client.post("/api/runs/sample",json={"name":name});assert r.status_code==200
        data=wait_run(client,r.json()["id"]);assert data["run"]["status"]==status
        assert "invoice_total" in data["run"]
        if code:assert code in json.loads(data["run"]["reason_codes"])
    pos={x["po_number"]:x["billed"] for x in client.get("/api/pos").json()}
    assert pos=={"PO-1001":118000.0,"PO-1042":200000.0,"PO-1050":59000.0,"PO-1077":200000.0}

def test_review_updates_once(client):
    client.post("/api/reset")
    r=client.post("/api/runs/sample",json={"name":"10_bank_change.pdf"})
    data=wait_run(client,r.json()["id"]);i=r.json()["id"]
    assert client.post(f"/api/runs/{i}/action",json={"action":"approve","note":"verified"}).status_code==200
    assert client.post(f"/api/runs/{i}/action",json={"action":"approve"}).status_code==409

def test_approval_override(client):
    client.post("/api/reset")
    first=client.post("/api/runs/sample",json={"name":"01_happy.pdf"}).json()
    wait_run(client,first["id"])
    second=client.post("/api/runs/sample",json={"name":"05_fuzzy_dup.pdf"}).json()
    wait_run(client,second["id"])
    url=f"/api/runs/{second['id']}/action"
    assert client.post(url,json={"action":"approve","note":"not enough"}).status_code==409
    assert client.post(url,json={"action":"approve","override":True,"note":"Finance approved exception"}).status_code==200
    events=client.get(f"/api/runs/{second['id']}").json()["events"]
    assert json.loads(events[-1]["detail"])["override"] is True

def test_secret_scanner():
    result=subprocess.run([sys.executable,"scripts/check_secrets.py"],cwd=ROOT,capture_output=True,text=True)
    assert result.returncode==0,result.stdout

def test_gemini_fallback_reasons(tmp_path,monkeypatch):
    import app.extract as extractor
    pdf=tmp_path/"x.pdf";pdf.write_bytes(b"%PDF-1.4")
    monkeypatch.delenv("DISABLE_GEMINI",raising=False);monkeypatch.setenv("GEMINI_API_KEY","test-key");monkeypatch.setenv("USE_CACHE","0");monkeypatch.setattr(extractor.time,"sleep",lambda _:None)
    monkeypatch.setenv("EXTRACTION_MODE","gemini_first")
    monkeypatch.setattr(extractor,"extract_gemini",lambda _: (_ for _ in ()).throw(RuntimeError("429 quota")))
    assert extractor.extract(pdf)["engine_note"]=="gemini_rate_limited"
    monkeypatch.setattr(extractor,"extract_gemini",lambda _: (_ for _ in ()).throw(TimeoutError("timed out")))
    assert extractor.extract(pdf)["engine_note"]=="gemini_timeout"
    assert "test-key" not in extractor.safe_error(RuntimeError("test-key"))

def test_unseen_offline(client):
    pdf=ROOT/"tests"/"unseen"/"unseen_layout.pdf"
    if not pdf.exists(): pytest.skip("unseen PDF not generated")
    client.post("/api/reset")
    response=client.post("/api/runs",files={"file":("unseen_layout.pdf",pdf.read_bytes(),"application/pdf")})
    data=wait_run(client,response.json()["id"])
    assert data["run"]["status"]=="APPROVED"

@pytest.mark.skipif(not __import__("os").environ.get("GEMINI_API_KEY") or __import__("os").environ.get("DISABLE_GEMINI")=="1",reason="Gemini not configured")
def test_unseen_gemini(client,monkeypatch):
    monkeypatch.delenv("DISABLE_GEMINI",raising=False)
    pdf=ROOT/"tests"/"unseen"/"unseen_layout.pdf"
    client.post("/api/reset")
    response=client.post("/api/runs",files={"file":("unseen_layout.pdf",pdf.read_bytes(),"application/pdf")})
    data=wait_run(client,response.json()["id"])
    extracted=json.loads(data["run"]["extracted"])
    if extracted.get("engine")!="gemini":pytest.skip("Gemini unavailable")
    assert extracted["engine"]=="gemini"
    assert extracted["invoice_date"]=="2026-11-03"
    assert extracted["po_reference"]=="PO-1001"
    assert extracted["total"]==118000
    assert data["run"]["status"]=="APPROVED"
