import csv,sys
from pathlib import Path
try: from .db import conn,init
except ImportError: from db import conn,init
ROOT=Path(__file__).resolve().parent.parent
def run(reset=False):
    init()
    with conn() as c:
        if c.execute("SELECT COUNT(*) FROM vendors").fetchone()[0]:
            if reset:
                with open(ROOT/"data"/"pos.csv",newline="",encoding="utf-8") as h:
                    c.executemany("UPDATE pos SET billed=? WHERE po_number=?",[(r["billed"],r["po_number"]) for r in csv.DictReader(h)])
            return
        for f,table in [("vendors.csv","vendors"),("pos.csv","pos")]:
            with open(ROOT/"data"/f,newline="",encoding="utf-8") as h:
                rows=list(csv.DictReader(h))
            cols=list(rows[0])
            c.executemany(f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?'*len(cols))})",[tuple(r.values()) for r in rows])
        c.executemany("INSERT INTO policy VALUES(?,?)",[("hard_pct",5),("dup_window_days",7),("min_confidence",.7),("vendor_match_min",75)])
if __name__=="__main__":
    sys.path.insert(0,str(ROOT));run()
