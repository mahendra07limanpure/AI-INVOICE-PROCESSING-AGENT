import os,time,json
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()
from google import genai
from google.genai import types
ROOT=Path(__file__).resolve().parents[1]
models=[x.strip() for x in os.getenv("GEMINI_BENCH_MODELS",os.getenv("GEMINI_MODEL","gemini-2.5-flash-lite,gemini-2.5-flash")).split(",") if x.strip()]
key=os.getenv("GEMINI_API_KEY","")
best=None
for model in models:
    started=time.monotonic();error=None
    try:
        client=genai.Client(api_key=key,http_options=types.HttpOptions(timeout=20000))
        for filename in ("samples/01_happy.pdf","tests/unseen/unseen_layout.pdf"):
            client.models.generate_content(model=model,contents=types.Part.from_bytes(data=(ROOT/filename).read_bytes(),mime_type="application/pdf"))
        print(model,round(time.monotonic()-started,2),"ok")
        best=(model,time.monotonic()-started) if best is None or time.monotonic()-started<best[1] else best
    except Exception as exc:
        message=str(exc).lower()
        error="timeout" if "timeout" in message else "rate_limited" if "429" in message or "quota" in message else "not_found" if "404" in message or "not found" in message else "auth_error" if "401" in message or "403" in message or "api key" in message else "failed"
        print(model,round(time.monotonic()-started,2),error)
print("recommended model:",best[0] if best else "none")
