"""addon.context-kit wrapper tests.

Run:  python3 -m unittest discover -s tests -v   (from the add-on root)

Covers: vendor hash-pin vs upstream HEAD + byte-identity vs git archive,
vendored kit behavior (styles/munch/diet, determinism), the service surface
honesty pins, strict per-method params, adversarial HTTP behavior, home-path
redaction (at entry, on responses, spill files, and the whole tree), sandbox
containment of munch reads, and manifest parity incl. port uniqueness against
all sibling add-ons.
"""
import hashlib
import io
import json
import os
import re
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON_ROOT = os.path.dirname(HERE)
UPSTREAM = os.environ.get("CONTEXTKIT_UPSTREAM", "/tmp/context-kit-src")
sys.path.insert(0, ADDON_ROOT)

import server  # noqa: E402

MANIFEST_PORT = 4901


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def post(payload, raw=None, base=None):
    body = raw if raw is not None else json.dumps(payload).encode()
    req = urllib.request.Request((base or BASE) + "/", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode())


def post_err(payload, raw=None, base=None):
    try:
        return post(payload, raw, base)
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, json.loads(exc.read().decode())


def raw_request(payload_bytes, port, timeout=10):
    """One raw socket request; returns (status_line, response_bytes|None)."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        sock.sendall(payload_bytes)
        try:
            data = sock.recv(65536)
            return data.split(b"\r\n", 1)[0].decode(), data
        except (ConnectionResetError, BrokenPipeError):
            return "connection-closed", None
    finally:
        sock.close()


class Service:
    """In-process service on an EPHEMERAL port (never the manifest port, which
    belongs to the deployed service contract and live sibling processes)."""

    def __enter__(self):
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    def url(self):
        return f"http://127.0.0.1:{self.port}"


BASE = ""  # helpers take an explicit base from Service().url()


class TestVendorPin(unittest.TestCase):
    """The vendored kit must be byte-identical to upstream commit 5456928a."""

    VENDOR_MANIFEST = os.path.join(ADDON_ROOT, "vendor", "VENDOR-MANIFEST.json")

    @classmethod
    def setUpClass(cls):
        with open(cls.VENDOR_MANIFEST) as f:
            cls.meta = json.load(f)

    def test_manifest_pins_expected_upstream(self):
        self.assertEqual(self.meta["upstream"]["name"], "context-kit")
        self.assertEqual(self.meta["upstream"]["vendor"], "KyaniteLabs")
        self.assertEqual(self.meta["upstream"]["license"], "MIT")
        self.assertEqual(self.meta["upstream"]["commit"],
                         "5456928ac1dcf1ca21f7ace78a4b1204d2871004")
        self.assertIn("git archive HEAD", self.meta["upstream"]["method"])

    def test_every_pinned_file_matches_recorded_hash(self):
        self.assertGreaterEqual(len(self.meta["files"]), 10)
        for rel, expected in self.meta["files"].items():
            path = os.path.join(ADDON_ROOT, "vendor", rel)
            self.assertTrue(os.path.isfile(path), f"missing vendored file: {rel}")
            with open(path, "rb") as f:
                self.assertEqual(sha256(f.read()), expected, f"vendor drift: {rel}")

    def test_no_unlisted_files_in_vendor(self):
        """The pin must be complete: an unlisted file under vendor/ would be
        served by contextkit.doc.get while evading every hash check."""
        unlisted = []
        for root, dirs, names in os.walk(os.path.join(ADDON_ROOT, "vendor")):
            dirs[:] = [d for d in dirs if d != "__pycache__"]  # bytecode caches are runtime artifacts
            for n in names:
                if n.endswith(".pyc"):
                    continue
                rel = os.path.relpath(os.path.join(root, n),
                                      os.path.join(ADDON_ROOT, "vendor"))
                if rel != "VENDOR-MANIFEST.json" and rel not in self.meta["files"]:
                    unlisted.append(rel)
        self.assertEqual(unlisted, [])

    def test_vendored_bytes_identical_to_git_archive_output(self):
        """Byte-identity against a FRESH `git archive HEAD` of the upstream
        clone (the committed tree, never the working tree). Skipped on
        machines without the clone; the recorded hash-pin still guards
        integrity there."""
        if not os.path.isdir(os.path.join(UPSTREAM, ".git")):
            self.skipTest(f"upstream clone not present at {UPSTREAM}")
        arch = subprocess.run(["git", "-C", UPSTREAM, "archive", "HEAD"],
                              capture_output=True, check=True).stdout
        archived = {}
        with tarfile.open(fileobj=io.BytesIO(arch)) as tf:
            for member in tf.getmembers():
                if member.isfile():
                    archived[member.name] = sha256(tf.extractfile(member).read())
        self.assertEqual(set(archived), set(self.meta["files"]))
        for rel, digest in archived.items():
            self.assertEqual(digest, self.meta["files"][rel], f"archive drift: {rel}")
            with open(os.path.join(ADDON_ROOT, "vendor", rel), "rb") as f:
                self.assertEqual(sha256(f.read()), digest, f"not byte-identical: {rel}")

    def test_upstream_head_matches_pin_and_tree_clean(self):
        """A dirty upstream working tree is a FINDING (nothing uncommitted was
        vendored); this test pins that HEAD is exactly what we recorded."""
        if not os.path.isdir(os.path.join(UPSTREAM, ".git")):
            self.skipTest(f"upstream clone not present at {UPSTREAM}")
        head = subprocess.run(["git", "-C", UPSTREAM, "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(head, self.meta["upstream"]["commit"])
        status = subprocess.run(["git", "-C", UPSTREAM, "status", "--porcelain"],
                                capture_output=True, text=True, check=True).stdout
        self.assertEqual(status, "", "upstream working tree has uncommitted changes")

    def test_pinned_pack_version_matches_vendored_kit(self):
        with open(os.path.join(ADDON_ROOT, "vendor", "kit", "__init__.py")) as f:
            match = re.search(r'__version__\s*=\s*"([^"]+)"', f.read())
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), self.meta["upstream"]["pack_version"])

    def test_addon_license_is_kit_license_verbatim(self):
        with open(os.path.join(ADDON_ROOT, "LICENSE"), "rb") as a, \
             open(os.path.join(ADDON_ROOT, "vendor", "LICENSE"), "rb") as b:
            self.assertEqual(a.read(), b.read())


class TestVendoredKitBehavior(unittest.TestCase):
    """The vendored modules do what the kit says they do (determinism laws)."""

    @classmethod
    def setUpClass(cls):
        cls.styles = server.kit_styles
        cls.munch = server.kit_munch
        cls.diet = server.kit_diet

    def test_style_router_laws(self):
        self.assertIsNone(self.styles.style_layer("any text", "instant"))
        self.assertIsNone(self.styles.style_layer("write me a poem about rain", "think"))
        self.assertIsNone(self.styles.style_layer("anything", "think", dial="off"))
        self.assertEqual(self.styles.style_layer("fix this bug", "think"), "fused")
        self.assertEqual(self.styles.style_layer("fix this bug", "think", dial="ponytail"), "ponytail")
        self.assertEqual(self.styles.style_layer("write a story", "think", dial="caveman"), "caveman")

    def test_prepend_stable_is_byte_deterministic_and_leads(self):
        out1 = self.styles.prepend_stable("You are a helpful agent.", "fused")
        out2 = self.styles.prepend_stable("You are a helpful agent.", "fused")
        self.assertEqual(out1, out2)  # byte-stable: identical bytes every call
        self.assertTrue(out1.startswith(self.styles.STYLES["fused"]))
        self.assertTrue(out1.endswith("You are a helpful agent."))
        self.assertIn("\n\n", out1)

    def _events(self):
        body = "x" * 5120
        return [{"kind": "tool_result", "content": body, "i": i} for i in range(3)]

    def test_project_dedup_arithmetic_and_non_mutation(self):
        events = self._events()
        snapshot = json.loads(json.dumps(events))
        projected = self.diet.project(events)
        self.assertEqual(events, snapshot, "input events must never be mutated")
        self.assertEqual(len(projected), 3)
        self.assertEqual(projected[0]["content"], "x" * 5120)  # first stays full
        self.assertTrue(projected[1]["content"].startswith("(unchanged output"))
        chars_in = sum(len(e["content"]) for e in events)
        chars_out = sum(len(e["content"]) for e in projected)
        self.assertLess(chars_out, chars_in * 0.5)  # >-50% on the 3x-identical class
        self.assertEqual(self.diet.project(self._events()), projected)  # determinism

    def test_project_elides_oldest_first_under_budget(self):
        events = [{"kind": "tool_result", "content": "a" * 400},
                  {"kind": "tool_result", "content": "b" * 400},
                  {"kind": "user_message", "content": "keep"}]
        projected = self.diet.project(events, budget_chars=500)
        self.assertEqual(projected[0]["content"], "(elided)")  # oldest body elided first
        self.assertEqual(projected[0]["_elided"], "a" * 200)   # 200-char preview survives
        self.assertEqual(projected[-1]["content"], "keep")     # non-tool_result untouched

    def test_disclose_head_tail_and_content_addressing(self):
        with tempfile.TemporaryDirectory() as tmp:
            stats = self.diet.SpillStats()
            text = "h" * 3000 + "m" * 4000 + "t" * 1000
            preview = self.diet.disclose(text, limit=4000, spill_dir=tmp, stats=stats)
            self.assertIn("FULL OUTPUT SPILLED", preview)
            self.assertTrue(preview.startswith(text[:3000]))   # head
            self.assertTrue(preview.endswith(text[-1000:]))    # tail
            spill = re.search(r"SPILLED TO (\S+\.txt)", preview).group(1)
            with open(spill) as f:
                self.assertEqual(f.read(), text)               # full body on disk
            again = self.diet.disclose(text, limit=4000, spill_dir=tmp, stats=stats)
            self.assertEqual(re.search(r"SPILLED TO (\S+\.txt)", again).group(1), spill)
            self.assertEqual(stats.spilled, 2)                 # same address, both counted
            self.assertEqual(stats.ratio(), 0.0)               # no readbacks seen

    def test_result_cache_mtime_invalidation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mod.py")
            with open(path, "w") as f:
                f.write("def alpha():\n    '''first'''\n")
            cache = self.munch.ResultCache()
            r1 = self.munch.cached_read(cache, path,
                                        lambda p: self.munch.read_symbol(p, "alpha"))
            self.assertIn("first", r1)
            r2 = self.munch.cached_read(cache, path,
                                        lambda p: self.munch.read_symbol(p, "alpha"))
            self.assertEqual(r1, r2)
            with open(path, "w") as f:
                f.write("def alpha():\n    '''second'''\n")
            r3 = self.munch.cached_read(cache, path,
                                        lambda p: self.munch.read_symbol(p, "alpha"))
            self.assertIn("second", r3)  # mtime change invalidated the cache

    def test_read_symbol_hit_miss_candidates(self):
        path = os.path.join(ADDON_ROOT, "var", "samples", "greeter.py")
        hit = self.munch.read_symbol(path, "greet")
        self.assertIn("def greet", hit)
        self.assertIn("greeter.py:", hit)
        self.assertIn("Return a greeting for name.", hit)
        miss = self.munch.read_symbol(path, "no_such_symbol")
        self.assertTrue(miss.startswith("ERROR: symbol 'no_such_symbol' not in"))
        self.assertIn("Candidates:", miss)
        self.assertIn("greet_all", miss)
        notpy = self.munch.read_symbol(
            os.path.join(ADDON_ROOT, "var", "samples", "notes.txt"), "x")
        self.assertIn("supports .py only", notpy)


class TestServiceSurface(unittest.TestCase):
    def setUp(self):
        self.svc = Service()
        self.svc.__enter__()
        self.base = self.svc.url()
        self.addCleanup(self.svc.__exit__, None, None, None)

    def test_status_roundtrip_and_honesty_pins(self):
        code, body = post({"method": "contextkit.status"}, base=self.base)
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["version"], "0.1.0")
        self.assertEqual(body["kit"]["name"], "context-kit")
        self.assertEqual(body["kit"]["license"], "MIT")
        self.assertEqual(body["kit"]["upstream_commit"],
                         "5456928ac1dcf1ca21f7ace78a4b1204d2871004")
        self.assertEqual(body["served_modules"], ["kit.styles", "kit.munch", "kit.diet"])
        self.assertEqual(body["capabilities_requested"], [])
        self.assertFalse(body["headless_battery_execution"])   # honesty pin, tested
        self.assertFalse(body["headless_style_ab_execution"])  # honesty pin, tested
        self.assertIn("never faked", body["honesty_note"])
        self.assertIn("llama.cpp", body["honesty_note"])

    def test_health_get(self):
        with urllib.request.urlopen(self.base + "/health", timeout=10) as resp:
            body = json.loads(resp.read().decode())
        self.assertTrue(body["ok"])

    def test_styles_lists_the_three_prompts(self):
        code, body = post({"method": "contextkit.styles"}, base=self.base)
        self.assertEqual(code, 200)
        self.assertEqual(sorted(body["styles"]), ["caveman", "fused", "ponytail"])
        self.assertEqual(body["default"], "fused")
        self.assertEqual(len(body["laws"]), 3)
        for text in body["styles"].values():
            self.assertTrue(text.startswith("THINKING STYLE"))

    def test_style_route_laws(self):
        code, body = post({"method": "contextkit.style.route",
                           "params": {"text": "fix this parsing bug"}}, base=self.base)
        self.assertEqual((code, body["effective_style"], body["creative_lane"]),
                         (200, "fused", False))
        code, body = post({"method": "contextkit.style.route",
                           "params": {"text": "write a vivid poem", "dial": "auto"}},
                          base=self.base)
        self.assertEqual((code, body["effective_style"], body["creative_lane"]),
                         (200, None, True))
        code, body = post({"method": "contextkit.style.route",
                           "params": {"text": "hello", "think_mode": "instant"}},
                          base=self.base)
        self.assertEqual((code, body["effective_style"]), (200, None))
        code, body = post({"method": "contextkit.style.route",
                           "params": {"text": "hello", "dial": "ponytail"}}, base=self.base)
        self.assertEqual((code, body["effective_style"]), (200, "ponytail"))

    def test_style_prepend_byte_stable(self):
        params = {"system_text": "You are an agent.", "style": "caveman"}
        code, body1 = post({"method": "contextkit.style.prepend", "params": params},
                           base=self.base)
        code, body2 = post({"method": "contextkit.style.prepend", "params": params},
                           base=self.base)
        self.assertEqual(code, 200)
        self.assertEqual(body1["content"], body2["content"])  # determinism over the wire
        self.assertTrue(body1["content"].startswith(server.kit_styles.STYLES["caveman"]))
        self.assertTrue(body1["content"].endswith("You are an agent."))

    def test_doc_list_and_get(self):
        code, listing = post({"method": "contextkit.doc.list"}, base=self.base)
        self.assertEqual(code, 200)
        self.assertIn("README.md", listing["files"])
        self.assertIn("docs/PATTERNS.md", listing["files"])
        self.assertIn("kit/diet.py", listing["files"])
        self.assertEqual(listing["count"], len(listing["files"]))
        code, body = post({"method": "contextkit.doc.get",
                           "params": {"path": "README.md"}}, base=self.base)
        self.assertEqual(code, 200)
        self.assertTrue(body["content"].startswith("# context-kit"))
        self.assertEqual(body["sha256"], sha256(body["content"].encode()))
        with open(os.path.join(ADDON_ROOT, "vendor", "README.md"), "rb") as f:
            self.assertEqual(body["sha256"], sha256(f.read()))  # served verbatim from vendor
        code, _ = post_err({"method": "contextkit.doc.get",
                            "params": {"path": "../server.py"}}, base=self.base)
        self.assertEqual(code, 404)  # exact manifest-key match; traversal unreachable

    def test_munch_read_symbol_hit_cached_and_miss(self):
        params = {"path": "greeter.py", "name": "greet"}
        code, body1 = post({"method": "contextkit.munch.read_symbol", "params": params},
                           base=self.base)
        self.assertEqual(code, 200)
        self.assertFalse(body1["cached"])
        self.assertIn("def greet", body1["result"])
        self.assertIn("Return a greeting for name.", body1["result"])
        code, body2 = post({"method": "contextkit.munch.read_symbol", "params": params},
                           base=self.base)
        self.assertEqual(code, 200)
        self.assertTrue(body2["cached"])                   # the kit's cache, live
        self.assertEqual(body1["result"], body2["result"])  # byte-identical
        code, body3 = post({"method": "contextkit.munch.read_symbol",
                            "params": {"path": "greeter.py", "name": "Greeter",
                                       "body": True}}, base=self.base)
        self.assertEqual(code, 200)
        self.assertIn("def hello", body3["result"])
        code, body4 = post({"method": "contextkit.munch.read_symbol",
                            "params": {"path": "greeter.py", "name": "missing"}},
                           base=self.base)
        self.assertEqual((code, body4["cached"]), (200, False))
        self.assertTrue(body4["result"].startswith("ERROR: symbol 'missing' not in"))
        self.assertIn("Candidates:", body4["result"])
        code, body5 = post({"method": "contextkit.munch.read_symbol",
                            "params": {"path": "notes.txt", "name": "x"}}, base=self.base)
        self.assertIn("supports .py only", body5["result"])

    def test_munch_sandbox_containment(self):
        for evil in ("../server.py", "../vendor/kit/styles.py", "/etc/passwd",
                     "~/.zshrc", "sub/../../server.py"):
            code, _ = post_err({"method": "contextkit.munch.read_symbol",
                                "params": {"path": evil, "name": "x"}}, base=self.base)
            self.assertEqual(code, 404, evil)
        code, _ = post_err({"method": "contextkit.munch.read_symbol",
                            "params": {"path": "no_such_file.py", "name": "x"}},
                           base=self.base)
        self.assertEqual(code, 404)

    def test_diet_project_determinism_and_savings(self):
        body_text = "y" * 5120
        events = [{"kind": "tool_result", "content": body_text} for _ in range(3)]
        code, body1 = post({"method": "contextkit.diet.project",
                            "params": {"events": events}}, base=self.base)
        self.assertEqual(code, 200)
        code, body2 = post({"method": "contextkit.diet.project",
                            "params": {"events": events}}, base=self.base)
        self.assertEqual(body1, body2)  # deterministic over the wire
        self.assertEqual(body1["events_out"], 3)
        self.assertEqual(body1["projected"][0]["content"], body_text)
        self.assertTrue(body1["projected"][1]["content"].startswith("(unchanged output"))
        self.assertGreater(body1["chars_saved"], 0)

    def test_diet_project_passthrough_and_budget(self):
        events = [{"kind": "tool_result", "content": "a" * 700},
                  {"kind": "tool_result", "content": "b" * 700},
                  {"kind": "user_message", "content": "keep me"}]
        code, body = post({"method": "contextkit.diet.project",
                           "params": {"events": events, "budget_chars": 1000}},
                          base=self.base)
        self.assertEqual(code, 200)
        self.assertEqual(body["projected"][0]["content"], "(elided)")
        self.assertEqual(body["projected"][1]["content"], "b" * 700)
        self.assertEqual(body["projected"][2]["content"], "keep me")

    def test_diet_disclose_spills_under_var(self):
        text = "p" * 3000 + "|" + "q" * 2000
        code, body = post({"method": "contextkit.diet.disclose",
                           "params": {"text": text, "limit": 4000, "label": "probe"}},
                          base=self.base)
        self.assertEqual(code, 200)
        self.assertTrue(body["spilled"])
        self.assertIn("FULL OUTPUT SPILLED", body["preview"])
        self.assertIn("probe", body["preview"])
        spill = body["spill_path"]
        self.assertIsNotNone(spill)
        self.assertIn("var/spill", spill)
        self.assertNotIn(os.path.expanduser("~"), spill)  # redacted generated path
        disk_path = spill.replace("~", os.path.expanduser("~"))
        self.assertTrue(os.path.isfile(disk_path))
        # content-addressed: identical text -> identical spill path
        code, body2 = post({"method": "contextkit.diet.disclose",
                            "params": {"text": text, "limit": 4000}}, base=self.base)
        self.assertEqual(body2["spill_path"], spill)
        # under the limit: verbatim, no spill
        code, body3 = post({"method": "contextkit.diet.disclose",
                            "params": {"text": "short"}}, base=self.base)
        self.assertFalse(body3["spilled"])
        self.assertIsNone(body3["spill_path"])
        self.assertEqual(body3["preview"], "short")
        # spill file bytes are the (entry-redacted) input, verbatim
        with open(disk_path) as f:
            self.assertEqual(f.read(), text)


class TestStrictParams(unittest.TestCase):
    def setUp(self):
        self.svc = Service()
        self.svc.__enter__()
        self.base = self.svc.url()
        self.addCleanup(self.svc.__exit__, None, None, None)

    def test_unknown_tool_400(self):
        code, body = post_err({"method": "contextkit.run"}, base=self.base)
        self.assertEqual(code, 400)
        self.assertIn("unknown tool", body["error"])

    def test_unknown_field_in_envelope_400(self):
        code, _ = post_err({"method": "contextkit.status", "extra": 1}, base=self.base)
        self.assertEqual(code, 400)

    def test_unknown_param_field_400(self):
        code, _ = post_err({"method": "contextkit.style.route",
                            "params": {"text": "x", "evil": 1}}, base=self.base)
        self.assertEqual(code, 400)

    def test_control_chars_identifier_params_400(self):
        cases = (
            ("contextkit.munch.read_symbol", {"path": "gr\x0beeter.py", "name": "greet"}),
            ("contextkit.munch.read_symbol", {"path": "greeter.py", "name": "gr\x7feet"}),
            ("contextkit.doc.get", {"path": "RE\x01ADME.md"}),
            ("contextkit.diet.disclose", {"text": "x" * 200, "label": "pro\x02be"}),
            ("contextkit.style.route", {"text": "x\x1b"}),
        )
        for method, params in cases:
            code, _ = post_err({"method": method, "params": params}, base=self.base)
            self.assertEqual(code, 400, (method, params))

    def test_content_params_allow_tab_newline_cr(self):
        text = "line1\nline2\ttabbed\rcr"
        code, _ = post({"method": "contextkit.style.prepend",
                        "params": {"system_text": text, "style": "fused"}}, base=self.base)
        self.assertEqual(code, 200)
        code, _ = post({"method": "contextkit.diet.project",
                        "params": {"events": [{"kind": "tool_result", "content": text}]}},
                       base=self.base)
        self.assertEqual(code, 200)
        code, _ = post({"method": "contextkit.diet.disclose",
                        "params": {"text": text, "limit": 100}}, base=self.base)
        self.assertEqual(code, 200)

    def test_missing_required_param_400(self):
        code, _ = post_err({"method": "contextkit.style.route", "params": {}}, base=self.base)
        self.assertEqual(code, 400)
        code, _ = post_err({"method": "contextkit.diet.project", "params": {}}, base=self.base)
        self.assertEqual(code, 400)

    def test_non_object_body_400(self):
        code, _ = post_err(None, raw=b"[1,2,3]", base=self.base)
        self.assertEqual(code, 400)

    def test_invalid_json_400(self):
        code, _ = post_err(None, raw=b"{nope", base=self.base)
        self.assertEqual(code, 400)

    def test_bad_content_length_400(self):
        status, _ = raw_request(
            b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: nope\r\n\r\n", self.svc.port)
        self.assertTrue(status.startswith("HTTP/1.1 400"), status)

    def test_wrong_param_types_400(self):
        cases = (
            ("contextkit.style.route", {"text": 42}),
            ("contextkit.munch.read_symbol",
             {"path": "greeter.py", "name": "greet", "body": "yes"}),
            ("contextkit.diet.project", {"events": {"kind": "tool_result"}}),
            ("contextkit.diet.project", {"events": [{"kind": 5}]}),
            ("contextkit.diet.project", {"events": "nope"}),
            ("contextkit.diet.project", {"events": []}),
            ("contextkit.diet.project", {"events": [{"kind": "tool_result", "content": "x"}],
                                         "budget_chars": "big"}),
            ("contextkit.diet.disclose", {"text": "x", "limit": 10}),  # below minimum
        )
        for method, params in cases:
            code, _ = post_err({"method": method, "params": params}, base=self.base)
            self.assertEqual(code, 400, (method, params))


class TestRedaction(unittest.TestCase):
    def setUp(self):
        self.svc = Service()
        self.svc.__enter__()
        self.base = self.svc.url()
        self.addCleanup(self.svc.__exit__, None, None, None)

    def test_redact_helpers(self):
        home = os.path.expanduser("~")
        self.assertEqual(server._redact_text("x" + home + "/y"), "x~/y")
        self.assertEqual(server._redact_obj({"a": [home + "/b"], "c": 3}),
                         {"a": ["~/b"], "c": 3})

    def test_input_redaction_flows_through_kit_functions(self):
        """Content is redacted on ENTRY: the kit functions compute on redacted
        bytes, so spill files and projections carry no home path either."""
        home = os.path.expanduser("~")
        code, body = post({"method": "contextkit.diet.disclose",
                           "params": {"text": "log at " + home + "/models/x " + "z" * 9000,
                                      "limit": 100}}, base=self.base)
        self.assertEqual(code, 200)
        self.assertNotIn(home, body["preview"])
        self.assertIn("~/models/x", body["preview"])
        disk_path = body["spill_path"].replace("~", os.path.expanduser("~"))
        with open(disk_path) as f:
            spilled = f.read()
        self.assertNotIn(home, spilled)  # disk redaction, not just response redaction
        self.assertIn("~/models/x", spilled)

    def test_no_home_paths_in_whole_tree(self):
        needle = (os.sep + "Users" + os.sep).encode()  # built at runtime so this file stays clean
        skip = {"__pycache__", ".git"}
        for root, dirs, files in os.walk(ADDON_ROOT):
            dirs[:] = [d for d in dirs if d not in skip]
            for name in files:
                if name.endswith(".pyc"):
                    continue
                path = os.path.join(root, name)
                with open(path, "rb") as f:
                    content = f.read()
                self.assertNotIn(needle, content, f"home path leaked in {path}")


class TestAdversarialHTTP(unittest.TestCase):
    def setUp(self):
        self.svc = Service()
        self.svc.__enter__()
        self.port = self.svc.port
        self.base = self.svc.url()
        self.addCleanup(self.svc.__exit__, None, None, None)

    def test_oversized_body_413_and_close(self):
        big = json.dumps({"method": "contextkit.status", "pad": "x" * 100000}).encode()
        self.assertGreater(len(big), server.MAX_BODY)
        status, data = raw_request(
            b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: " + str(len(big)).encode()
            + b"\r\n\r\n" + big[:65536 + 1], self.port)
        self.assertTrue(status.startswith("HTTP/1.1 413"), status)
        self.assertIsNotNone(data)

    def test_lying_content_length_408(self):
        """Declaring more bytes than sent must not hang or misparse: 408 + close.
        Handler timeout is patched to 1s so the suite stays fast; production
        uses 30s (same code path)."""
        original_timeout = server.Handler.timeout
        server.Handler.timeout = 1
        try:
            status, _ = raw_request(
                b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 500\r\n\r\n"
                b'{"method":"cont', self.port)
            self.assertTrue(status.startswith("HTTP/1.1 408"), status)
        finally:
            server.Handler.timeout = original_timeout

    def test_chunked_encoding_400(self):
        status, _ = raw_request(
            b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"1a\r\n{\"method\":\"contextkit.status\"}\r\n0\r\n\r\n", self.port)
        self.assertTrue(status.startswith("HTTP/1.1 400"), status)

    def test_request_flood_20_concurrent(self):
        errors = []

        def hit(n):
            try:
                code, body = post({"method": "contextkit.status"}, base=self.base)
                if code != 200 or not body["ok"]:
                    errors.append((n, code))
            except Exception as exc:  # noqa: BLE001
                errors.append((n, repr(exc)))

        threads = [threading.Thread(target=hit, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])


class TestManifestParity(unittest.TestCase):
    """The manifest must promise exactly what server.py serves."""

    ADDONS_DIR = os.path.dirname(ADDON_ROOT)

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ADDON_ROOT, "addon.json")) as f:
            cls.manifest = json.load(f)

    def test_manifest_id_and_entrypoint(self):
        self.assertEqual(self.manifest["id"], "addon.context-kit")
        self.assertEqual(self.manifest["service"]["entrypoint"],
                         f"http://127.0.0.1:{MANIFEST_PORT}")
        self.assertEqual(self.manifest["service"]["healthCommand"], "contextkit.status")
        self.assertEqual(self.manifest["service"]["protocol"], "http-json")

    def test_entrypoint_loopback_only(self):
        self.assertTrue(self.manifest["service"]["entrypoint"].startswith("http://127.0.0.1:"))

    def test_every_declared_tool_is_served(self):
        src = open(os.path.join(ADDON_ROOT, "server.py")).read()
        methods = []
        for tool in self.manifest["tools"]:
            self.assertIn(f'"{tool["name"]}"', src,
                          f"manifest tool not routed in server: {tool['name']}")
            self.assertIsInstance(tool["inputSchema"], dict)
            self.assertIsInstance(tool["outputSchema"], dict)
            self.assertEqual(tool["requiredCapabilities"], [])
            methods.append(tool["name"])
        self.assertEqual(len(methods), len(set(methods)))

    def test_no_undeclared_contextkit_methods_served(self):
        src = open(os.path.join(ADDON_ROOT, "server.py")).read()
        served = set(re.findall(r'"(contextkit\.[a-z._]+)"', src))
        declared = {t["name"] for t in self.manifest["tools"]}
        self.assertEqual(served, declared, "server surface and manifest tools diverged")

    def test_zero_capabilities_claimed(self):
        self.assertEqual(self.manifest["requestedCapabilities"], [])
        self.assertEqual(self.manifest["grantPresets"], [])

    def test_port_unique_across_all_siblings(self):
        """The manifest port must be UNUSED by every sibling add-on manifest."""
        collisions = []
        for name in sorted(os.listdir(self.ADDONS_DIR)):
            sibling = os.path.join(self.ADDONS_DIR, name)
            if name == os.path.basename(ADDON_ROOT) or not os.path.isdir(sibling):
                continue
            manifest_path = os.path.join(sibling, "addon.json")
            if not os.path.isfile(manifest_path):
                continue
            with open(manifest_path) as f:
                sibling_manifest = f.read()
            if f":{MANIFEST_PORT}" in sibling_manifest:
                collisions.append(name)
        self.assertEqual(collisions, [], f"port {MANIFEST_PORT} collides with: {collisions}")


if __name__ == "__main__":
    unittest.main()
