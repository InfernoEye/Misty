import sys; sys.dont_write_bytecode=True
from app.brain.egress import classify_egress
samples = [
 "SSN 536-07-1234 of Ann Maria Doe",
 "card 4111 1111 1111 1111 exp 04/29",
 "my email anna.kowalska@gmail.com",
 "rent dispute with landlord at Flat 4B, 12 Green Road, Leeds, LS1 2AB",
 "123-45-6789",
 "password hunter2",
]
for s in samples:
    print(f"{classify_egress('web_search', {'query': s}).reason_code:24s} {s[:60]}")
