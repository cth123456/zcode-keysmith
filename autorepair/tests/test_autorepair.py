#!/usr/bin/env python3
"""Tests for the keysmith-autorepair layer.

These exercise the real code paths: the Codex adapter against a temporary
config file and a stand-in binary, the platform registry through the documented
drop-in extension point, and the unattended repair gate (auto_repair AND a
recorded baseline) through the actual CLI.

Run: python3 -m unittest discover -s autorepair/tests -v
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

AUTOREPAIR_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AUTOREPAIR_DIR))

import autorepair_core as core  # noqa: E402
import autorepair_platform_codex as codex  # noqa: E402

CLI = AUTOREPAIR_DIR / "keysmith-autorepair.py"

STUB_PLATFORM = '''
"""Drop-in test platform."""

import json

PLATFORM_ID = "stub"
PLATFORM_LABEL = "Stub"


def detect(ctx):
    return {"installed": True, "app_version": "1.0", "runtime_path": "/tmp/stub"}


def probe(ctx, detection):
    return {
        "state": "needs_repair",
        "injected": False,
        "anchors_ok": True,
        "runtime_sha256": "stub-sha",
        "app_version": "1.0",
        "detail": "stub needs repair",
    }


def repair(ctx, probe):
    _count(ctx, "repair")
    if ctx.dry_run:
        return {"ok": True, "actions": [], "detail": "plan"}
    return {"ok": True, "actions": [], "detail": "repaired"}


def verify(ctx):
    _count(ctx, "verify")
    return {"ok": True, "actions": [], "detail": "verified"}


def revert(ctx):
    _count(ctx, "revert")
    return {"ok": True, "actions": [], "detail": "reverted"}


def _count(ctx, name):
    path = ctx.managed_dir / "calls.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    data[name] = data.get(name, 0) + 1
    path.write_text(json.dumps(data))
'''


def make_ctx(managed_dir: Path, *, dry_run: bool, config: dict | None = None) -> core.PlatformContext:
    return core.PlatformContext(
        managed_dir=managed_dir,
        config=config or {"platforms": {}},
        state={"schema": core.STATE_SCHEMA, "platforms": {}},
        dry_run=dry_run,
    )


class TomlEditingTests(unittest.TestCase):
    """The Codex key must only ever be written at the top level."""

    FIXTURE = (
        'model = "gpt-6-astra"\n'
        "approval_policy = \"never\"\n"
        "\n"
        "[plugins.thing]\n"
        'model_instructions_file = "/should/not/be/touched.md"\n'
        "\n"
        "[profiles.work]\n"
        'model_instructions_file = "/also/untouched.md"\n'
    )

    def test_reading_ignores_table_scoped_keys(self) -> None:
        self.assertIsNone(codex._read_top_level_value(self.FIXTURE))

    def test_insert_places_key_in_top_level_block(self) -> None:
        updated = codex._set_top_level_value(self.FIXTURE, "/managed/model-instructions.md")
        top_level, _, tables = updated.partition("[plugins.thing]")
        self.assertIn('model_instructions_file = "/managed/model-instructions.md"', top_level)
        self.assertIn('model = "gpt-6-astra"', top_level)
        # The pre-existing table-scoped keys are untouched.
        self.assertIn('model_instructions_file = "/should/not/be/touched.md"', tables)
        self.assertIn('model_instructions_file = "/also/untouched.md"', tables)
        self.assertEqual(codex._read_top_level_value(updated), "/managed/model-instructions.md")

    def test_replace_keeps_a_single_top_level_key(self) -> None:
        once = codex._set_top_level_value(self.FIXTURE, "/first.md")
        twice = codex._set_top_level_value(once, "/second.md")
        self.assertEqual(twice.count('model_instructions_file = "/second.md"'), 1)
        self.assertEqual(codex._read_top_level_value(twice), "/second.md")
        self.assertIn('model_instructions_file = "/should/not/be/touched.md"', twice)

    def test_remove_only_touches_top_level(self) -> None:
        once = codex._set_top_level_value(self.FIXTURE, "/first.md")
        removed = codex._remove_top_level_value(once)
        self.assertIsNone(codex._read_top_level_value(removed))
        self.assertIn('model_instructions_file = "/should/not/be/touched.md"', removed)

    def test_config_without_tables_appends(self) -> None:
        updated = codex._set_top_level_value('model = "x"\n', "/managed.md")
        self.assertEqual(codex._read_top_level_value(updated), "/managed.md")


class CodexAdapterTests(unittest.TestCase):
    """Full detect -> probe -> repair -> verify -> revert cycle on temp files."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.config = self.root / "config.toml"
        self.config.write_text('model = "gpt-6-astra"\n\n[features]\njs_repl = false\n')
        # A stand-in "binary" that only needs to contain the config key, which is
        # exactly what the real compatibility anchor looks for.
        self.binary = self.root / "codex-stub"
        self.binary.write_bytes(b"...." + codex.CONFIG_KEY.encode() + b"....")
        self.prompt = self.root / "system-role.md"
        self.prompt.write_text("# managed prompt\n")
        self.managed = self.root / "managed"
        self.platform_config = {
            "enabled": True,
            "config_file": str(self.config),
            "binary": str(self.binary),
            "prompt_file": str(self.prompt),
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _ctx(self, dry_run: bool) -> core.PlatformContext:
        return make_ctx(self.managed, dry_run=dry_run, config={"platforms": {"codex": dict(self.platform_config)}})

    def test_needs_repair_when_key_missing(self) -> None:
        ctx = self._ctx(dry_run=True)
        detection = codex.detect(ctx)
        self.assertTrue(detection["installed"])
        probe = codex.probe(ctx, detection)
        self.assertEqual(probe["state"], core.STATE_NEEDS_REPAIR)
        self.assertTrue(probe["anchors_ok"])
        self.assertFalse(probe["injected"])

    def test_dry_run_writes_nothing(self) -> None:
        ctx = self._ctx(dry_run=True)
        before = self.config.read_text()
        outcome = codex.repair(ctx, codex.probe(ctx, codex.detect(ctx)))
        self.assertTrue(outcome["ok"])
        self.assertEqual(self.config.read_text(), before)
        self.assertFalse((self.managed / "codex").exists())

    def test_repair_then_verify_then_revert(self) -> None:
        ctx = self._ctx(dry_run=False)
        outcome = codex.repair(ctx, codex.probe(ctx, codex.detect(ctx)))
        self.assertTrue(outcome["ok"])
        managed_prompt = ctx.prompt_file("codex")
        self.assertTrue(managed_prompt.is_file())
        self.assertEqual(codex._read_top_level_value(self.config.read_text()), str(managed_prompt))
        self.assertTrue(list(self.config.parent.glob("config.toml.bak-keysmith-autorepair-*")))

        probe = codex.probe(ctx, codex.detect(ctx))
        self.assertEqual(probe["state"], core.STATE_OK)
        self.assertTrue(probe["injected"])
        self.assertTrue(codex.verify(ctx)["ok"])

        codex.revert(ctx)
        self.assertIsNone(codex._read_top_level_value(self.config.read_text()))
        self.assertIn("[features]", self.config.read_text())

    def test_unknown_key_in_binary_reports_unsupported(self) -> None:
        self.binary.write_bytes(b"a binary that never mentions the key")
        ctx = self._ctx(dry_run=False)
        probe = codex.probe(ctx, codex.detect(ctx))
        self.assertEqual(probe["state"], core.STATE_UNSUPPORTED)
        self.assertFalse(probe["anchors_ok"])


class PlatformStatusTests(unittest.TestCase):
    """State transitions and the repair gate inside core.platform_status."""

    class Stub:
        PLATFORM_ID = "stub"
        PLATFORM_LABEL = "Stub"

        def __init__(self, state: str) -> None:
            self.state = state
            self.calls = {"detect": 0, "probe": 0, "repair": 0, "verify": 0}

        def detect(self, ctx):
            self.calls["detect"] += 1
            return {"installed": True, "app_version": "1.0", "runtime_path": "/tmp/x"}

        def probe(self, ctx, detection):
            self.calls["probe"] += 1
            return {
                "state": self.state,
                "injected": self.state == core.STATE_OK,
                "anchors_ok": self.state != core.STATE_UNSUPPORTED,
                "runtime_sha256": "sha-1",
                "app_version": "1.0",
                "baseline": {"version": "1.0", "runtime_sha256": "sha-1"},
            }

        def repair(self, ctx, probe):
            self.calls["repair"] += 1
            return {"ok": True, "actions": [], "detail": "repaired"}

        def verify(self, ctx):
            self.calls["verify"] += 1
            return {"ok": True, "actions": [], "detail": "verified"}

    def _ctx(self, tmp: Path, dry_run: bool) -> core.PlatformContext:
        return make_ctx(tmp, dry_run=dry_run, config={"platforms": {"stub": {"enabled": True, "auto_repair": True}}})

    def test_disabled_platform_is_never_probed_or_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_ctx(Path(tmp), dry_run=False, config={"platforms": {"stub": {"enabled": False}}})
            stub = self.Stub(core.STATE_NEEDS_REPAIR)
            result = core.platform_status(stub, ctx, repair=True)
            self.assertEqual(result["state"], core.STATE_DISABLED)
            self.assertEqual(stub.calls["probe"], 0)
            self.assertEqual(stub.calls["repair"], 0)

    def test_needs_repair_is_repaired_and_baselined(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(Path(tmp), dry_run=False)
            stub = self.Stub(core.STATE_NEEDS_REPAIR)
            result = core.platform_status(stub, ctx, repair=True)
            self.assertEqual(stub.calls["repair"], 1)
            self.assertEqual(stub.calls["verify"], 1)
            self.assertTrue(result["repaired"])
            self.assertEqual(result["state"], core.STATE_OK)
            self.assertEqual(ctx.platform_state("stub")["baseline"]["version"], "1.0")

    def test_unsupported_is_never_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(Path(tmp), dry_run=False)
            stub = self.Stub(core.STATE_UNSUPPORTED)
            result = core.platform_status(stub, ctx, repair=True)
            self.assertEqual(stub.calls["repair"], 0)
            self.assertEqual(result["state"], core.STATE_UNSUPPORTED)

    def test_dry_run_reports_a_plan_not_a_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(Path(tmp), dry_run=True)
            stub = self.Stub(core.STATE_NEEDS_REPAIR)
            result = core.platform_status(stub, ctx, repair=True)
            self.assertFalse(result["repaired"])
            self.assertTrue(result["planned"])
            self.assertEqual(result["state"], core.STATE_NEEDS_REPAIR)
            self.assertEqual(ctx.platform_state("stub").get("baseline"), None)

    def test_injected_state_records_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(Path(tmp), dry_run=False)
            stub = self.Stub(core.STATE_OK)
            core.platform_status(stub, ctx, repair=False)
            self.assertEqual(ctx.platform_state("stub")["baseline"]["runtime_sha256"], "sha-1")

    def test_unattended_policy_blocks_first_injection(self) -> None:
        """无人值守：没有基线就不许写，并在 detail 里说明该怎么办。"""
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(Path(tmp), dry_run=False)
            stub = self.Stub(core.STATE_NEEDS_REPAIR)
            result = core.platform_status(stub, ctx, repair=True, baseline_policy="require")
            self.assertEqual(stub.calls["repair"], 0)
            self.assertEqual(result["state"], core.STATE_NEEDS_REPAIR)
            self.assertIn("从未确认注入过", result["detail"])
            self.assertIn("repair --platform", result["detail"])

    def test_explicit_policy_injects_without_baseline(self) -> None:
        """人的显式动作（repair / 勾选启用）：没有基线也要真的注入。"""
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(Path(tmp), dry_run=False)
            stub = self.Stub(core.STATE_NEEDS_REPAIR)
            result = core.platform_status(stub, ctx, repair=True, baseline_policy="any")
            self.assertEqual(stub.calls["repair"], 1)
            self.assertTrue(result["repaired"])
            self.assertEqual(result["state"], core.STATE_OK)


class CliGateTests(unittest.TestCase):
    """The unattended path through the real CLI and the real drop-in loader."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.managed = Path(self._tmp.name)
        (self.managed / "platforms").mkdir(parents=True, exist_ok=True)
        (self.managed / "platforms" / "stub_platform.py").write_text(STUB_PLATFORM, encoding="utf-8")
        self.calls_path = self.managed / "calls.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, *extra: str) -> dict:
        completed = subprocess.run(
            [sys.executable, str(CLI), *extra, "--json", "--managed-dir", str(self.managed)],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        self.assertTrue(completed.stdout.strip(), completed.stderr)
        return json.loads(completed.stdout)

    def _write_config(self, auto_repair: bool) -> None:
        (self.managed / "config.json").write_text(
            json.dumps(
                {
                    "schema": core.CONFIG_SCHEMA,
                    "platforms": {"stub": {"enabled": True, "auto_repair": auto_repair}},
                }
            )
        )

    def _calls(self) -> dict:
        return json.loads(self.calls_path.read_text()) if self.calls_path.exists() else {}

    @staticmethod
    def _row(payload: dict, platform_id: str = "stub") -> dict:
        for row in payload["platforms"]:
            if row["id"] == platform_id:
                return row
        raise AssertionError(f"platform {platform_id} missing from {payload['platforms']}")

    def test_dropin_is_discovered(self) -> None:
        payload = self._run("platforms")
        ids = {row["id"] for row in payload["platforms"]}
        self.assertIn("stub", ids)

    def test_unattended_run_does_not_repair_without_a_baseline(self) -> None:
        self._write_config(auto_repair=True)
        payload = self._run("check", "--auto")
        self.assertEqual(self._row(payload)["state"], core.STATE_NEEDS_REPAIR)
        self.assertEqual(self._calls().get("repair"), None)
        # A check still records what it saw.
        state = json.loads((self.managed / "state.json").read_text())
        self.assertEqual(state["platforms"]["stub"]["last_state"], core.STATE_NEEDS_REPAIR)

    def test_unattended_run_repairs_once_a_baseline_exists(self) -> None:
        self._write_config(auto_repair=True)
        (self.managed / "state.json").write_text(
            json.dumps(
                {
                    "schema": core.STATE_SCHEMA,
                    "platforms": {"stub": {"baseline": {"version": "0.9"}}},
                }
            )
        )
        payload = self._run("check", "--auto")
        self.assertEqual(self._calls().get("repair"), 1)
        self.assertEqual(self._row(payload)["state"], core.STATE_OK)

    def test_auto_repair_disabled_never_repairs(self) -> None:
        self._write_config(auto_repair=False)
        (self.managed / "state.json").write_text(
            json.dumps({"schema": core.STATE_SCHEMA, "platforms": {"stub": {"baseline": {"version": "0.9"}}}})
        )
        payload = self._run("check", "--auto")
        self.assertEqual(self._calls().get("repair"), None)
        self.assertEqual(self._row(payload)["state"], core.STATE_NEEDS_REPAIR)

    def test_plain_repair_plans_unless_yes(self) -> None:
        self._write_config(auto_repair=False)
        payload = self._run("repair", "--platform", "stub")
        self.assertEqual(payload["mode"], "preview")
        self.assertEqual(self._calls().get("repair"), 1)
        self.assertEqual(self._calls().get("verify"), None)

    def test_repair_with_yes_verifies(self) -> None:
        self._write_config(auto_repair=False)
        payload = self._run("repair", "--platform", "stub", "--yes")
        self.assertEqual(payload["mode"], "execute")
        self.assertEqual(self._calls().get("repair"), 1)
        self.assertEqual(self._calls().get("verify"), 1)
        self.assertEqual(self._row(payload)["state"], core.STATE_OK)

    def test_enable_injects_immediately(self) -> None:
        """勾选「启用」是人的明确动作：不该停在"开了但没生效"。"""
        self._write_config(auto_repair=False)
        payload = self._run("enable", "--platform", "stub")
        self.assertEqual(self._calls().get("repair"), 1)
        self.assertEqual(self._row(payload)["state"], core.STATE_OK)

    def test_disable_only_flips_the_switch(self) -> None:
        self._write_config(auto_repair=False)
        payload = self._run("disable", "--platform", "stub")
        self.assertEqual(self._calls().get("repair"), None)
        self.assertEqual(self._row(payload)["state"], core.STATE_DISABLED)


class AgentCommandTests(unittest.TestCase):
    """LaunchAgent 只做检测：macOS 应用管理不允许 launchd 写别的 App 包。"""

    @staticmethod
    def _cli():
        spec = importlib.util.spec_from_file_location("autorepair_cli_for_test", CLI)
        module = importlib.util.module_from_spec(spec)
        sys.modules["autorepair_cli_for_test"] = module
        spec.loader.exec_module(module)
        return module

    def test_detect_only_is_the_default_command(self) -> None:
        cli = self._cli()
        ctx = make_ctx(Path("/tmp/agent"), dry_run=True, config={"platforms": {}})
        argv = cli.render_agent_command(ctx)
        self.assertIn("check", argv)
        self.assertNotIn("--auto", argv)
        self.assertIn("--json", argv)

    def test_auto_repair_can_be_opted_in(self) -> None:
        cli = self._cli()
        ctx = make_ctx(
            Path("/tmp/agent"), dry_run=True, config={"platforms": {}, "agent": {"detect_only": False}}
        )
        self.assertIn("--auto", cli.render_agent_command(ctx))


class StatusSnapshotTests(unittest.TestCase):
    """GUI 只读快照：字段齐全，且不因为缺字段就崩。"""

    def test_snapshot_carries_enabled_and_auto_repair(self) -> None:
        snapshot = core.status_snapshot(
            [
                {
                    "id": "zcode",
                    "label": "ZCode",
                    "enabled": True,
                    "auto_repair": True,
                    "state": core.STATE_OK,
                    "state_label": core.STATE_LABELS[core.STATE_OK],
                    "detail": "注入有效",
                }
            ],
            "check",
        )
        self.assertEqual(snapshot["schema"], core.STATUS_SCHEMA)
        self.assertEqual(snapshot["platforms"][0]["auto_repair"], True)
        self.assertEqual(snapshot["exit_status"], 0)
        self.assertEqual(snapshot["summary"], "ZCode：已注入")

    def test_snapshot_flags_attention(self) -> None:
        snapshot = core.status_snapshot(
            [
                {
                    "id": "zcode",
                    "label": "ZCode",
                    "enabled": True,
                    "state": core.STATE_NEEDS_REPAIR,
                    "state_label": core.STATE_LABELS[core.STATE_NEEDS_REPAIR],
                }
            ],
            "check",
        )
        self.assertEqual(snapshot["exit_status"], 1)


if __name__ == "__main__":
    unittest.main()
