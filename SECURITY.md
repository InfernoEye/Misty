# Security Policy

## Supported versions

Misty is a personal, private tool with no published release. The single
maintainer ([ForgottenSin](https://github.com/InfernoEye/Misty)) handles all
security work; there is no rolling version line. Report any issue against the
current code.

## Reporting a vulnerability

Because this is a private repository, the maintainer is the only person who
receives reports, and reports stay visible to no one else.

1. Do **not** open a public issue or a public pull request — that could expose
   the problem before it is fixed.
2. Email the maintainer at
   [dominikn180@gmail.com](mailto:dominikn180@gmail.com) with:
   - What the vulnerability is and which component it touches.
   - Clear reproduction steps.
   - Expected severity and any impact you foresee.
   - Your contact preference for follow-up.
3. If you would prefer a private channel, say so and the maintainer will
   arrange one.

If the repository has GitHub Security Advisories available for it, you can
instead file a report through **Settings → Security → Security advisories**,
which keeps the report private by default.

## What to report

- Remote code execution or injection via input Misty processes
  (voice transcription, captured images, URLs, window text, OCR tokens).
- Auth or credential handling, and any secret or private data stored on disk.
- Improper enforcement of approval, cancellation, or privacy boundaries.
- Unsafe loading of the local models or of the pinned submodules/binaries.

## What not to report

- Feature requests or non-security bugs — open an issue instead.
- Known, accepted limitations already documented in the README or docs.

## What to expect

- A confirmation of receipt, ideally within a short period.
- A triage: what the issue is, its severity, and whether it affects this
  codebase.
- If valid, a fix, or a clear explanation of why it is accepted behavior.

## Scope and limits

There is no bug bounty and no responsible-disclosure reward. Reports are
handled in good faith; please do not perform destructive testing.
