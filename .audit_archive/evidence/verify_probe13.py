"""Probe: does the pinned opener resolve the hostname (second DNS lookup)?"""
import sys, socket, ssl
sys.dont_write_bytecode = True
import app.web.fetch as F
from app.web.fetch import FetchResult
calls = []
real_gai = socket.getaddrinfo
def fake_gai(host, port, *a, **k):
    calls.append((host, port))
    raise AssertionError("resolver consulted at connect time for %r" % (host,))
import ipaddress
F.socket.getaddrinfo = lambda h, p, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", p))]
dest = F._resolve_destination("https://example.com/")
F.socket.getaddrinfo = real_gai
print("validated:", dest)
real_connect = socket.socket.connect
attempts = []
def fake_connect(self, address):
    attempts.append((self.family, address))
    raise ConnectionRefusedError("blocked by probe")
socket.socket.connect = fake_connect
# also capture whether a DNS lookup happens inside the opener
F.socket.getaddrinfo = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("DNS at open time"))
try:
    resp = F._open_pinned(dest, 1.0)
except Exception as e:
    print("open raised:", type(e).__name__, str(e)[:80])
finally:
    F.socket.getaddrinfo = real_gai
    socket.socket.connect = real_connect
print("socket.connect targets:", attempts)
print("connect host is the validated IP:", all(a[1][0] == dest.connect_host for a in attempts))
