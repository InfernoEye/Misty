import sys; sys.dont_write_bytecode = True
from app.brain.egress import classify_egress
u = "https://example.com/?key=whatever"
print(len(u), classify_egress("fetch_web_page", {"url": u}).reason_code)
