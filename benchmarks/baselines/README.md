# Prompt Parity Baselines

This directory stores accepted, content-light live-model baselines for the frozen
Prompt Parity contract.

A baseline contains only:

- contract version/hash and suite hash;
- provider/model identity;
- Krelcode version and runner settings;
- per-case trial/pass/pass-rate/stability counts.

It must never contain Prompt bodies, final answers, tool argument bodies, tool
output, workspace files, histories, credentials or API keys.

Create a baseline only after the complete 16-case suite passes with at least
three trials per case:

```bash
PYTHONPATH=src python -m krelcode.benchmarks.prompt \
  --live --provider openai --model YOUR_DEEPSEEK_MODEL \
  --save-baseline benchmarks/baselines/deepseek-chat.json
```

Run the release gate:

```bash
PYTHONPATH=src python -m krelcode.benchmarks.prompt \
  --live --provider openai --model YOUR_DEEPSEEK_MODEL \
  --gate benchmarks/baselines/deepseek-chat.json
```


`--gate` fails closed when the contract, provider/model, suite hash, case
manifest, minimum trial count, pass@k, stability or runtime error requirements
do not match the accepted baseline.
