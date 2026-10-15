"""Audit a locally built Solution against its claim; this is not official verification."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from resources import run_guarded


CHALLENGE = "SigGolf.Challenge."
THEOREMS = [CHALLENGE + name for name in (
    "signature_bytes", "witness_bytes", "cache_bytes", "layout_offsets", "certificate"
)]
DEFINITIONS = [CHALLENGE + "submission"]
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*")


def render_audit(claim: dict, comparator: dict, marker: str) -> str:
    """Generate exact type checks and an axiom audit without replaying cached proofs."""
    if comparator.get("theorem_names") != THEOREMS or comparator.get("definition_names") != DEFINITIONS:
        raise ValueError("Unsupported comparator targets")
    allowed = comparator.get("permitted_axioms")
    if not isinstance(allowed, list) or any(not isinstance(n, str) or not NAME.fullmatch(n) for n in allowed):
        raise ValueError("Invalid comparator axiom names")
    numbers = [claim[k] for k in ("S", "W", "K", "C")]
    layout_keys = ("message", "secret_key", "public_key", "cache", "signature", "witness")
    numbers += [claim["layout"][k] for k in layout_keys]
    if any(type(n) is not int or n < 0 for n in numbers):
        raise ValueError("Claim values must be nonnegative integers")
    if not re.fullmatch(r"[A-Za-z0-9_]+", marker):
        raise ValueError("Invalid audit marker")
    s, w, k, c = numbers[:4]
    message, secret_key, public_key, cache, signature, witness = numbers[4:]
    statements = [
        f"SigGolf.Challenge.submission.sizes.signature = {s}",
        f"SigGolf.Challenge.submission.sizes.witness = {w}",
        f"SigGolf.Challenge.submission.sizes.cache = {k}",
        "SigGolf.Challenge.submission.layout = "
        f"{{ message := {message}, secretKey := {secret_key}, publicKey := {public_key}, "
        f"cache := {cache}, signature := {signature}, witness := {witness} }}",
        f"SigGolf.Certificate SigGolf.Challenge.submission {c}",
    ]
    lines = ["import Solution", "import Lean", "", "namespace LocalCertificateAudit", ""]
    for target, statement in zip(THEOREMS, statements):
        leaf = target.rsplit(".", 1)[1]
        lines += [f"theorem expected_{leaf} : {statement} :=", f"  {target}", ""]
    lines += ["end LocalCertificateAudit", "", "open Lean Elab Command", "", "run_cmd do", "  let env ← getEnv"]
    for target in THEOREMS:
        leaf = target.rsplit(".", 1)[1]
        lines += [
            f"  let some (.thmInfo actual) := env.find? `{target}",
            f"    | throwError \"Target is not a theorem: {target}\"",
            f"  let some (.thmInfo expected) := env.find? `LocalCertificateAudit.expected_{leaf}",
            "    | throwError \"Missing expected theorem\"",
            "  unless actual.type == expected.type && actual.levelParams == expected.levelParams do",
            f"    throwError \"Claim type differs from target: {target}\"",
        ]
    lines += [
        "  let some (.defnInfo submission) := env.find? `SigGolf.Challenge.submission",
        "    | throwError \"Submission is not a definition\"",
        "  unless submission.type == mkConst `SigGolf.Submission && submission.levelParams.isEmpty do",
        "    throwError \"Submission has an unexpected type\"",
        "  unless submission.safety == .safe do",
        "    throwError \"Submission is not a safe definition\"",
        "  let permitted : Array Name := #[" + ", ".join("`" + n for n in allowed) + "]",
        "  let targets : Array Name := #[" + ", ".join("`" + n for n in THEOREMS + DEFINITIONS) + "]",
        "  for target in targets do",
        "    for axiomName in ← collectAxioms target do",
        "      unless permitted.contains axiomName do",
        '        throwError "Disallowed axiom {axiomName} in {target}"',
        f'  logInfo "{marker}"',
        "",
    ]
    return "\n".join(lines)


def audit(project: Path, claim: dict, comparator: dict, log_path: Path, env=None,
          max_memory_gb=98.0) -> dict:
    """Run after a successful dependency-aware Lake build of Solution in project."""
    project, log_path = Path(project).resolve(), Path(log_path).resolve()
    env = dict(os.environ if env is None else env)
    for name in ("LEAN_PATH", "LEAN_SRC_PATH", "LEAN_SYSROOT", "ELAN_TOOLCHAIN",
                 "LEAN_GITHASH", "LAKE_OVERRIDE_LEAN", "LAKE_HOME"):
        env.pop(name, None)
    marker = "LOCAL_CERTIFICATE_AUDIT_OK_" + secrets.token_hex(12)
    source = project / "LocalCertificateAudit.lean"
    source.write_text(render_audit(claim, comparator, marker), encoding="utf-8")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    timed_out = False
    with log_path.open("w", encoding="utf-8") as log:
        try:
            exit_code = run_guarded(
                ["lake", "env", "lean", "--json", "-DautoImplicit=false", source.name],
                cwd=project, env=env, log=log, max_memory_gb=max_memory_gb, timeout=120,
            )
        except subprocess.TimeoutExpired:
            timed_out, exit_code = True, None
            log.write("\nLocal certificate audit timed out after 120 seconds.\n")
    records = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    success_markers = [r for r in records if isinstance(r, dict)
                       and r.get("severity") == "information" and r.get("data") == marker]
    ok = exit_code == 0 and len(success_markers) == 1
    return {
        "ok": ok, "status": "passed" if ok else "failed", "mode": "local_incremental",
        "official_verified": False, "exit_code": exit_code, "timed_out": timed_out,
        "elapsed_seconds": round(time.monotonic() - started, 3), "log": str(log_path),
        "theorems": THEOREMS, "definitions": DEFINITIONS,
        "permitted_axioms": comparator["permitted_axioms"],
    }
