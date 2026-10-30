# Verify the installed SDK

`verify.py` runs all four combinations for both `tazo-large` and `tazo-small`:

| Mode | Client | API |
| --- | --- | --- |
| `sync-upload` | `Tazo` | `POST /v1/decisions` |
| `sync-stream` | `Tazo` and a sender thread | `/v1/decisions/stream` |
| `async-upload` | `AsyncTazo` | `POST /v1/decisions` |
| `async-stream` | `AsyncTazo` and concurrent tasks | `/v1/decisions/stream` |

Both clients also check health and model discovery. Cases run sequentially and
submit real inference work. The target's account limits apply. Streaming audio is
paced at recording speed, uses the negotiated message limit, and must produce a
final decision covering the complete recording followed by `session.closed`.
It explicitly requests `audio_scope="turn"` for that check. The SDK default is
`chunk`, whose final decision covers the latest window; `smoke.py` exercises
that default and accepts a `None` turn-duration limit.

## Run against the PyPI package

Use Python 3.11 or newer, a virtual environment with the SDK version you are
verifying installed, and a key created in the [Tazo console](https://app.tazolabs.com).
This checkout prepares `tazo-sdk==0.1.2`; see [RELEASING.md](../RELEASING.md)
for publishing and testing it from TestPyPI and PyPI. Install that exact version
after publication to verify these streaming changes.
Run directly with that environment's Python from the repository root:

```sh
export TAZO_API_KEY='YOUR_TAZO_API_KEY'
python examples/verify.py \
  --audio examples/weather.wav \
  --base-url https://api.tazolabs.com \
  --expected-choice get_weather \
  --output /tmp/tazo-sdk-verification.json
```

The included `weather.wav` is synthesized speech saying "What is the weather
today?", generated with macOS `say` and converted to mono 16 kHz PCM16. Replace
`--audio` with your own recording to test other inputs.

The WAV must be uncompressed mono 16 kHz PCM16, nonempty, within 16 MiB, and
shorter than each model's advertised audio and streaming-turn limits. The script
prints the installed SDK version and import path, each decision with its
probabilities, timings, and a final `Summary: 8/8 passed`. The JSON report contains
decisions and failures, without the API key or audio bytes. A failure produces a
nonzero exit code. No internal Tazo APIs are used.

The default question routes a spoken weather request to `get_weather`, or selects
`NO_TOOL` for other audio. To check the model's answer for a known weather sample,
add `--expected-choice get_weather`. For known unrelated speech, use
`--expected-choice NO_TOOL`. Without this option, checks establish that the API
flow and response contract work; they do not establish that the model understood
the recording correctly. Models may return different probabilities across runs.

Other options:

- `--models tazo-large`: run the four modes for a single model.
- `--timeout 180`: HTTP operation budget and per-event streaming wait, in seconds.
  Async streaming additionally has a deadline for the entire session.

The `upload.py`, `stream.py`, and `smoke.py` examples are also useful for
individual operations and durable idempotency replay checks.

## After verification

Keep the JSON report locally if needed and revoke temporary test keys when
finished. Source code and future releases are maintained in
[tazolabs/tazo-sdk](https://github.com/tazolabs/tazo-sdk). See
[RELEASING.md](../RELEASING.md) for the `sdk-v0.1.2` release steps.
