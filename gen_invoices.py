from pathlib import Path
import re
try:
 from jinja2 import Template
except Exception:
 Template=None
try:
 from weasyprint import HTML
except Exception:
 HTML=None
ROOT=Path(__file__).parent;OUT=ROOT/"samples";OUT.mkdir(exist_ok=True)
data=[("01_happy","Apex Office Supplies Pvt Ltd","27AAACA1234F1Z5","HDFC0001234-5566","INV-2231","2026-11-02","PO-1001",100000,18000,118000),("02_split_2of3","BlueRiver Logistics","29AABCB5678K1Z2","ICIC0004321-7788","BRL-0087","2026-11-03","PO-1042",84745.76,15254.24,100000),("03_split_overbill","BlueRiver Logistics","29AABCB5678K1Z2","ICIC0004321-7788","BRL-0091","2026-11-05","PO-1042",127118.64,22881.36,150000),("05_fuzzy_dup","Apex Office Supplies Pvt Ltd","27AAACA1234F1Z5","HDFC0001234-5566","INV2231 (Revised)","2026-11-03","PO-1001",100000,18000,118000),("07_tax_embedded","Delta Cloud Services","06AADCD3456M1Z4","AXIS0006666-3344","DCS-5501","2026-11-04","PO-1077",200000,36000,236000),("08_blocked_vendor","Shady Traders","33AAECS7890N1Z1","KKBK0007777-9900","ST-009","2026-11-04","",50000,9000,59000),("09_no_po","Cedar Print Works","07AAFCC9012L1Z9","SBIN0005555-1122","CPW-310","2026-11-06","",50000,9000,59000),("10_bank_change","Delta Cloud Services","06AADCD3456M1Z4","AXIS0009999-0000","DCS-5502","2026-11-07","PO-1077",200000,36000,236000)]
tpls=[]
if Template:
    common="{% if po %}<p>PO: {{po}}</p>{% endif %}<table><tr><th>Description</th><th>Qty</th><th>Unit price</th><th>Amount</th></tr><tr><td>Goods and services</td><td>1</td><td>INR {{sub}}</td><td>INR {{sub}}</td></tr><tr><td>GST 18%</td><td></td><td></td><td>INR {{gst}}</td></tr></table><p>Subtotal: INR {{sub}}</p><p>GST Amount: INR {{gst}}</p><p>Total: INR {{total}}</p>"
    head="<header><h1>{{v}}</h1><p>GSTIN: {{tax}}</p><p>Bank A/C: {{bank}}</p><p>Invoice No: {{no}}</p><p>Invoice Date: {{date}}</p><p>Due Date: 2026-12-01</p></header>"
    css="<style>@page{size:A4;margin:18mm}body{font:12pt Arial,sans-serif}p,header,table{display:block;margin:8pt 0}table{width:100%;border-collapse:collapse}th,td{border:1px solid #333;padding:5pt;text-align:left}</style>"
    tpls=[Template(f"<html><head>{css}</head><body>{head}{common}</body></html>"),Template(f"<html><head>{css}</head><body><div>{head}</div><div>{common}</div></body></html>"),Template(f"<html><head>{css}</head><body><section>{head}{common}</section></body></html>")]
def html_lines(text):
    text=re.sub(r"<style\b[^>]*>.*?</style>","",text,flags=re.I|re.S)
    plain=re.sub(r"<br\s*/?>","\n",text,flags=re.I)
    plain=re.sub(r"</(td|th)>"," ",plain,flags=re.I)
    plain=re.sub(r"</(h1|h2|p|div|tr|li|table|section|body)>","\n",plain,flags=re.I)
    plain=re.sub(r"<[^>]+>","",plain)
    return [re.sub(r"\s+"," ",x).strip() for x in plain.splitlines() if x.strip()]
def pdf(name,text):
    if HTML:
        try:
            HTML(string=text).write_pdf(str(OUT/(name+".pdf")))
            return
        except Exception:
            pass
    lines=html_lines(text)
    stream="BT\n/F1 11 Tf\n50 760 Td\n"
    for line in lines:
        safe=line.replace("\\","\\\\").replace("(","\\(").replace(")","\\)")
        stream+=f"({safe}) Tj\n0 -18 Td\n"
    stream+="ET\n"
    objects=[
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream.encode('latin-1'))} >>\nstream\n{stream}endstream",
    ]
    pdf_data="%PDF-1.4\n"
    offsets=[]
    for i,obj in enumerate(objects,1):
        offsets.append(len(pdf_data.encode("latin-1")))
        pdf_data+=f"{i} 0 obj\n{obj}\nendobj\n"
    xref=len(pdf_data.encode("latin-1"))
    pdf_data+=f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n"
    pdf_data+="".join(f"{offset:010d} 00000 n \n" for offset in offsets)
    pdf_data+=f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    (OUT/(name+".pdf")).write_bytes(pdf_data.encode("latin-1"))
for i,(n,v,t,b,no,d,po,s,g,total) in enumerate(data):
    kw=dict(v=v,tax=t,bank=b,no=no,date=d,po=po,sub=f"{s:,.2f}",gst=f"{g:,.2f}",total=f"{total:,.2f}")
    text=tpls[i%len(tpls)].render(**kw) if tpls else ""
    pdf(n,text)
(OUT/"04_exact_dup.pdf").write_bytes((OUT/"01_happy.pdf").read_bytes())
def scanned(name):
    try:
        from PIL import Image,ImageDraw,ImageFilter
        import random
        img=Image.new("RGB",(1000,1400),"white")
        ImageDraw.Draw(img).multiline_text((70,90),"Cedar Print Works\nGSTIN blurred\nInvoice No unreadable\nTotal unreadable",fill=(20,20,20),spacing=16)
        img=img.rotate(3,expand=True,fillcolor="white").filter(ImageFilter.GaussianBlur(5))
        px=img.load();rng=random.Random(6)
        for _ in range(8000):
            x,y=rng.randrange(img.width),rng.randrange(img.height);g=rng.randrange(40,180);px[x,y]=(g,g,g)
        img.convert("RGB").save(OUT/(name+".pdf"),"PDF")
    except Exception:
        pdf(name,"")
scanned("06_scanned_missing")
