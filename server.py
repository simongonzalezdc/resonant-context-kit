#!/usr/bin/env python3
"""addon.context-kit local-service entry (http-json on 127.0.0.1:4901).

ResonantOS add-on contract: protocol http-json, healthCommand contextkit.status.
Wraps the FROZEN vendored context-kit (KyaniteLabs, MIT) in-process: the
vendored modules are imported directly, no subprocess, no secrets, no outbound
network. The only disk writes are the kit's own content-addressed spill files,
confined to this add-on's var/spill/ directory.

Honest surface (what the kit ACTUALLY does, and what is runnable headless):
  - kit.styles  — style prompts + style_layer() router + prepend_stable():
    pure functions; served for real (contextkit.styles, style.route, style.prepend).
  - kit.munch   — read_symbol() over a SANDBOX of synthetic sample files
    (var/samples/), accelerated by the kit's own mtime-validated ResultCache;
    served for real (contextkit.munch.read_symbol).
  - kit.diet    — projection-time dedup/compaction (pure) and progressive
    disclosure (spills to var/spill/); served for real (diet.project, diet.disclose).
  - kit.instruments/tpt_battery.py + tpt_style.py — need a LIVE llama.cpp-class
    OpenAI-compatible endpoint to measure against. This service requests zero
    network capability and never dials out, so they are PINNED not-runnable
    headless (headless_battery_execution / headless_style_ab_execution: false)
    and never faked. Run them from the vendored tree against your own rig.

Redaction law: client-provided content strings are redacted (home path -> "~")
on ENTRY, so every byte the kit functions compute on is already redacted; every
response is redacted again on the way out (covers service-generated paths such
as spill locations). No absolute home paths leave or land in this service.

Exit codes: 0 normal stop; 78 port bind failure.
"""

import hashlib
import json
import os
import socket
import sys
import threading

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("CONTEXTKIT_PORT", "4901"))  # dev override; manifest port 4901 is the contract
MAX_BODY = 64 * 1024
MAX_STR = 2048            # identifier-ish string params
MAX_TEXT = 60000          # content strings (must fit a 64KB body with envelope room)
MAX_EVENTS = 5000

ADDON_ROOT = os.path.dirname(os.path.abspath(__file__))
VENDOR_ROOT = os.path.join(ADDON_ROOT, "vendor")
SAMPLES_ROOT = os.path.join(ADDON_ROOT, "var", "samples")
SPILL_DIR = os.path.join(ADDON_ROOT, "var", "spill")

sys.dont_write_bytecode = True  # keep the vendored tree byte-frozen: no __pycache__ under vendor/
sys.path.insert(0, VENDOR_ROOT)

from kit import __version__ as KIT_VERSION  # noqa: E402
from kit import diet as kit_diet            # noqa: E402
from kit import munch as kit_munch          # noqa: E402
from kit import styles as kit_styles        # noqa: E402

with open(os.path.join(VENDOR_ROOT, "VENDOR-MANIFEST.json")) as _f:
    _VENDOR_META = json.load(_f)

STYLE_KEYS = tuple(sorted(kit_styles.STYLES))
DIALS = ("auto",) + STYLE_KEYS + ("off",)
THINK_MODES = ("think", "instant")

# Content strings legitimately carry tabs/newlines (system prompts, tool
# output). Identifier-ish strings never do. Both classes reject every other
# C0 control and DEL.
_CONTENT_OK = (0x09, 0x0A, 0x0D)


def _has_bad_control(text, allow_content_ws=False):
    for ch in text:
        o = ord(ch)
        if o == 0x7F or (o < 0x20 and not (allow_content_ws and o in _CONTENT_OK)):
            return True
    return False


# ------------------------------------------------ service-side kit telemetry
_counters_lock = threading.Lock()
_counters = {"cache_hits": 0, "cache_misses": 0, "spills": 0}


def _bump(key):
    with _counters_lock:
        _counters[key] += 1


def _stat_hook(key, hit):
    _bump("cache_hits" if hit else "cache_misses")


# The kit's own read cache accelerates sandbox reads (mtime-validated; the
# full cached bytes always flow back — the kit's three cache laws, kept).
_read_cache = kit_munch.ResultCache(cap=50, ttl_s=300)
_read_cache.stat_hook = _stat_hook

