import json,time
from .db import conn
def emit(run_id,stage,state,detail,ms=0):
    with conn() as c:c.execute("INSERT INTO events(run_id,stage,state,detail,ms) VALUES(?,?,?,?,?)",(run_id,stage,state,json.dumps(detail,default=str),ms))
