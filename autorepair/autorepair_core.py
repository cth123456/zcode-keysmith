#!/usr/bin/env python3
"""Shared runtime for keysmith-autorepair: paths, state, locks, platform registry.

The Auto Repair layer answers one question: after an AI coding tool updates
itself, is the managed system-prompt injection still in place, and can we put it
back without guessing?  Everything here is stdlib-only and additive to
zcode-keysmith: the upstream script is treated as a black box that is invoked
through its own CLI contract (zcode-keysmith/v1).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

JSON_SCHEMA = "keysmith-autorepair/v1"
CONFIG_SCHEMA = "keysmith-autorepair/config/v1"
STATE_SCHEMA = "keysmith-autorepair/state/v1"
STATUS_SCHEMA = "keysmith-autorepair/status/v1"
STATUS_FILE_NAME = "status.json"
STATUS_FIELDS = (
    "id",
    "label",
    "enabled",
    "auto_repair",
    "installed",
    "state",
    "state_label",
    "injected",
    "anchors_ok",
    "baseline_ready",
    "app_version",
    "detail",
    "runtime_path",
    "last_check",
    "last_repair",
    "repaired",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANAGED_DIR = Path.home() / ".keysmith-autorepair"
CONFIG_FILE_NAME = "config.json"
STATE_FILE_NAME = "state.json"
LOCK_FILE_NAME = ".operation.lock"
DEFAULT_PROMPT_FILE_NAME = "system-role.md"
USER_PLATFORM_DIR_NAME = "platforms"

# Platform states reported to every consumer (Bar Control, ReleaseManager, CLI).
STATE_OK = "injected"
STATE_DISABLED = "disabled"
STATE_NOT_INSTALLED = "not_installed"
STATE_NEEDS_REPAIR = "needs_repair"
STATE_UNSUPPORTED = "unsupported"
STATE_DRIFTED = "drifted"
STATE_ERROR = "error"

STATE_LABELS = {
    STATE_OK: "已注入",
    STATE_DISABLED: "未启用",
    STATE_NOT_INSTALLED: "未安装",
    STATE_NEEDS_REPAIR: "已失效，可修复",
    STATE_UNSUPPORTED: "需要适配新版本",
    STATE_DRIFTED: "已注入（基线已更新）",
    STATE_ERROR: "检查失败",
}


class AutorepairError(Exception):
    """User-facing error."""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def expand_path(value: str | os.PathLike[str]) -> Path:
    return Path(os.path.expanduser(str(value)))


def file_sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_text_atomic(path: Path, content: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as tmp:
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    if mode is not None:
        os.chmod(path, mode)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


@dataclass
class PlatformContext:
    """Everything a platform module needs, resolved once per run."""

    managed_dir: Path
    config: dict[str, Any]
    state: dict[str, Any]
    dry_run: bool = True
    verbose: bool = False
    config_path: Path | None = None
    state_path: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def platform_config(self, platform_id: str) -> dict[str, Any]:
        platforms = self.config.setdefault("platforms", {})
        entry = platforms.get(platform_id)
        if not isinstance(entry, dict):
            entry = {}
            platforms[platform_id] = entry
        return entry

    def platform_state(self, platform_id: str) -> dict[str, Any]:
        platforms = self.state.setdefault("platforms", {})
        entry = platforms.get(platform_id)
        if not isinstance(entry, dict):
            entry = {}
            platforms[platform_id] = entry
        return entry

    def prompt_file(self, platform_id: str) -> Path:
        """Managed prompt source for a platform.

        Priority: explicit config path, then the per-platform prompt file, then
        the platform's own already-installed prompt (so a repair never replaces a
        user's prompt with a shipped example).
        """
        entry = self.platform_config(platform_id)
        explicit = str(entry.get("prompt_file") or "").strip()
        if explicit:
            return expand_path(explicit)
        return self.managed_dir / platform_id / DEFAULT_PROMPT_FILE_NAME


def default_config(managed_dir: Path) -> dict[str, Any]:
    return {
        "schema": CONFIG_SCHEMA,
        "prompt_file": str(managed_dir / DEFAULT_PROMPT_FILE_NAME),
        # ZCode is the platform this repository already manages, and re-applying
        # its patch after an app update is the point of this layer.  Codex
        # replaces the whole built-in instruction block, so it stays opt-in and
        # manual.
        "platforms": {
            "zcode": {"enabled": True, "auto_repair": True},
            "codex": {"enabled": False, "auto_repair": False},
        },
        "agent": {
            "interval_seconds": 900,
            "watch_runtime": True,
            # macOS 的应用管理保护（TCC App Management）不允许 launchd 任务写别的
            # App 包，所以定时任务只做「检测 + 报告」；真正重打补丁交给有该权限的
            # GUI 上下文（Bar Control 破甲层 / 发布管理中心一键更新）。
            "detect_only": True,
        },
        "recent_limit": 20,
    }


def load_config(managed_dir: Path) -> tuple[dict[str, Any], Path]:
    path = managed_dir / CONFIG_FILE_NAME
    config = read_json(path)
    if config is None:
        config = default_config(managed_dir)
        return config, path
    defaults = default_config(managed_dir)
    merged = {**defaults, **config}
    platforms = dict(defaults["platforms"])
    stored = config.get("platforms")
    if isinstance(stored, dict):
        for key, value in stored.items():
            merged_entry = dict(platforms.get(key) or {})
            if isinstance(value, dict):
                merged_entry.update(value)
            else:
                merged_entry["enabled"] = bool(value)
            platforms[key] = merged_entry
    merged["platforms"] = platforms
    agent = dict(defaults["agent"])
    if isinstance(config.get("agent"), dict):
        agent.update(config["agent"])
    merged["agent"] = agent
    return merged, path


def load_state(managed_dir: Path) -> tuple[dict[str, Any], Path]:
    path = managed_dir / STATE_FILE_NAME
    state = read_json(path)
    if state is None or state.get("schema") != STATE_SCHEMA:
        state = {"schema": STATE_SCHEMA, "updated_at": now_iso(), "platforms": {}}
    if not isinstance(state.get("platforms"), dict):
        state["platforms"] = {}
    return state, path


@contextmanager
def operation_lock(managed_dir: Path) -> Iterator[None]:
    """Single-flight guard so launchd, Bar Control and the CLI never race."""
    import fcntl

    managed_dir.mkdir(parents=True, exist_ok=True)
    lock_path = managed_dir / LOCK_FILE_NAME
    handle = lock_path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _load_module_from_path(module_name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise AutorepairError(f"无法加载平台模块：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _builtin_platform_paths() -> list[Path]:
    here = Path(__file__).resolve().parent
    return sorted(here.glob("autorepair_platform_*.py"))


def user_platform_dir(managed_dir: Path) -> Path:
    return managed_dir / USER_PLATFORM_DIR_NAME


def discover_platforms(managed_dir: Path) -> dict[str, Any]:
    """Built-in adapters plus drop-ins from ~/.keysmith-autorepair/platforms/.

    A drop-in is any .py file exposing PLATFORM_ID / PLATFORM_LABEL and the
    detect/probe/repair/verify hooks.  That is the documented extension point for
    more AI coding tools.
    """
    modules: dict[str, Any] = {}
    for path in _builtin_platform_paths():
        module = _load_module_from_path(f"autorepair_builtin_{path.stem}", path)
        platform_id = getattr(module, "PLATFORM_ID", None)
        if platform_id:
            modules[str(platform_id)] = module
    dropin_dir = user_platform_dir(managed_dir)
    if dropin_dir.is_dir():
        for path in sorted(dropin_dir.glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                module = _load_module_from_path(f"autorepair_dropin_{path.stem}", path)
            except Exception as error:  # a broken drop-in must not break the CLI
                modules[f"?{path.stem}"] = error
                continue
            platform_id = getattr(module, "PLATFORM_ID", None)
            if platform_id:
                modules[str(platform_id)] = module
    return modules


def resolve_module(modules: dict[str, Any], platform_id: str) -> Any:
    module = modules.get(platform_id)
    if module is None:
        known = ", ".join(sorted(k for k in modules if not k.startswith("?"))) or "（无）"
        raise AutorepairError(f"未知平台：{platform_id}（可用：{known}）")
    if isinstance(module, Exception):
        raise AutorepairError(f"平台模块加载失败：{platform_id}：{module}")
    return module


def platform_status(
    module: Any,
    ctx: PlatformContext,
    *,
    repair: bool = False,
) -> dict[str, Any]:
    """Run one platform through detect -> probe -> (repair) -> verify."""
    platform_id = str(getattr(module, "PLATFORM_ID"))
    label = str(getattr(module, "PLATFORM_LABEL", platform_id))
    entry = ctx.platform_config(platform_id)
    state_entry = ctx.platform_state(platform_id)
    enabled = bool(entry.get("enabled"))
    result: dict[str, Any] = {
        "id": platform_id,
        "label": label,
        "enabled": enabled,
        "auto_repair": bool(entry.get("auto_repair")),
        "state": STATE_DISABLED if not enabled else STATE_ERROR,
        "state_label": STATE_LABELS[STATE_DISABLED if not enabled else STATE_ERROR],
        "injected": False,
        "anchors_ok": False,
        "baseline_ready": bool(state_entry.get("baseline")),
        "installed": False,
        "app_version": state_entry.get("last_version") or "",
        "runtime_path": "",
        "runtime_sha256": "",
        "detail": "",
        "actions": [],
        "planned": False,
        "warnings": [],
        "last_check": state_entry.get("last_check"),
        "last_repair": state_entry.get("last_repair"),
        "repaired": False,
    }
    try:
        detection = module.detect(ctx) or {}
    except Exception as error:
        result["detail"] = f"探测失败：{error}"
        return result
    result["installed"] = bool(detection.get("installed"))
    result["app_version"] = str(detection.get("app_version") or result["app_version"])
    result["runtime_path"] = str(detection.get("runtime_path") or "")
    if detection.get("detail"):
        result["detail"] = str(detection["detail"])
    if not enabled:
        result["state"] = STATE_DISABLED
        result["state_label"] = STATE_LABELS[STATE_DISABLED]
        return result
    if not result["installed"]:
        result["state"] = STATE_NOT_INSTALLED
        result["state_label"] = STATE_LABELS[STATE_NOT_INSTALLED]
        return result
    try:
        probe = module.probe(ctx, detection) or {}
    except Exception as error:
        result["state"] = STATE_ERROR
        result["state_label"] = STATE_LABELS[STATE_ERROR]
        result["detail"] = f"状态检查失败：{error}"
        return result
    result.update(
        {
            "injected": bool(probe.get("injected")),
            "anchors_ok": bool(probe.get("anchors_ok")),
            "runtime_sha256": str(probe.get("runtime_sha256") or ""),
            "detail": str(probe.get("detail") or result["detail"]),
            "state": str(probe.get("state") or STATE_ERROR),
        }
    )
    result["state_label"] = STATE_LABELS.get(result["state"], result["state"])
    baseline = state_entry.get("baseline") or {}
    # Repair only when the probe says the layout is recognised.  STATE_UNSUPPORTED
    # means the anchors moved and we must stop rather than guess.
    if repair and result["state"] == STATE_NEEDS_REPAIR:
        actions, ok, detail, warnings = _run_repair(module, ctx, probe)
        result["actions"] = actions
        result["warnings"].extend(warnings)
        # A dry run produced a plan, not a repaired machine.
        result["repaired"] = ok and not ctx.dry_run
        result["planned"] = ok and ctx.dry_run
        if ok and not ctx.dry_run:
            result["state"] = STATE_OK
            result["state_label"] = STATE_LABELS[STATE_OK]
            result["injected"] = True
            detail = detail or "已重新注入"
        result["detail"] = detail or result["detail"]
    _record(ctx, platform_id, result, probe, baseline)
    seen = result["state"]
    if result["repaired"]:
        seen = STATE_OK
    state_entry["last_check"] = now_iso()
    if result["repaired"]:
        state_entry["last_repair"] = state_entry["last_check"]
    state_entry["last_state"] = seen
    state_entry["last_version"] = result["app_version"]
    if result["runtime_sha256"]:
        state_entry["last_runtime_sha256"] = result["runtime_sha256"]
    result["last_check"] = state_entry["last_check"]
    result["last_repair"] = state_entry.get("last_repair")
    return result


def _run_repair(
    module: Any, ctx: PlatformContext, probe: dict[str, Any]
) -> tuple[list[dict[str, Any]], bool, str, list[str]]:
    warnings: list[str] = []
    try:
        outcome = module.repair(ctx, probe) or {}
    except Exception as error:
        return [], False, f"修复失败：{error}", warnings
    actions = list(outcome.get("actions") or [])
    ok = bool(outcome.get("ok"))
    detail = str(outcome.get("detail") or "")
    warnings.extend(str(item) for item in (outcome.get("warnings") or []))
    if ok and not ctx.dry_run:
        try:
            verification = module.verify(ctx) or {}
        except Exception as error:
            return actions, False, f"修复后验证失败：{error}", warnings
        actions.extend(verification.get("actions") or [])
        warnings.extend(str(item) for item in (verification.get("warnings") or []))
        if not verification.get("ok", True):
            return actions, False, str(verification.get("detail") or "修复后验证未通过"), warnings
        if verification.get("detail"):
            detail = f"{detail}；{verification['detail']}" if detail else str(verification["detail"])
    return actions, ok, detail, warnings


def _record(
    ctx: PlatformContext,
    platform_id: str,
    result: dict[str, Any],
    probe: dict[str, Any],
    baseline: dict[str, Any],
) -> None:
    """Advance the baseline only over a state we actually observed as injected."""
    if ctx.dry_run:
        return
    state_entry = ctx.platform_state(platform_id)
    version = str(probe.get("app_version") or result.get("app_version") or "")
    runtime_sha = str(probe.get("runtime_sha256") or "")
    injected_now = bool(result.get("repaired")) or (
        result["state"] in (STATE_OK, STATE_DRIFTED) and bool(probe.get("injected"))
    )
    if not injected_now:
        baseline_update = probe.get("baseline")
        if isinstance(baseline_update, dict) and baseline_update:
            state_entry["baseline"] = baseline_update
        return
    new_baseline = dict(baseline)
    if version:
        new_baseline["version"] = version
    if runtime_sha:
        new_baseline["runtime_sha256"] = runtime_sha
    extra = probe.get("baseline")
    if isinstance(extra, dict):
        new_baseline.update(extra)
    new_baseline["seen_at"] = now_iso()
    state_entry["baseline"] = new_baseline


def build_report(
    operation: str,
    mode: str,
    platforms: list[dict[str, Any]],
    *,
    ok: bool = True,
    warnings: list[str] | None = None,
    blockers: list[str] | None = None,
    error: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The stable JSON contract shared with Bar Control and ReleaseManager."""
    payload: dict[str, Any] = {
        "schema": JSON_SCHEMA,
        "operation": operation,
        "mode": mode,
        "ok": ok,
        "platforms": platforms,
        "summary": summarize(platforms),
        "warnings": list(warnings or []),
        "blockers": list(blockers or []),
        "generated_at": now_iso(),
        "exit_status": 0 if ok else 1,
        "error": error,
    }
    if extra:
        payload.update(extra)
    return payload