_spill_stats = kit_diet.SpillStats()
# Readbacks happen when the CLIENT re-reads the spilled file; this service
# never sees that, so readbacks stay client-side by design (PATTERNS.md #6):
# only note_spill is observable here.


# ---------------------------------------------------------------- redaction
def _redact_text(text):
    home = os.path.expanduser("~")
    return text.replace(home, "~") if home and home != "~" else text


def _redact_obj(obj):
    if isinstance(obj, str):
        return _redact_text(obj)
    if isinstance(obj, list):
        return [_redact_obj(item) for item in obj]
    if isinstance(obj, dict):
        return {key: _redact_obj(value) for key, value in obj.items()}
    return obj


# ------------------------------------------------------------- kit bindings
def _kit_style_route(text, think_mode, dial):
    """Return ({effective_style|None}, creative_lane) via the vendored router."""
    creative = bool(kit_styles.CREATIVE.search(text))
    return kit_styles.style_layer(text, think_mode, dial=dial), creative


def _sandbox_resolve(rel_path):
    """Resolve a client path strictly inside var/samples/. Returns an absolute
    path or None. Absolute paths and any realpath escape are refused."""
    if os.path.isabs(rel_path) or rel_path.startswith("~"):
        return None
    candidate = os.path.realpath(os.path.join(SAMPLES_ROOT, rel_path))
    root = os.path.realpath(SAMPLES_ROOT)
    if candidate != root and not candidate.startswith(root + os.sep):
        return None
    return candidate


def _kit_read_symbol(rel_path, name, want_body):
    """read_symbol over the sandbox, accelerated by the vendored ResultCache.
    Mirrors kit.munch.cached_read's law: cache successful reads only; the kit's
    'ERROR: ...' strings (miss, candidates, non-.py) are never cached."""
    abs_path = _sandbox_resolve(rel_path)
    if abs_path is None or not os.path.isfile(abs_path):
        return None, None  # unknown sandbox file
    key = f"read_symbol|{abs_path}|{name}|{bool(want_body)}"
    hit = _read_cache.lookup(key, path=abs_path)
    if hit is not None:
        return hit, True
    try:
        result = kit_munch.read_symbol(abs_path, name, want_body=want_body)
    except (OSError, SyntaxError, ValueError):
        return None, None  # unreadable or unparseable: unknown to the surface
    if not result.startswith("ERROR"):
        try:
            _read_cache.store(key, result, path=abs_path)
        except (OSError, ValueError):
            pass  # the cache must never break a tool call (kit law)
    return result, False


def _kit_project(events, budget_chars, min_body):
    """The vendored projection pipeline, verbatim kit.diet.project semantics."""
    projected = kit_diet.project([dict(e) for e in events],
                                 budget_chars=budget_chars, min_body=min_body)

    def _chars(seq):
        return sum(len(e.get("content", "")) for e in seq)

    chars_in, chars_out = _chars(events), _chars(projected)
    return {
        "projected": projected,
        "events_in": len(events),
        "events_out": len(projected),
        "chars_in": chars_in,
        "chars_out": chars_out,
        "chars_saved": chars_in - chars_out,
    }


def _spill_path_for(text):
    """Same content address the vendored disclose() uses (sha1[:12])."""
    h = hashlib.sha1(text.encode(errors="replace")).hexdigest()[:12]
    return os.path.join(SPILL_DIR, f"{h}.txt")


def _kit_disclose(text, limit, label):
    """Progressive disclosure with the spill confined to var/spill/ and the
    kit's own SpillStats counting spills."""
    preview = kit_diet.disclose(text, limit=limit, spill_dir=SPILL_DIR,
                                label=label, stats=_spill_stats)
    spilled = len(text) > limit
    return {
        "preview": preview,
        "spilled": spilled,
        "spill_path": _spill_path_for(text) if spilled else None,
        "total_chars": len(text),
        "limit": limit,
        "label": label,
    }


