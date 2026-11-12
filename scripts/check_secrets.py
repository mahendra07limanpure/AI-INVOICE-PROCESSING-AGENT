import os,re,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
patterns=[re.compile(r"AIza[0-9A-Za-z_-]{35}"),re.compile(r"GEMINI_API_KEY\s*=\s*[\"']?[A-Za-z0-9_-]{20,}")]
bad=[]
files=subprocess.check_output(["git","ls-files","-co","--exclude-standard"],cwd=ROOT,text=True).splitlines()
for name in files:
    p=ROOT/name
    if p.name==".env.example" or not p.is_file():continue
    try:lines=p.read_text(errors="ignore").splitlines()
    except OSError:continue
    for n,line in enumerate(lines,1):
        if any(x.search(line) for x in patterns):bad.append((name,n))
for name,n in bad:print(f"{name}:{n}")
raise SystemExit(1 if bad else 0)
