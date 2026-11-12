from pathlib import Path
try:
    from weasyprint import HTML
except Exception:
    HTML=None
ROOT=Path(__file__).resolve().parents[1]
out=ROOT/"tests"/"unseen"/"unseen_layout.pdf";out.parent.mkdir(parents=True,exist_ok=True)
html="""<html><body><h1>TAX INVOICE</h1><table><tr><td>Bill No.<br>AOS/26-27/0457</td><td>Date of Issue<br>03 Nov 2026<br>Payment Due<br>02 Dec 2026</td></tr><tr><td>Purchase Order Ref<br>PO-1001</td><td>Seller GST Reg. No.<br>27AAACA1234F1Z5</td></tr></table><p>Apex Office Supplies Pvt Ltd</p><p>Bank account: HDFC0001234-5566</p><table border="1" width="100%"><tr><th>description</th><th>qty</th><th>rate</th><th>amount</th></tr><tr><td>Office supplies</td><td>1</td><td>100000.00</td><td>100000.00</td></tr></table><p>Net Amount: 100,000.00</p><p>Tax @18%: 18,000.00</p><h2>Amount Payable: 118,000.00</h2></body></html>"""
if HTML:
    HTML(string=html).write_pdf(out)
else:
    text="TAX INVOICE\nBill No. AOS/26-27/0457\nDate of Issue 03 Nov 2026\nPayment Due 02 Dec 2026\nPurchase Order Ref PO-1001\nSeller GST Reg. No. 27AAACA1234F1Z5\nApex Office Supplies Pvt Ltd\nBank account HDFC0001234-5566\ndescription qty rate amount\nOffice supplies 1 100000.00 100000.00\nNet Amount 100,000.00\nTax @18% 18,000.00\nAmount Payable 118,000.00"
    stream="BT /F1 10 Tf 40 760 Td ("+text.replace("\\","\\\\").replace("(","\\(").replace(")","\\)").replace("\n",") Tj 0 -18 Td (")+" ) Tj ET"
    objects=["<< /Type /Catalog /Pages 2 0 R >>","<< /Type /Pages /Kids [3 0 R] /Count 1 >>","<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>","<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>","<< /Length "+str(len(stream.encode()))+" >>\nstream\n"+stream+"\nendstream"]
    pdf="%PDF-1.4\n";offsets=[]
    for i,obj in enumerate(objects,1):offsets.append(len(pdf.encode()));pdf+=f"{i} 0 obj\n{obj}\nendobj\n"
    start=len(pdf.encode());pdf+="xref\n0 6\n0000000000 65535 f \n"+"".join(f"{x:010} 00000 n \n" for x in offsets)+"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n"+str(start)+"\n%%EOF"
    out.write_bytes(pdf.encode())
print(out)
