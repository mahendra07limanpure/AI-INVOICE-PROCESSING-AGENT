import sqlite3
from pathlib import Path
DB=Path(__file__).resolve().parent.parent/"data.db"
def conn():
    c=sqlite3.connect(DB,timeout=10)
    c.row_factory=sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c
def init():
    with conn() as c:
        c.executescript("""CREATE TABLE IF NOT EXISTS vendors(id TEXT PRIMARY KEY,name TEXT,tax_id TEXT,bank_account TEXT,status TEXT,tolerance_pct REAL,tolerance_abs REAL);
CREATE TABLE IF NOT EXISTS pos(po_number TEXT PRIMARY KEY,vendor_id TEXT,total REAL,billed REAL,basis TEXT);
CREATE TABLE IF NOT EXISTS policy(key TEXT PRIMARY KEY,value REAL);
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY AUTOINCREMENT,filename TEXT,stored_path TEXT,file_hash TEXT,status TEXT,reason_codes TEXT,explanation TEXT,extracted TEXT,po_number TEXT,amount REAL,vendor_id TEXT,email_draft TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,run_id INTEGER,stage TEXT,state TEXT,detail TEXT,ms INTEGER,created_at TEXT DEFAULT CURRENT_TIMESTAMP);""")
