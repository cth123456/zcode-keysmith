#!/usr/bin/env python3
"""Codex platform adapter for keysmith-autorepair.

Codex has a first-class, file-based replacement for its built-in instructions:

    model_instructions_file = "/path/to/model-instructions.md"

(Codex's own config reference: "Replacement for built-in instructions instead of
AGENTS.md."  The `instructions` key is documented as reserved for future use, so
this adapter does not touch it.)

Two consequences shape this adapter:

* The key is a top-level config key, so a Codex update does not remove it by
  itself — but the tools that rewrite ~/.codex/config.toml (CC Switch, manual
  edits, a reset profile) do.  Drift is therefore detected by reading the config,
  not by watching the app bundle.
* Replacing built-in instructions is invasive: Codex loses its own base prompt.
  The platform is opt-in (enabled: false in the default config) and this adapter
  never invents a prompt — it reuses one that already exists on the machine.

Compatibility anchor: before writing, the adapter checks that the resolved Codex
binary still knows the `model_instructions_file` key at all.  If a future Codex
renames or drops it, the adapter reports "需要适配新版本" and writes nothing.
"""

from __future__ import annotations

import hashlib
import json
import plistlib
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from autorepair_core import (
    AutorepairError,
    STATE_NEEDS_REPAIR,
    STATE_NOT_INSTALLED,
    STATE_OK,
    STATE_UNSUPPORTED,
    PlatformContext,
    expand_path,
    file_sha256,
    now_iso,
    write_text_atomic,
)

PLATFORM_ID = "codex"
PLATFORM_LABEL = "Codex"

CONFIG_KEY = "model_instructions_file"
CONFIG_RELATIVE = Path(".codex/config.toml")
KEYSMITH_MANAGED_PROMPT = Path.home() / ".zcode-keysmith" / "system-role.md"
BINARY_CANDIDATES = (
    "/Applications/ChatGPT.app/Contents/Resources/codex",
    "/opt/homebrew/bin/codex",
    "/usr/local/bin/codex",
)
NPM_PACKAGE_JSON = Path("/opt/homebrew/lib/node_modules/@openai/codex/package.json")
_TOP_LEVEL_KEY_RE = re.compile(r"^\s*" + CONFIG_KEY + r"\s*=\s*(?P<value>.*?)\s*$")
_TABLE_RE = re.compile(r"^\s*\[")


def _config_path(ctx: PlatformContext) -> Path:
    entry = ctx.platform_config(PLATFORM_ID)
    override = str(entry.get("config_file") or "").strip()
    if override:
        return expand_path(override)
    return Path.home() / CONFIG_RELATIVE


def _binary(ctx: PlatformContext) -> Path | None:
    entry = ctx.platform_config(PLATFORM_ID)
    override = str(entry.get("binary") or "").strip()
    candidates = [expand_path(override)] if override else []
    candidates.extend(Path(item) for item in BINARY_CANDIDATES)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _read_version_from_package_json(binary: Path | None) -> str:
    """Read the CLI version without starting a process."""
    candidates: list[Path] = []
    if binary is not None:
        # .../@openai/codex/bin/codex.js -> .../@openai/codex/package.json
        for parent in binary.resolve().parents:
            candidates.append(parent / "package.json")
            if parent.name == "codex":
                break
    candidates.append(NPM_PACKAGE_JSON)
    for candidate in candidates:
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        version = str(payload.get("version") or "").strip()
        name = str(payload.get("name") or "")
        if version and ("codex" in name or candidate == NPM_PACKAGE_JSON):
            return version
    return ""


def _read_version_from_app(binary: Path | None) -> str:
    if binary is None:
        return ""
    for parent in binary.resolve().parents:
        if parent.name.endswith(".app"):
            info = parent / "Contents" / "Info.plist"
            try:
                with info.open("rb") as handle:
                    payload = plistlib.load(handle)
            except (OSError, plistlib.InvalidFileException):
                return ""
            version = str(payload.get("CFBundleShortVersionString") or "")
            build = str(payload.get("CFBundleVersion") or "")
            return f"{version} ({build})" if version and build else version or build
    return ""


def _binary_sha256(path: Path | None) -> str:
    if path is None:
        return ""
    return file_sha256(path) or ""


def _binary_knows_key(path: Path | None) -> bool:
    """Stream the binary and look for the config key (the compatibility anchor)."""
    if path is None:
        return False
    needle = CONFIG_KEY.encode("utf-8")
    overlap = len(needle) - 1
    tail = b""
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(8 * 1024 * 1024)
                if not chunk:
                    return False
                haystack = tail + chunk
                if needle in haystack:
                    return True
                tail = haystack[-overlap:] if overlap else b""
    except OSError:
        return False


