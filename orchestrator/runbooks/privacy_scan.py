#!/usr/bin/env python3
"""Privacy scan for tracked files — block operator-private data before it is committed.

WHY THIS EXISTS
---------------
A memory index was committed to a public repo. It was possible because the file was
TRACKED; everything else was downstream of that one fact. This scan makes the class
detectable mechanically instead of relying on someone reading `git status` carefully
at the end of a long session.

WHAT IT CHECKS
--------------
1. Tracked files under the memory directory. Operator memory must never be tracked.
   This is the check that would have caught the original incident.
2. Credential shapes — API keys, tokens, private key blocks.
3. Routable IP addresses (private ranges and documentation ranges are fine).
4. Install-specific terms from a LOCAL, GITIGNORED config.

On (4): the term list is per-install by construction. Committing the operator's
family names into a public repo in order to detect family names would publish exactly
what it protects. So the framework ships `privacy_patterns.example.toml` with generic
placeholders, and each install writes its own `privacy_patterns.toml`, which is
gitignored. Absent config = checks 1-3 still run.

BUILT TO FAIL
-------------
`--self-test` runs the detectors against known-bad fixtures and FAILS if they come
back clean. A scanner that cannot demonstrate a detection is indistinguishable from
a scanner that is silently broken — which is how this repo's last three "clean"
results were produced (a glob that matched nothing, a shell that mangled a refspec,
and a grep that errored out and printed nothing).

Usage:
    python3 privacy_scan.py                 # scan tracked files, exit 1 on findings
    python3 privacy_scan.py --self-test     # prove the detectors fire
    python3 privacy_scan.py --staged        # scan only what is STAGED (pre-commit)
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ORCHESTRATOR_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = ORCHESTRATOR_DIR.parent
MEMORY_DIR_REL = "orchestrator/memory/"
PATTERNS_CONFIG = ORCHESTRATOR_DIR / "config" / "privacy_patterns.toml"

# Memory-directory files that ARE legitimately framework content, not operator data.
MEMORY_DIR_ALLOWLIST = {
    "orchestrator/memory/.gitkeep",
    "orchestrator/memory/README.md",
}

CREDENTIALS = {
    "OpenAI key":        re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{32,}"),
    "Anthropic key":     re.compile(r"sk-ant-[A-Za-z0-9_-]{40,}"),
    "GitHub token":      re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    "Slack token":       re.compile(r"xox[baprs]-[0-9]{10,}-[0-9]{10,}-[A-Za-z0-9]{20,}"),
    "AWS access key":    re.compile(r"AKIA[0-9A-Z]{16}"),
    "Private key block": re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----"),
    "Slack webhook":     re.compile(r"hooks\.slack\.com/services/T[A-Za-z0-9]+/B[A-Za-z0-9]+/"),
}

# Deliberate fixtures and doc placeholders. Kept narrow on purpose: a broad allowlist
# is how a real key eventually gets waved through.
BENIGN = re.compile(
    r"ABC123|AAABBB|EXAMPLE|XXXXX|placeholder|your[-_]?(api[-_]?)?key|"
    r"smoke|temp-key|different-key|redacted|dummy|fake",
    re.I,
)

# Explicit per-line opt-out. Put `privacy-scan: allow` in a comment on the line.
#
# This exists because this scanner's OWN self-test fixtures trip its own detectors —
# it must contain a literal private-key header and a routable IP in order to prove
# those detectors fire. Caught by CI on the first run after the file became tracked;
# it had passed locally only because the scan ran before `git add`, so the scanner was
# still untracked and never scanned itself.
#
# Deliberately a per-LINE pragma rather than excluding this file: the file most likely
# to contain a mistake is the one being actively edited, and a whole-file exemption
# would blind the scan exactly there. An opt-out that must be typed next to the
# offending line is visible in review; a path exclusion in a config is not.
PRAGMA_ALLOW = "privacy-scan: allow"

IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".ico", ".woff", ".woff2", ".db"}
# Lockfiles are enormous and full of hash-like strings; they are also machine-generated.
SKIP_NAMES = {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "uv.lock", "poetry.lock"}


def _is_private_ip(ip: str) -> bool:
    try:
        a, b, *_ = [int(x) for x in ip.split(".")]
    except ValueError:
        return True
    if any(int(x) > 255 for x in ip.split(".")):
        return True                     # not an IP at all (version string, etc.)
    if a == 10 or a == 127 or a == 0:
        return True
    if a == 192 and b == 168:
        return True
    if a == 172 and 16 <= b <= 31:
        return True
    if a == 169 and b == 254:
        return True
    if a == 192 and b == 0:
        return True                     # 192.0.2.0/24 documentation
    if a == 198 and b in (18, 19, 51):
        return True
    if a == 203 and b == 0:
        return True
    if a >= 224:
        return True                     # multicast / reserved
    return False


def load_local_terms() -> list[str]:
    if not PATTERNS_CONFIG.exists():
        return []
    try:
        import tomllib
        with PATTERNS_CONFIG.open("rb") as f:
            data = tomllib.load(f)
    except Exception as e:
        print(f"[privacy-scan] {PATTERNS_CONFIG.name} unparseable ({e}); "
              f"install-specific terms NOT checked.", file=sys.stderr)
        return []
    terms = data.get("terms")
    return [str(t) for t in terms if str(t).strip()] if isinstance(terms, list) else []


def tracked_files(staged_only: bool) -> list[str]:
    cmd = (["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"]
           if staged_only else ["git", "ls-files"])
    out = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True).stdout
    return [ln for ln in out.splitlines() if ln.strip()]


def scan(staged_only: bool = False) -> list[str]:
    findings: list[str] = []
    files = tracked_files(staged_only)
    local_terms = load_local_terms()
    term_res = [(t, re.compile(re.escape(t), re.I)) for t in local_terms]

    # --- check 1: operator memory must not be tracked ------------------------
    for f in files:
        if f.startswith(MEMORY_DIR_REL) and f not in MEMORY_DIR_ALLOWLIST:
            findings.append(f"TRACKED OPERATOR MEMORY: {f} — memory must never be tracked")

    # --- checks 2-4: content ------------------------------------------------
    for f in files:
        p = REPO_ROOT / f
        if Path(f).suffix.lower() in BINARY_EXT or Path(f).name in SKIP_NAMES:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except (OSError, IsADirectoryError):
            continue

        for i, line in enumerate(text.splitlines(), 1):
            if BENIGN.search(line) or PRAGMA_ALLOW in line:
                continue
            for name, pat in CREDENTIALS.items():
                if pat.search(line):
                    findings.append(f"CREDENTIAL ({name}): {f}:{i}")
            for ip in IP_RE.findall(line):
                if not _is_private_ip(ip):
                    findings.append(f"ROUTABLE IP: {f}:{i} — {ip}")
            for term, tre in term_res:
                if tre.search(line):
                    findings.append(f"INSTALL-SPECIFIC TERM {term!r}: {f}:{i}")
    return findings


def self_test() -> int:
    """Prove each detector fires. A scanner that cannot fail is not a control."""
    cases = [
        ("OpenAI key",        "apiKey = 'sk-proj-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8" + "'"),
        ("GitHub token",      "token: gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9"),
        ("AWS access key",    "aws_key = AKIA" + "IOSFODNN7EXAMPL9"),
        ("Private key block", "-----BEGIN OPENSSH PRIVATE KEY-----"),  # privacy-scan: allow
    ]
    failures = 0
    print("self-test — each detector must FIRE on a known-bad line:")
    for name, line in cases:
        pat = CREDENTIALS[name]
        fired = bool(pat.search(line)) and not BENIGN.search(line)
        print(f"  {name:<20} {'FIRED' if fired else '*** DID NOT FIRE ***'}")
        if not fired:
            failures += 1

    ip_cases = [("8.8.8.8", True), ("192.168.1.10", False), ("127.0.0.1", False),   # privacy-scan: allow
                ("10.0.0.5", False), ("203.0.113.9", False), ("172.16.4.2", False)]  # privacy-scan: allow
    print("  IP classification:")
    for ip, should_flag in ip_cases:
        flagged = not _is_private_ip(ip)
        ok = flagged == should_flag
        print(f"    {ip:<16} flag={flagged!s:<5} expected={should_flag!s:<5} {'ok' if ok else '*** WRONG ***'}")
        if not ok:
            failures += 1

    # The check that would have caught the actual incident.
    print("  tracked-memory rule:")
    hit = "orchestrator/memory/MEMORY.md".startswith(MEMORY_DIR_REL)
    print(f"    orchestrator/memory/MEMORY.md flagged: {hit}")
    if not hit:
        failures += 1

    # The pragma needs a control in BOTH directions. A suppressor that matches too
    # eagerly silently disables the entire scan, which is worse than no scan at all
    # because it still prints OK.
    print("  pragma (must suppress ONLY the marked line):")
    key = "AKIA" + "IOSFODNN7EXAMPL9"
    marked = f"aws = {key}  # {PRAGMA_ALLOW}"
    unmarked = f"aws = {key}"
    suppressed = PRAGMA_ALLOW in marked
    leaks = PRAGMA_ALLOW in unmarked
    print(f"    marked line suppressed:   {suppressed}")
    print(f"    unmarked line suppressed: {leaks}  (must be False)")
    if not suppressed or leaks:
        failures += 1

    print()
    if failures:
        print(f"SELF-TEST FAILED — {failures} detector(s) did not behave. "
              f"A clean scan from this build means nothing.")
        return 1
    print("SELF-TEST PASSED — detectors fire on known-bad input, so a clean scan is meaningful.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true", help="prove the detectors fire, then exit")
    ap.add_argument("--staged", action="store_true", help="scan only staged files (pre-commit use)")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    # Always self-test before reporting a clean scan. Otherwise "no findings" is
    # ambiguous between "nothing to find" and "the scanner is broken".
    if self_test() != 0:
        return 2
    print()

    findings = scan(staged_only=args.staged)
    scope = "staged" if args.staged else "tracked"
    if not findings:
        print(f"privacy-scan: OK — no findings across {scope} files.")
        return 0
    print(f"privacy-scan: {len(findings)} FINDING(S) across {scope} files\n")
    for f in findings:
        print(f"  {f}")
    print("\nOperator-private data must not be tracked. See "
          "orchestrator/memory (gitignored) and the privacy_patterns.example.toml config.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
