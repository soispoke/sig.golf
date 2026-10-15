# Incremental Lean checks

This fork provides a reusable development runner for sig.golf candidates. It keeps a
separate Lake project, copies only changed source files, and lets Lake invalidate the
normal dependency graph. Repeated edits can reuse completed proofs instead of starting
every certificate build from scratch. The source checkout is never modified.

A successful full local check also compares the exact certificate theorem types against
`claim.json`, checks that the submission definition is safe, and checks permitted axioms.
It writes a receipt bound to the source snapshot, runner, toolchain and `Solution.olean`.
The receipt always says `official_verified: false`: this runner does not evaluate the
program images, measure RISC-V cycles, reproduce the verifier's isolation, or issue a score.

## First run

Use Python 3.10 or newer and elan with the repository's pinned Lean toolchain on macOS
or Linux. From this fork:

```sh
python3 tools/lean-fast/check.py --init
```

This creates `.work/lean-fast/project`, copies the pinned project and candidate, runs
`lake exe cache get` to obtain dependency caches, then builds and audits the certificate.
Dependency acquisition requires network access. The first build may still be expensive.
Later checks reuse the same project:

```sh
python3 tools/lean-fast/check.py
```

To check another candidate checkout using the runner from this fork:

```sh
python3 /path/to/sig.golf-fork/tools/lean-fast/check.py --repo /path/to/candidate --init
```

Repeat without `--init` after editing the candidate. By default, each checkout gets its
own `.work/lean-fast` directory. Use `--workspace /path/to/separate/state` to choose a
persistent location. One runner at a time may use a workspace; keep separate workspaces
for simultaneous candidates. Use caches you built yourself or obtained from the pinned
project's cache service. Do not hand-edit Lake trace hashes or copy arbitrary `.olean`
files into the workspace: local incremental checks trust their cache.

## Smaller checks while developing

```sh
python3 tools/lean-fast/check.py --target SigGolfCandidate.MyProof
python3 tools/lean-fast/check.py --target SigGolfCandidate.A --target SigGolfCandidate.B
python3 tools/lean-fast/check.py --target SigGolfCandidate.MyProof --dependencies-only
```

Focused checks build only the selected modules and their dependencies. They do not issue
a full certificate receipt, and clear any previous current receipt. Finish with a default
full check before treating the complete certificate as locally checked.

Logs are under `.work/lean-fast/logs/`. The current receipt is
`.work/lean-fast/receipt.json`; timestamped receipts remain beside their logs. Failed builds,
failed audits and source changes during a full check prevent a new receipt. A receipt
records a particular snapshot, not a promise about later edits.

## Memory limits

The default is `LEAN_NUM_THREADS=2`, configurable with `--threads`. This is Lean's runtime
worker setting; it is not an OS-enforced limit on all concurrent processes. The runner
checks memory before starting each command and polls during commands, including when
they produce no output. It stops only the process group it started.

The stop threshold is the lower of 85% of physical RAM and `--max-memory-gb` (default
98 GB, maximum 100 GB). On Linux, every visible finite cgroup v2 memory limit also
constrains the run. Starting a command requires a further 10% margin below each threshold.
For example, an 8 GB host stops at 6.8 GB used and requires usage below 6.12 GB to start.
The limit measures the whole host or cgroup, so other jobs can cause a refusal or stop.

Unreadable memory counters, unsupported platforms and legacy v1 memory cgroups fail
closed. This polling guard reduces pressure; it cannot guarantee prevention of every
sudden allocation spike or OOM. A job whose peak working set exceeds available RAM needs
a smaller proof/build or a larger host. Lower thread counts may help, but do not make
an arbitrarily large proof fit. After pressure subsides, rerun to reuse completed work.

## Optional proof elaboration helper

[`examples/BitVecLists.lean`](examples/BitVecLists.lean) demonstrates `bv_list%`, a term
elaborator for large constant `BitVec` lists. It produces ordinary `List.cons` and
`BitVec.ofNat` expressions instead of resolving numeral typeclasses for every element.
The example includes kernel-checked equalities for empty lists and width truncation.

To adopt it, copy the helper into an allowed `submission/SigGolfCandidate/` module,
import that module, and replace a large literal list with, for example,
`bv_list% 32 [1, 2, 3]`. Preserve the original widths, values, image bytes and proof
obligations. Check the affected modules, then the full certificate and official verifier.
The helper is an example, not an automatic rewrite of every candidate.

For large proofs, reuse established lemmas and split repeated image lookup proofs into
small shared modules so unrelated changes do not invalidate every proof. Avoid replacing
kernel proofs with unchecked evaluation or broadening permitted axioms.

## Evidence and limits

During development, a warm small-edit check rebuilt three modules and completed its
certificate audit in about 17.15 seconds. That was one existing candidate and a warm
workspace, not a clean build or an official accepting run. This package does not claim
a measured tenfold improvement for a complete candidate or a submission-time guarantee.

Run the lightweight regression tests and Lean helper examples with:

```sh
python3 -B -m unittest discover -s tools/lean-fast -p test_runner.py
lean tools/lean-fast/examples/BitVecLists.lean
```

Tests cover source synchronization, receipt lifecycle, policy cache invalidation,
certificate audit handling, memory limits and owned-process cleanup. The receipt tests
use a simulated compiler and do not establish any candidate's proof validity.

Before submission, run the unchanged official checks on a supported Linux host:

```sh
bash scripts/setup.sh
python3 scripts/run.py
```

Competition submissions still edit only `submission/`. Use Yukon for submission and
promotion. Fork tools and local receipts are not part of a candidate's score.
