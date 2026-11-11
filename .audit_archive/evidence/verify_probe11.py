import sys
sys.dont_write_bytecode = True
from app.brain import egress
for url in ["https://example.com/?token=SECRET123",
            "https://example.com/some/very/long/path",
            "https://example.com/?q=Diagnosis+for+Anna+Kowalska+patient+4417+ICD-10+F32.1"]:
    print(len(url), egress.classify_egress("fetch_web_page", {"url": url}).reason_code, url[:50])
print(egress.classify_egress("fetch_web_page", {"url": "https://example.com/?g-recaptcha-response=SECRET123"}).reason_code)