def _anchors_ok(ctx: PlatformContext, binary: Path | None) -> tuple[bool, str]:
    """Cached anchor check, keyed by the binary's identity on disk."""
    if binary is None:
        return False, ""
    try:
        stat = binary.stat()
    except OSError:
        return False, ""
    token = f"{stat.st_size}:{int(stat.st_mtime)}"
    state_entry = ctx.platform_state(PLATFORM_ID)
    cached = state_entry.get("anchor_cache") or {}
    if cached.get("token") == token and isinstance(cached.get("ok"), bool):
        return bool(cached["ok"]), str(cached.get("checked_at") or "")
    ok = _binary_knows_key(binary)
    if not ctx.dry_run:
        state_entry["anchor_cache"] = {"token": token, "ok": ok, "checked_at": now_iso()}
    return ok, now_iso()


def _read_top_level_value(text: str) -> str | None:
    """Return the top-level CONFIG_KEY value, ignoring copies inside [tables]."""
    for line in text.splitlines():
        if _TABLE_RE.match(line):
            break
        match = _TOP_LEVEL_KEY_RE.match(line)
        if match:
            raw = match.group("value").strip()
            if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
                try:
                    return str(json.loads(raw))
                except json.JSONDecodeError:
                    return raw[1:-1]
            if raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
                return raw[1:-1]
            return raw
    return None


def _set_top_level_value(text: str, value: str) -> str:
    """Set CONFIG_KEY at the top level, replacing or inserting one line only."""
    assignment = f"{CONFIG_KEY} = {json.dumps(value)}"
    lines = text.splitlines()
    out: list[str] = []
    in_table = False
    replaced = False
    for line in lines:
        if _TABLE_RE.match(line):
            if not replaced:
                # First table header: the top-level block ends here.
                out.append(assignment)
                replaced = True
            in_table = True
            out.append(line)
            continue
        if not in_table and not replaced and _TOP_LEVEL_KEY_RE.match(line):
            out.append(assignment)
            replaced = True
            continue
        out.append(line)
    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.append(assignment)
    return "\n".join(out) + "\n"


def _remove_top_level_value(text: str) -> str:
    """Remove only the top-level CONFIG_KEY; keys inside [tables] stay put."""
    lines = text.splitlines()
    out: list[str] = []
    in_table = False
    for line in lines:
        if _TABLE_RE.match(line):
            in_table = True
            out.append(line)
            continue
        if not in_table and _TOP_LEVEL_KEY_RE.match(line):
            continue
        out.append(line)
    return "\n".join(out) + "\n"


def _backup(path: Path) -> Path | None:
    if not path.is_file():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.bak-keysmith-autorepair-{stamp}")
    shutil.copy2(path, target)
    return target


def detect(ctx: PlatformContext) -> dict[str, Any]:
    binary = _binary(ctx)
    config = _config_path(ctx)
    version = _read_version_from_package_json(binary) or _read_version_from_app(binary)
    installed = binary is not None or config.is_file()
    detail = ""
    if not installed:
        detail = "未找到 Codex CLI 或 ~/.codex/config.toml"
    return {
        "installed": installed,
        "app_version": version,
        "runtime_path": str(binary) if binary else str(config),
        "binary_path": str(binary) if binary else "",
        "config_path": str(config),
        "detail": detail,
    }


def probe(ctx: PlatformContext, detection: dict[str, Any]) -> dict[str, Any]:
    config = Path(str(detection.get("config_path") or _config_path(ctx)))
    binary = Path(str(detection.get("binary_path"))) if detection.get("binary_path") else None
    anchors_ok, checked_at = _anchors_ok(ctx, binary)
    text = ""
    if config.is_file():
        try:
            text = config.read_text(encoding="utf-8")
        except OSError as error:
            raise AutorepairError(f"无法读取 Codex 配置：{error}") from error
    current = _read_top_level_value(text)
    target = ctx.prompt_file(PLATFORM_ID)
    prompt_exists = target.is_file()
    injected = current is not None and current == str(target) and prompt_exists
    version = str(detection.get("app_version") or "")
    if injected:
        state = STATE_OK
        detail = "Codex 已指向受管指令文件"
    elif not anchors_ok:
        state = STATE_UNSUPPORTED
        detail = (
            f"当前 Codex 二进制里找不到 {CONFIG_KEY} 这个键；可能已改名或被移除，"
            "已停止写入，需要适配新版本"
        )
    elif config.exists() is False and not detection.get("installed"):
        state = STATE_NOT_INSTALLED
        detail = "未找到 ~/.codex/config.toml"
    else:
        state = STATE_NEEDS_REPAIR
        if current is None:
            detail = f"config.toml 里没有 {CONFIG_KEY}（可能被 CC Switch 或手工编辑重置），可自动补回"
        elif current != str(target):
            detail = f"{CONFIG_KEY} 指向 {current}，与受管路径不一致，可自动纠正"
        else:
            detail = f"{CONFIG_KEY} 已指向受管路径，但指令文件缺失，可自动补回"
    if injected:
        detail = f"{detail}；Codex 需重新启动后生效"
    return {
        "state": state,
        "injected": injected,
        "anchors_ok": anchors_ok,
        "runtime_sha256": _binary_sha256(binary) if not injected else "",
        "app_version": version,
        "config_path": str(config),
        "binary_path": str(binary) if binary else "",
        "current_value": current or "",
        "expected_value": str(target),
        "prompt_exists": prompt_exists,
        "anchor_checked_at": checked_at,
        "detail": detail,
        "baseline": {"version": version, "config_key_value": str(target)} if injected else None,
    }


