# context-kit-instrument (addon.context-kit)

ResonantOS local-service add-on wrapping the canonical
[KyaniteLabs/context-kit](https://github.com/KyaniteLabs/context-kit) (MIT) —
the measured context-engineering kit for local LLM agent harnesses.

**This add-on is the instrument; the notes are the companion.** The shipped
docs gift
[simongonzalezdc/resonant-context-notes](https://github.com/simongonzalezdc/resonant-context-notes)
restates the kit's measured results for harness builders with every validity
label intact. The canonical repo — vendored here byte-identically — holds the
code, the experiments, and the rig every number was measured on. Where the two
disagree, the canonical repo wins.

## What it serves (the kit's honest surface)

http-json on `http://127.0.0.1:4901`, loopback only. Every vendored module the
kit actually ships runs for real, in-process:

| Tool | What it runs | Kit module |
|---|---|---|
| `contextkit.status` | Service + kit identity, cache/spill counters, honesty pins | — |
| `contextkit.styles` | The three style prompts (fused/caveman/ponytail) verbatim + the three style laws + regime note | `kit/styles.py` |
| `contextkit.style.route` | The real `style_layer()` router (auto / creative lane / instant / off) | `kit/styles.py` |
| `contextkit.style.prepend` | The real `prepend_stable()` byte-stable style lead | `kit/styles.py` |
| `contextkit.doc.list` / `contextkit.doc.get` | Every vendored kit file verbatim (hash-pinned tree) | whole repo |
| `contextkit.munch.read_symbol` | Real `read_symbol()` — symbol-level reads, sandboxed to `var/samples/`, accelerated by the kit's own mtime-validated `ResultCache` | `kit/munch.py` |
| `contextkit.diet.project` | Real `project()` — budget elision, identical-output dedup, long-session compaction; input never mutated | `kit/diet.py` |
| `contextkit.diet.disclose` | Real `disclose()` — head+tail preview, content-addressed spill confined to `var/spill/` | `kit/diet.py` |

### Honest surface: what is pinned NOT runnable (and never faked)

The kit's measurement instruments — `kit/instruments/tpt_battery.py` and
`tpt_style.py` — measure seconds/tokens-per-correct-task against a **live
llama.cpp-class OpenAI-compatible endpoint**. This service requests zero
network capability and never dials out (not even loopback-to-loopback), so
they cannot run here. `contextkit.status` reports
`headless_battery_execution: false` and `headless_style_ab_execution: false`
with the honesty note, exactly as the kit's own validity-label law demands:
a capability the service does not have is claimed nowhere, faked nowhere.
Run the instruments from the vendored tree against your own rig — n>=3 per
arm in one thermal window.

### Sandbox and redaction laws

- **munch sandbox**: `read_symbol` paths resolve strictly inside this add-on's
  `var/samples/` (synthetic fixture modules). Absolute paths, `~`, and any
  realpath escape are refused. Cache laws kept: read-only tools only, the full
  cached bytes flow back, mtime re-validated on every hit.
- **spill confinement**: `disclose` spills only into `var/spill/`
  (content-addressed, sha1[:12]).
- **redaction**: client content strings are home-path-redacted on ENTRY
  (`<home>/you` -> `~/you`), so the kit functions compute on redacted bytes and
  spill files on disk carry no home path; every response is redacted again on
  the way out (covers service-generated paths like spill locations).
- **control characters**: identifier-ish params (`path`, `name`, `label`,
  enums) reject every C0 control and DEL. Content params (`text`,
  `system_text`, event `content`) accept tab/newline/CR — real prompts and
  tool output carry them — and reject every other control character.
- **transport**: body <= 64KB (else 413 + close), lying/dead request body
  times out at 30s (408 + close), chunked encoding 400, unknown tool 400,
  per-method param allowlists (never a union), bind conflict exits 78.
- **no subprocess, no outbound network**: the vendored kit is imported, not
  executed; the service requests zero capabilities.

## Vendor provenance

Vendored via `git archive HEAD` of
[KyaniteLabs/context-kit](https://github.com/KyaniteLabs/context-kit) —
committed tree only, upstream working tree verified clean, never a dirty
checkout. Commit: `5456928ac1dcf1ca21f7ace78a4b1204d2871004` (2026-08-18,
"champion row: q4_0 KV flip + instrument-labeled prose speeds").
Every vendored file is sha256-pinned in
`vendor/VENDOR-MANIFEST.json`; the test suite re-derives a fresh `git archive`
and asserts byte-identity. The kit's LICENSE is copied verbatim to this
add-on's `LICENSE` (MIT, Copyright (c) 2026 Kyanite Labs (Simon Gonzalez de
Cruz)); upstream notices ride along under `vendor/THIRD-PARTY-NOTICES.md`.

## Layout

```
addon.json               ResonantOS manifest (id addon.context-kit, port 4901)
server.py                stdlib-only http-json service (Python; matches the kit)
LICENSE                  kit's MIT, verbatim
README.md                this file
run-validator-check.sh   validates addon.json against the real ResonantOS validator
tests/test_addon.py      wrapper suite (vendor pin, determinism, adversarial HTTP, privacy)
vendor/                  frozen kit tree + VENDOR-MANIFEST.json (sha256 pins)
var/samples/             synthetic sandbox fixtures for munch reads
var/spill/               runtime spill directory (content-addressed)
```

## Run

```
python3 server.py                 # binds 127.0.0.1:4901 (CONTEXTKIT_PORT to override in dev)
curl -s http://127.0.0.1:4901/health
```

Example: route a turn, then apply the style byte-stably.

```
curl -s http://127.0.0.1:4901/ -d '{"method":"contextkit.style.route","params":{"text":"fix the parser"}}'
curl -s http://127.0.0.1:4901/ -d '{"method":"contextkit.style.prepend","params":{"system_text":"You are an agent.","style":"fused"}}'
```

## Verify

```
sh run-validator-check.sh <path-to-2.0.0-alpha-clone>   # 0 errors, 0 warnings
python3 -m unittest discover -s tests -v                # from this directory
```

The suite covers: vendor hash-pin + git-archive byte-identity, vendored-module
determinism (prepend byte-stability, projection idempotence, cache
invalidation), every honesty pin, strict per-method params, the adversarial
HTTP matrix (413/408/chunked/control-chars/unknown-tool/flood), sandbox
containment, spill confinement, entry+response+disk redaction, whole-tree
privacy scan, and port uniqueness against all sibling add-on manifests.

Zero capabilities requested. Nothing leaves 127.0.0.1.
