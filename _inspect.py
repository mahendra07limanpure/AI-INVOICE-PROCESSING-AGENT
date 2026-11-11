import sqlite3
c = sqlite3.connect("data.db")
c.row_factory = sqlite3.Row
print("TABLES", [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")])
print("--- RUNS ---")
for r in c.execute("SELECT id,filename,status,reason_codes,po_number,amount,vendor_id,substr(extracted,1,600) ex FROM runs ORDER BY id"):
    print(dict(r))
    print("---")
print("--- EVENTS ---")
for r in c.execute("SELECT id,run_id,stage,state,substr(detail,1,240) d FROM events ORDER BY id"):
    print(dict(r))