# ------------------------------------------------------------ param checks
def _check_str(params, key, max_len=MAX_STR, content=False, required=True, default=None):
    """Validate one string param. Returns (ok, value, error)."""
    if key not in params:
        if required:
            return False, None, f"missing field: {key}"
        return True, default, None
    value = params[key]
    if not isinstance(value, str):
        return False, None, f"{key} must be a string"
    if not (0 < len(value) <= max_len):
        return False, None, f"{key} must be 1..{max_len} characters"
    if _has_bad_control(value, allow_content_ws=content):
        kind = "content" if content else "identifier"
        return False, None, f"{key} contains control characters invalid for a {kind} field"
    return True, value, None


def _check_int(params, key, lo, hi, required=False, default=None):
    if key not in params:
        if required:
            return False, None, f"missing field: {key}"
        return True, default, None
    value = params[key]
    if isinstance(value, bool) or not isinstance(value, int):
        return False, None, f"{key} must be an integer"
    if not (lo <= value <= hi):
        return False, None, f"{key} must be {lo}..{hi}"
    return True, value, None


def _validate_params(params, allowed):
    if not isinstance(params, dict):
        return None, "params must be an object"
    for key in params:
        if key not in allowed:
            return None, f"unknown field: {key}"
    return params, None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 30  # a lying Content-Length must not pin a thread forever

    def _reply(self, code, payload, close=False):
        if close:
            self.close_connection = True  # never leave undrained bodies on a keep-alive connection
        body = json.dumps(_redact_obj(payload)).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        if close:
            self.send_header("Connection", "close")  # advertise what the socket is about to do
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True  # client vanished mid-reply (e.g. a slowloris probe gave up); never traceback

    def do_GET(self):
        if self.path in ("/", "/health"):
            self._reply(200, self._status())
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/":
            self._reply(404, {"error": "not found"}, close=True)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._reply(400, {"error": "bad content-length"}, close=True)
            return
        if length <= 0 or length > MAX_BODY:
            self._reply(413 if length > MAX_BODY else 400,
                        {"error": "body must be 1..65536 bytes"}, close=True)
            return
        try:
            req = json.loads(self.rfile.read(length).decode("utf-8"))
        except (TimeoutError, socket.timeout, OSError):
            self._reply(408, {"error": "request body incomplete (timeout)"}, close=True)
            return
        except (ValueError, UnicodeDecodeError):
            self._reply(400, {"error": "body must be valid JSON"}, close=True)
            return
        if not isinstance(req, dict):
            self._reply(400, {"error": "body must be a JSON object"}, close=True)
            return
        method = req.get("method")
        params = req.get("params", {})
        for key in req:
            if key not in ("method", "params"):
                self._reply(400, {"error": f"unknown field: {key}"}, close=True)
                return
        if not isinstance(method, str):
            self._reply(400, {"error": "method must be a string"}, close=True)
            return
        handler = _METHODS.get(method)
        if handler is None:
            self._reply(400, {"error": f"unknown tool: {method}"})
            return
        handler(self, params)

    # -- contextkit.status --------------------------------------------------
    def _m_status(self, params):
        params, err = _validate_params(params, allowed=set())
        if err:
            self._reply(400, {"error": err})
            return
        self._reply(200, self._status())

    def _status(self):
        with _counters_lock:
            counts = dict(_counters)
        upstream = _VENDOR_META["upstream"]
        return {
            "ok": True,
            "version": "0.1.0",
            "kit": {
                "name": upstream["name"],
                "version": KIT_VERSION,
                "license": upstream["license"],
                "upstream_commit": upstream["commit"],
                "vendored_files": len(_VENDOR_META["files"]),
                "vendor": upstream["vendor"],
            },
            "served_modules": ["kit.styles", "kit.munch", "kit.diet"],
            "cache": {
                "hits": counts["cache_hits"],
                "misses": counts["cache_misses"],
                "entries": len(_read_cache._ent),
            },
            "spills": {
                "spilled": _spill_stats.spilled,
                "readback_note": (
                    "readbacks happen when the client re-reads the spilled file; "
                    "this service never sees them, so the ~15% guard (PATTERNS.md #6) "
                    "is the harness operator's measurement, not this service's claim"
                ),
            },
            "capabilities_requested": [],
            "headless_battery_execution": False,
            "headless_style_ab_execution": False,
            "honesty_note": (
                "kit.styles, kit.munch and kit.diet run for real inside this service "
                "(munch reads are sandboxed to var/samples; diet spills are confined "
                "to var/spill). The measurement instruments (tpt_battery.py, "
                "tpt_style.py) need a live llama.cpp-class OpenAI-compatible endpoint "
                "to measure against; this service never dials out, so they are pinned "
                "not-runnable here and never faked. Run them from the vendored tree "
                "against your own rig, n>=3 per arm in one thermal window."
            ),
        }

    # -- contextkit.styles --------------------------------------------------
    def _m_styles(self, params):
        params, err = _validate_params(params, allowed=set())
        if err:
            self._reply(400, {"error": err})
            return
        self._reply(200, {
            "ok": True,
            "styles": {k: kit_styles.STYLES[k] for k in STYLE_KEYS},
            "default": "fused",
            "laws": [
                "Style steers verbosity; the server-side budget stays the only hard backstop.",
                "Byte-stable position: style text leads the leading system message identically every request (prepend_stable).",
                "Exempt lanes: creative work and instant/no-think turns get no style.",
            ],
            "regime_note": (
                "Effective where the model would overthink (high-effort reasoning); "
                "counterproductive at low effort. Route per session, not per turn."
            ),
        })

    # -- contextkit.style.route --------------------------------------------
    def _m_style_route(self, params):
        params, err = _validate_params(params, allowed={"text", "think_mode", "dial"})
        if err:
            self._reply(400, {"error": err})
            return
        ok, text, err = _check_str(params, "text", max_len=MAX_TEXT, content=True)
        if err:
            self._reply(400, {"error": err})
            return
        think_mode = params.get("think_mode", "think")
        if think_mode not in THINK_MODES:
            self._reply(400, {"error": "think_mode must be one of: think, instant"})
            return
        dial = params.get("dial", "auto")
        if dial not in DIALS:
            self._reply(400, {"error": f"dial must be one of: {', '.join(DIALS)}"})
            return
        effective, creative = _kit_style_route(_redact_text(text), think_mode, dial)
        self._reply(200, {
            "ok": True,
            "effective_style": effective,
            "creative_lane": creative,
            "think_mode": think_mode,
            "dial": dial,
        })

    # -- contextkit.style.prepend ------------------------------------------
    def _m_style_prepend(self, params):
        params, err = _validate_params(params, allowed={"system_text", "style"})
        if err:
            self._reply(400, {"error": err})
            return
        ok, system_text, err = _check_str(params, "system_text", max_len=MAX_TEXT, content=True)
        if err:
            self._reply(400, {"error": err})
            return
        style = params.get("style")
        if style not in STYLE_KEYS:
            self._reply(400, {"error": f"style must be one of: {', '.join(STYLE_KEYS)}"})
            return
        content = kit_styles.prepend_stable(_redact_text(system_text), style)
        self._reply(200, {"ok": True, "style": style, "content": content})

    # -- contextkit.doc.list ------------------------------------------------
    def _m_doc_list(self, params):
        params, err = _validate_params(params, allowed=set())
        if err:
            self._reply(400, {"error": err})
            return
        files = sorted(_VENDOR_META["files"])
        self._reply(200, {
            "ok": True,
            "upstream_commit": _VENDOR_META["upstream"]["commit"],
            "count": len(files),
            "files": files,
        })

    # -- contextkit.doc.get -------------------------------------------------
    def _m_doc_get(self, params):
        params, err = _validate_params(params, allowed={"path"})
        if err:
            self._reply(400, {"error": err})
            return
        ok, rel_path, err = _check_str(params, "path")
        if err:
            self._reply(400, {"error": err})
            return
        if rel_path not in _VENDOR_META["files"]:
            self._reply(404, {"error": "unknown path in vendored tree; call contextkit.doc.list"})
            return
        abs_path = os.path.join(VENDOR_ROOT, rel_path)
        try:
            with open(abs_path, "rb") as f:
                data = f.read()
        except OSError:
            self._reply(422, {"error": "vendored file unreadable"})
            return
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            self._reply(422, {"error": "vendored file is not valid UTF-8 text"})
            return
        self._reply(200, {
            "ok": True,
            "path": rel_path,
            "size": len(text),
            "sha256": hashlib.sha256(data).hexdigest(),
            "content": text,  # already vendor-pinned content; response layer still redacts
        })

    # -- contextkit.munch.read_symbol ---------------------------------------
    def _m_read_symbol(self, params):
        params, err = _validate_params(params, allowed={"path", "name", "body"})
        if err:
            self._reply(400, {"error": err})
            return
        ok, rel_path, err = _check_str(params, "path")
        if err:
            self._reply(400, {"error": err})
            return
        ok, name, err = _check_str(params, "name")
        if err:
            self._reply(400, {"error": err})
            return
        want_body = params.get("body", False)
        if not isinstance(want_body, bool):
            self._reply(400, {"error": "body must be a boolean"})
            return
        result, cached = _kit_read_symbol(rel_path, name, want_body)
        if result is None:
            self._reply(404, {"error": "unknown or unreadable sandbox file; reads are confined to var/samples"})
            return
        self._reply(200, {"ok": True, "path": rel_path, "name": name,
                          "cached": cached, "result": result})

    # -- contextkit.diet.project --------------------------------------------
    def _m_diet_project(self, params):
        params, err = _validate_params(params, allowed={"events", "budget_chars", "min_body"})
        if err:
            self._reply(400, {"error": err})
            return
        events = params.get("events")
        if not isinstance(events, list) or not (1 <= len(events) <= MAX_EVENTS):
            self._reply(400, {"error": f"events must be an array of 1..{MAX_EVENTS} objects"})
            return
        cleaned = []
        for idx, event in enumerate(events):
            if not isinstance(event, dict):
                self._reply(400, {"error": f"events[{idx}] must be an object"})
                return
            for field in ("kind", "content"):
                if field in event and not isinstance(event[field], str):
                    self._reply(400, {"error": f"events[{idx}].{field} must be a string"})
                    return
            if "content" in event:
                if len(event["content"]) > MAX_BODY:
                    self._reply(400, {"error": f"events[{idx}].content exceeds {MAX_BODY} characters"})
                    return
                event = dict(event, content=_redact_text(event["content"]))
            cleaned.append(event)
        ok, budget_chars, err = _check_int(params, "budget_chars", 1000, 10_000_000, default=300000)
        if err:
            self._reply(400, {"error": err})
            return
        ok, min_body, err = _check_int(params, "min_body", 0, 100_000, default=200)
        if err:
            self._reply(400, {"error": err})
            return
        self._reply(200, {"ok": True, **_kit_project(cleaned, budget_chars, min_body)})

    # -- contextkit.diet.disclose -------------------------------------------
    def _m_diet_disclose(self, params):
        params, err = _validate_params(params, allowed={"text", "limit", "label"})
        if err:
            self._reply(400, {"error": err})
            return
        ok, text, err = _check_str(params, "text", max_len=MAX_TEXT, content=True)
        if err:
            self._reply(400, {"error": err})
            return
        ok, limit, err = _check_int(params, "limit", 100, 65535, default=8000)
        if err:
            self._reply(400, {"error": err})
            return
        ok, label, err = _check_str(params, "label", max_len=64, required=False, default="output")
        if err:
            self._reply(400, {"error": err})
            return
        self._reply(200, {"ok": True, **_kit_disclose(_redact_text(text), limit, label)})

    def log_message(self, fmt, *args):  # keep service logs quiet and content-free
        sys.stderr.write("context-kit-service: " + (fmt % args) + "\n")


_METHODS = {
    "contextkit.status": Handler._m_status,
    "contextkit.styles": Handler._m_styles,
    "contextkit.style.route": Handler._m_style_route,
    "contextkit.style.prepend": Handler._m_style_prepend,
    "contextkit.doc.list": Handler._m_doc_list,
    "contextkit.doc.get": Handler._m_doc_get,
    "contextkit.munch.read_symbol": Handler._m_read_symbol,
    "contextkit.diet.project": Handler._m_diet_project,
    "contextkit.diet.disclose": Handler._m_diet_disclose,
}


def main():
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as exc:
        sys.stderr.write(f"context-kit-service: cannot bind 127.0.0.1:{PORT} ({exc}); manifest entrypoint expects this port\n")
        return 78
    sys.stderr.write(f"context-kit-service: listening on http://127.0.0.1:{PORT}\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
