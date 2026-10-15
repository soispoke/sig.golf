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

## See which edits invalidate expensive modules

Before rebuilding a candidate, compare its current source with a Git commit:

```sh
python3 tools/lean-fast/impact.py --repo /path/to/candidate --base HEAD \
  --watch SigGolfCandidate.ClaudeWCT.Numerics.N600Price \
  --watch SigGolfCandidate.ClaudeWCT.Numerics.N600PriceChecks
```

Use module names that exist in that candidate. The JSON report lists potentially
invalidated modules reachable from `Solution` and an import path explaining each affected
watched module. It includes uncommitted and untracked candidate files, removed imports,
and project-pin changes. `--target Module` selects a narrower build target.

This is a source dependency estimate, not a build or a cache receipt: Lake still decides
actual cache validity. Separating an expensive numeric proof from changing policy modules
can preserve its cached result. Use this report to check that separation before and after
a refactor. The tool does not rewrite imports or change the candidate.

## Profile the separate kernel replay

A fast `lake build` does not guarantee fast certificate verification. The comparator also
exports declarations and rechecks them in a fresh kernel environment. To find expensive
proofs in an existing export:

```sh
python3 tools/lean-fast/profile_replay.py --init
python3 tools/lean-fast/profile_replay.py /path/to/export.ndjson --timeout 3600
```

Initialization builds a separate pinned Lean 4.33.1 project under `.work/lean-fast-replay`.
It fetches the pinned `lean4export` dependency and tests that malformed constructors,
recursors and proof terms are rejected. The wrapper uses the same memory guard as the
incremental runner, one Lean runtime worker, and a workspace lock. A source or profiler
binary change requires initialization again. Use `--workspace` and `--max-memory-gb` to
choose another location or lower memory ceiling.

Each run writes `replay.log`, sorted `timings.tsv`, streamed `timings.tsv.partial.tsv`, and
`result.json` in a timestamped `runs/` directory. Partial timings contain only completed
declarations; a killed or timed-out declaration will not have a timing. The final report
includes the export hash, elapsed time, summed kernel time and the slowest declarations.
A failed replay or changed export cannot produce a completed report.

The profiler is diagnostic. It preserves Lean's postponed constructor and recursor
consistency checks, but it does not compare the challenge theorem types, enforce the
competition's allowed-axiom policy, execute program images, or issue a score. Its reports
always have `official_verified: false`. Exports must come from the exact candidate being
investigated; do not use reduced or `sorry`-filled substitutes as verification evidence.

## Optional list reduction helpers

[`examples/FastLists.lean`](examples/FastLists.lean) provides direct-recursion versions of
list drop, append, flatten and optional indexing. General theorems prove equality to the
standard operations, with additional boundary examples and axiom checks. These helpers
were extracted from the existing `44f3494` candidate optimization, but their effect depends
on the proof that uses them. They are examples to copy into `submission/SigGolfCandidate/`
and measure, not automatic rewrites or a measured speedup for every candidate.

Keep program images and certificate statements unchanged when changing proof machinery.
Check both elaboration and replay: an edit can improve one and make the other slower.
Do not bypass Lake invalidation with hand-edited traces or unverified `.olean` overlays.

## Evidence and limits

During development, a warm small-edit check rebuilt three modules and completed its
certificate audit in about 17.15 seconds. That was one existing candidate and a warm
workspace, not a clean build or an official accepting run. This package does not claim
a measured tenfold improvement for a complete candidate or a submission-time guarantee.

Run the lightweight regression tests and Lean helper examples with:

```sh
python3 -B -m unittest discover -s tools/lean-fast -p 'test_*.py'
lean tools/lean-fast/examples/BitVecLists.lean
lean tools/lean-fast/examples/FastLists.lean
```

Tests cover source synchronization, receipt lifecycle, policy cache invalidation,
certificate audit handling, memory limits, owned-process cleanup, dependency invalidation
and failed replay reports. The receipt tests use a simulated compiler and do not establish any candidate's proof validity.

A small Linux integration test builds the pinned profiler, compiles both helper examples,
exports the four general list equalities, and replays them. It requires network access
and the pinned toolchain, uses a temporary workspace, and does not build the candidate:

```sh
python3 tools/lean-fast/integration.py --max-memory-gb 6
```

This test replayed 489 declarations successfully during development. That demonstrates
the tooling path on a small fixture, not a full candidate performance improvement.

Before submission, run the unchanged official checks on a supported Linux host:

```sh
bash scripts/setup.sh
python3 scripts/run.py
```

Competition submissions still edit only `submission/`. Use Yukon for submission and
promotion. Fork tools and local receipts are not part of a candidate's score.