def summarize(platforms: list[dict[str, Any]]) -> str:
    if not platforms:
        return "没有启用任何平台"
    parts = []
    for item in platforms:
        parts.append(f"{item['label']}：{item.get('state_label') or item.get('state')}")
    return "；".join(parts)


def exit_code_for(platforms: list[dict[str, Any]]) -> int:
    states = {item.get("state") for item in platforms}
    if states & {STATE_UNSUPPORTED, STATE_ERROR, STATE_NEEDS_REPAIR}:
        return 1
    return 0


def status_snapshot(platforms: list[dict[str, Any]], operation: str) -> dict[str, Any]:
    """Compact read-only snapshot for GUI consumers (Bar Control).

    It is written next to state.json so a menu-bar app never has to spawn a
    process or re-hash a 14 MB runtime file just to draw a status chip.
    """
    return {
        "schema": STATUS_SCHEMA,
        "generated_at": now_iso(),
        "operation": operation,
        "summary": summarize(platforms),
        "exit_status": exit_code_for(platforms),
        "platforms": [
            {key: item.get(key) for key in STATUS_FIELDS}
            for item in platforms
            if isinstance(item, dict) and item.get("id")
        ],
    }


def write_status_snapshot(managed_dir: Path, platforms: list[dict[str, Any]], operation: str) -> None:
    try:
        write_json_atomic(managed_dir / STATUS_FILE_NAME, status_snapshot(platforms, operation))
    except OSError:
        # The GUI snapshot is a convenience; it must never fail a real check.
        pass
