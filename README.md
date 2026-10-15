# sig.golf on Yukon

Optimize a stateless hash-based signature scheme under the [beta rules](RULES.md).
The single `full` track minimizes **signature bytes × RISC-V verification cycles**.
Yukon promotions are manual.

## Contract

The contract lives in this repository: `RULES.md` states the rules, `SigGolf/` holds the
Lean statements a certificate proves, `verifier/` checks a submission, and `lakefile.lean`,
`lake-manifest.json`, and `lean-toolchain` pin the Lean project. The contract commit is the
repository commit. The bot under `service/` and the website under `site/` are the upstream
sig.golf deployment and are not used by Yukon.

The proposed baseline is the **6,404 bytes × 11,573 cycles = 74,113,492** SPHINCS+
variant from [upstream PR #34](https://github.com/leanEthereum/sig.golf-submissions/pull/34),
using its [PR #35 cache-size port](https://github.com/leanEthereum/sig.golf-submissions/pull/35)
with a 131,072-byte cache. `BASELINE.json` records the exact source revision. Upstream
accepted it under an earlier contract; this repository's vendored contract requires
fresh verification.

## Develop and submit

Only `submission/` is editable: `claim.json`, `Solution.lean` and the
`SigGolfCandidate/` Lean modules. The verifier checks source policy, claim matching,
permitted axioms, and the certificate, then evaluates the four images and records their
digests. Read `RULES.md` for the exact model and proof obligations and `site/llms.txt` for
a guide to writing a submission.

From the repository root on Linux:

```sh
bash scripts/setup.sh
python3 scripts/run.py
```

Setup installs elan through a pinned installer, selects the contract's Lean toolchain,
builds the verifier tools and trusted `SigGolf` library, and checks Linux prerequisites.
Go 1.24+, Landlock and user systemd with the verifier's required namespace and resource
isolation must be available. The verifier checks isolation before compiling a candidate.
Unsupported hosts fail; there is no unsandboxed scoring fallback.

After Yukon import, use `yukon setup --track full`, `yukon run --track full` and
`yukon submit --track full`. `yukon switch full` changes the selected track without
changing Git branches or files. CI evaluates the dispatched checkout; local runs
include working-tree edits. Failed verification emits no score. The wrapper preserves
exact integer scores and rejects any value beyond Yukon's safe integer range.

## Local iteration tools in this fork

[The incremental Lean runner](tools/lean-fast/README.md) keeps a separate persistent build
workspace for faster repeated proof checks across candidates. It includes memory guards,
a source-bound local certificate audit, and an optional large-literal elaboration helper.
Start with `python3 tools/lean-fast/check.py --init`. Official Linux verification and
Yukon submission remain the final checks described above.

## Workflow and caches

The manual `benchmark.yml` workflow uses `blacksmith-16vcpu-ubuntu-2404` (64 GB RAM).
Because the Blacksmith host lacks Landlock, `scripts/blacksmith.sh` boots a checksum-pinned
Ubuntu 26.04 KVM guest with 12 vCPUs and 32 GB RAM. The verifier runs as an unprivileged
user under `/srv`, with Landlock and the upstream systemd isolation checks intact.
The job allows five hours around the verifier's four-hour, 24 GiB limit. The VM is stopped
on success or failure, and verifier logs and the guest console are retained as artifacts.

The guest's elan/toolchains, the complete `.lake` dependency/build workspace and
verifier tools are cached together by contract commit and bootstrap-script hashes.
Only the default branch saves shared caches, before candidate verification. Candidate
outputs and scores are never cached. Restored tools still run the upstream setup checks.

Yukon owns submission PRs and promotions. Do not enable the original upstream record
publisher on this repository. `records.json` is not updated by Yukon.

## License

See `LICENSE` and `THIRD_PARTY_NOTICES.md`.
