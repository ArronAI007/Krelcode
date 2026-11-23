# Prompt Parity Baselines

This directory stores accepted, content-light live-model baselines for the frozen
Prompt Parity contract.

A baseline is content-light and contains only:

- contract version/hash and suite hash;
- provider/model identity;
- Krelcode version and frozen runner/provider fingerprints;
- per-case trial/pass/pass-rate/stability counts.

It must never contain Prompt bodies, final answers, tool argument bodies, tool
output, workspace files, histories, credentials or API keys.

## DeepSeek freeze

Use the checked-in DeepSeek profile so provider, model, endpoint fingerprint,
sampling parameters and timeout are reproducible:

```bash
export DEEPSEEK_API_KEY=sk-...

PYTHONPATH=src python -m krelcode.benchmarks.prompt \
  --live \
  --config benchmarks/deepseek-live.toml \
  --trials 3 \
  --sandbox-backend static \
  --save-baseline benchmarks/baselines/deepseek-flash.json \
  --save-report .krelcode/prompts/benchmarks/deepseek-freeze.json \
  --json
```

The baseline is written only when the complete 16-case × 3-trial suite passes.
Existing baselines are protected from accidental replacement; use
`--force-baseline` only after reviewing a deliberate calibration change.

The config reads the key from `DEEPSEEK_API_KEY`. Reports persist only a hash of
the base URL and generation settings, never the endpoint, key or Prompt body.

## Nightly regression

`.github/workflows/nightly-deepseek-live.yml` runs daily at 02:00 Asia/Shanghai
and can also be dispatched manually in `regression` or `freeze` mode.

Configure the repository secret:

```text
DEEPSEEK_API_KEY=...
```

Regression mode runs the full suite and fails closed against the committed
baseline. Freeze mode uploads a candidate baseline artifact for human review; it
does not commit or replace accepted evidence automatically.

Run the same gate locally:

```bash
PYTHONPATH=src python -m krelcode.benchmarks.prompt \
  --live \
  --config benchmarks/deepseek-live.toml \
  --trials 3 \
  --sandbox-backend static \
  --gate benchmarks/baselines/deepseek-flash.json \
  --save-report .krelcode/prompts/benchmarks/latest.json \
  --tune-output .krelcode/prompts/benchmarks/tuning.json
```

Render a GitHub-friendly content-light summary from a saved CLI result:

```bash
python -m krelcode.benchmarks.nightly --summary .nightly/result.json
```

The default destination is `.krelcode/prompts/benchmarks/latest.json`. Report
files never contain answers or tool-argument bodies, even when
`--include-content` is used for the debug-only console output.


`--gate` fails closed when the contract, provider/model, Prompt release/profile,
suite hash, runner/provider fingerprint, case manifest, minimum trial count,
pass@k, stability or runtime error requirements do not match the accepted
baseline. The workflow uploads the report, tuning candidates and candidate
baseline (freeze mode) for 30 days.