def _source_prompt(ctx: PlatformContext) -> Path:
    """Pick an existing prompt; never invent one for Codex."""
    entry = ctx.platform_config(PLATFORM_ID)
    explicit = str(entry.get("prompt_file") or "").strip()
    candidates = []
    if explicit:
        candidates.append(expand_path(explicit))
    candidates.append(ctx.managed_dir / "system-role.md")
    candidates.append(KEYSMITH_MANAGED_PROMPT)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise AutorepairError(
        "没有可用的指令文件：Codex 会整体替换内置指令，本工具不会自动生成。"
        "请先准备一份（例如拷到 ~/.keysmith-autorepair/system-role.md），"
        "或在 config.json 里为 codex 设置 prompt_file"
    )


def repair(ctx: PlatformContext, probe_data: dict[str, Any]) -> dict[str, Any]:
    config = Path(str(probe_data.get("config_path") or _config_path(ctx)))
    target = ctx.prompt_file(PLATFORM_ID)
    source = _source_prompt(ctx)
    actions: list[dict[str, Any]] = []
    if source.resolve() != target.resolve():
        actions.append(
            {
                "action": "plan" if ctx.dry_run else "write",
                "path": str(target),
                "detail": f"复制指令文件（来自 {source}）",
            }
        )
        if not ctx.dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(target, source.read_text(encoding="utf-8"), mode=0o600)
    text = ""
    if config.is_file():
        try:
            text = config.read_text(encoding="utf-8")
        except OSError as error:
            raise AutorepairError(f"无法读取 Codex 配置：{error}") from error
    updated = _set_top_level_value(text, str(target))
    actions.append(
        {
            "action": "plan" if ctx.dry_run else "write",
            "path": str(config),
            "detail": f"写入顶级键 {CONFIG_KEY} = {target}",
        }
    )
    if not ctx.dry_run:
        backup = _backup(config)
        if backup is not None:
            actions.append({"action": "backup", "path": str(backup), "detail": "写前备份"})
        write_text_atomic(config, updated)
    detail = (
        f"计划：把 {CONFIG_KEY} 指向 {target}（未写入）"
        if ctx.dry_run
        else f"已把 {CONFIG_KEY} 指向 {target}；Codex 需重新启动后生效"
    )
    return {"ok": True, "actions": actions, "detail": detail}


def verify(ctx: PlatformContext) -> dict[str, Any]:
    detection = detect(ctx)
    if not detection.get("installed"):
        return {"ok": False, "actions": [], "detail": "Codex 未安装"}
    probe_data = probe(ctx, detection)
    ok = bool(probe_data.get("injected"))
    if ok:
        detail = f"配置就绪：{CONFIG_KEY} → {probe_data.get('expected_value')}"
        if not probe_data.get("anchors_ok"):
            return {"ok": False, "actions": [], "detail": "二进制里已找不到该配置键，需要适配新版本"}
    else:
        detail = f"配置未就绪：{probe_data.get('detail')}"
    return {"ok": ok, "actions": [], "detail": detail}


def revert(ctx: PlatformContext) -> dict[str, Any]:
    config = _config_path(ctx)
    if not config.is_file():
        return {"ok": True, "actions": [], "detail": "没有 Codex 配置需要处理"}
    text = config.read_text(encoding="utf-8")
    updated = _remove_top_level_value(text)
    actions = [
        {
            "action": "plan" if ctx.dry_run else "remove",
            "path": str(config),
            "detail": f"移除顶级键 {CONFIG_KEY}",
        }
    ]
    if not ctx.dry_run:
        backup = _backup(config)
        if backup is not None:
            actions.append({"action": "backup", "path": str(backup), "detail": "写前备份"})
        write_text_atomic(config, updated)
    return {"ok": True, "actions": actions, "detail": "已移除受管的 Codex 指令指向"}
