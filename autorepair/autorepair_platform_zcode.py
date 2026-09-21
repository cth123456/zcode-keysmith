#!/usr/bin/env python3
"""ZCode platform adapter for keysmith-autorepair.

ZCode Desktop ships an Electron bundle and an in-app runtime at
Contents/Resources/glm/zcode.cjs.  The app's own auto-update replaces that whole
file, which silently removes a Keysmith runtime patch.  This adapter never
patches by itself: it reads state through zcode-keysmith's own JSON reports, and
when the anchor layout changed beyond recognition it stops instead of guessing.

Invocation safety: zcode-keysmith lives in the same repository and is called
in-process through its `main(argv)` entry point, so this adapter starts no child
process and never touches a shell.  Arguments are fixed flags plus paths taken
from the user's own config or from zcode-keysmith's JSON output.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import plistlib
import sys
from pathlib import Path
from typing import Any

from autorepair_core import (
    AutorepairError,
    REPO_ROOT,
    STATE_NEEDS_REPAIR,
    STATE_NOT_INSTALLED,
    STATE_OK,
    STATE_UNSUPPORTED,
    PlatformContext,
    expand_path,
    file_sha256,
)

PLATFORM_ID = "zcode"
PLATFORM_LABEL = "ZCode"

DEFAULT_ZCODE_APP = Path("/Applications/ZCode.app")
DEFAULT_MANAGED_DIR = Path.home() / ".zcode-keysmith"
RUNTIME_RELATIVE = Path("Contents/Resources/glm/zcode.cjs")

_UPSTREAM_MODULE: list[Any] = []


def _keysmith_script(ctx: PlatformContext) -> Path:
    entry = ctx.platform_config(PLATFORM_ID)
    override = str(entry.get("keysmith_script") or "").strip()
    script = expand_path(override) if override else REPO_ROOT / "zcode-keysmith.py"
    if not script.is_file():
        raise AutorepairError(f"找不到 zcode-keysmith.py：{script}")
    return script


def _load_keysmith(ctx: PlatformContext) -> Any:
    """Import the sibling zcode-keysmith.py once per process."""
    if _UPSTREAM_MODULE:
        return _UPSTREAM_MODULE[0]
    script = _keysmith_script(ctx)
    spec = importlib.util.spec_from_file_location("zcode_keysmith_upstream", script)
    if spec is None or spec.loader is None:
        raise AutorepairError(f"无法加载 zcode-keysmith：{script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["zcode_keysmith_upstream"] = module
    spec.loader.exec_module(module)
    _UPSTREAM_MODULE.append(module)
    return module


def _run_keysmith(ctx: PlatformContext, operation: str, *extra: str) -> dict[str, Any]:
    """Run one zcode-keysmith subcommand and return its parsed JSON report."""
    module = _load_keysmith(ctx)
    argv = [operation, *extra, "--json"]
    if ctx.verbose:
        print(f"[autorepair] zcode-keysmith {operation} {' '.join(extra)}", file=sys.stderr)
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured):
            module.main(argv)
    except SystemExit as error:
        code = error.code
        if isinstance(code, int) and code not in (0, None) and not captured.getvalue().strip():
            raise AutorepairError(f"zcode-keysmith {operation} 退出码 {code} 且没有输出") from error
    except Exception as error:
        raise AutorepairError(f"zcode-keysmith {operation} 失败：{error}") from error
    stdout = captured.getvalue().strip()
    if not stdout:
        raise AutorepairError(f"zcode-keysmith {operation} 没有输出 JSON")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise AutorepairError(f"zcode-keysmith {operation} 返回了非 JSON 输出：{error}") from error
    if not isinstance(payload, dict):
        raise AutorepairError(f"zcode-keysmith {operation} 返回了意外的 JSON 类型")
    return payload


def _managed_dir(ctx: PlatformContext) -> str:
    entry = ctx.platform_config(PLATFORM_ID)
    override = str(entry.get("managed_dir") or "").strip()
    return str(expand_path(override)) if override else str(DEFAULT_MANAGED_DIR)


def _zcode_app(ctx: PlatformContext) -> Path | None:
    entry = ctx.platform_config(PLATFORM_ID)
    override = str(entry.get("zcode_app") or "").strip()
    candidates = [expand_path(override)] if override else []
    candidates.append(DEFAULT_ZCODE_APP)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _app_version(app: Path | None) -> str:
    if app is None:
        return ""
    info = app / "Contents" / "Info.plist"
    try:
        with info.open("rb") as handle:
            payload = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException):
        return ""
    version = payload.get("CFBundleShortVersionString") or ""
    build = payload.get("CFBundleVersion") or ""
    if version and build:
        return f"{version} ({build})"
    return str(version or build or "")


def _common_args(ctx: PlatformContext, probe: dict[str, Any] | None = None) -> list[str]:
    managed = str((probe or {}).get("managed_dir") or _managed_dir(ctx))
    args = ["--managed-dir", managed]
    entry = ctx.platform_config(PLATFORM_ID)
    app = _zcode_app(ctx)
    if app is not None:
        args.extend(["--zcode-app", str(app)])
    runtime = str(entry.get("zcode_runtime") or "").strip()
    if runtime:
        args.extend(["--zcode-runtime", str(expand_path(runtime))])
    node_command = str(entry.get("node_command") or "").strip()
    if node_command:
        args.extend(["--node-command", str(expand_path(node_command))])
    return args


def _injected_from_doctor(doctor: dict[str, Any]) -> bool:
    runtime = doctor.get("runtime") or {}
    mode = str(runtime.get("injection_mode") or "")
    if mode == "runtime-patch":
        return bool(runtime.get("patched"))
    # Legacy wrapper injection: the app keeps its own command and a managed
    # wrapper is reached through persistent environment variables.
    managed = doctor.get("managed") or {}
    if not managed.get("wrapper_exists"):
        return False
    env = doctor.get("env") or {}
    expected = {
        "ZCODE_KEYSMITH_SYSTEM_FILE",
        "ZCODE_KEYSMITH_ORIGINAL",
        "ZCODE_KEYSMITH_NODE_COMMAND",
    }
    present = [entry for key, entry in env.items() if key in expected and isinstance(entry, dict)]
    if not present:
        return False
    return all(str(entry.get("persistent") or "") == "matches" for entry in present)


def detect(ctx: PlatformContext) -> dict[str, Any]:
    app = _zcode_app(ctx)
    if app is None:
        return {"installed": False, "app_version": "", "runtime_path": "", "detail": "未找到 ZCode.app"}
    runtime = app / RUNTIME_RELATIVE
    return {
        "installed": runtime.is_file(),
        "app_version": _app_version(app),
        "runtime_path": str(runtime),
        "app_path": str(app),
        "detail": "" if runtime.is_file() else f"未找到运行期文件：{runtime}",
    }


def probe(ctx: PlatformContext, detection: dict[str, Any]) -> dict[str, Any]:
    doctor = _run_keysmith(ctx, "doctor", *_common_args(ctx))
    runtime = doctor.get("runtime") or {}
    managed = doctor.get("managed") or {}
    runtime_path = Path(str(detection.get("runtime_path") or runtime.get("path") or ""))
    runtime_sha = file_sha256(runtime_path) if runtime_path.is_file() else ""
    injected = _injected_from_doctor(doctor)
    anchors_ok = bool(runtime.get("patchable"))
    state_entry = ctx.platform_state(PLATFORM_ID)
    baseline = state_entry.get("baseline") or {}
    version = str(detection.get("app_version") or "")
    baseline_changed = bool(baseline) and (
        baseline.get("version") != version
        or (runtime_sha and baseline.get("runtime_sha256") != runtime_sha)
    )
    if injected:
        detail = "ZCode 已更新，注入仍在；基线已追平" if baseline_changed else "注入有效"
        state = STATE_OK
    elif not runtime.get("exists"):
        state = STATE_NOT_INSTALLED
        detail = "ZCode 运行期文件不存在，无法打补丁"
    elif not anchors_ok:
        state = STATE_UNSUPPORTED
        detail = (
            "ZCode 运行期结构已变化，找不到已知锚点；已停止打补丁，需要适配新版本"
            f"（ZCode {version or '未知版本'}）"
        )
    else:
        state = STATE_NEEDS_REPAIR
        if baseline and baseline.get("version"):
            detail = f"ZCode {baseline.get('version')} → {version or '未知'} 更新后注入失效，可自动重新打补丁"
        else:
            detail = "注入未生效，但运行期结构可识别，可重新打补丁"
    if runtime.get("zcode_running"):
        detail = f"{detail}；ZCode 正在运行，修复后需完全退出并重新打开"
    return {
        "state": state,
        "injected": injected,
        "anchors_ok": anchors_ok,
        "runtime_sha256": runtime_sha,
        "app_version": version,
        "managed_dir": str(managed.get("dir") or ""),
        "system_file": str(managed.get("system_file") or ""),
        "injection_mode": str(runtime.get("injection_mode") or ""),
        "detail": detail,
        "baseline": {"version": version, "runtime_sha256": runtime_sha} if injected else None,
        "doctor": doctor,
    }


def _source_system_file(ctx: PlatformContext, probe: dict[str, Any]) -> Path:
    """Reuse the prompt already managed for this machine, never a shipped example."""
    installed = str(probe.get("system_file") or "")
    if installed and Path(installed).is_file():
        return Path(installed)
    candidate = ctx.prompt_file(PLATFORM_ID)
    if candidate.is_file():
        return candidate
    shared = ctx.managed_dir / "system-role.md"
    if shared.is_file():
        return shared
    example = REPO_ROOT / "examples" / "system-role.md"
    if example.is_file():
        return example
    raise AutorepairError("找不到可用的 system-role.md；请先安装 Keysmith 或提供 --prompt-file")


def _actions_from(report: dict[str, Any]) -> list[dict[str, Any]]:
    actions = []
    for item in report.get("actions") or []:
        if isinstance(item, dict):
            actions.append(
                {
                    "action": str(item.get("action") or ""),
                    "path": str(item.get("path") or ""),
                    "detail": str(item.get("detail") or ""),
                }
            )
    return actions


def repair(ctx: PlatformContext, probe: dict[str, Any]) -> dict[str, Any]:
    source = _source_system_file(ctx, probe)
    args = ["--system-file", str(source), *_common_args(ctx, probe)]
    args.append("--dry-run" if ctx.dry_run else "--yes")
    result = _run_keysmith(ctx, "install", *args)
    ok = bool(result.get("ok"))
    if ok and not ctx.dry_run:
        detail = f"已用 {source.name} 重新打补丁"
    elif ok:
        detail = f"计划：用 {source.name} 重新打补丁（未写入）"
    else:
        blockers = result.get("blockers") or []
        reason = "；".join(str(item) for item in blockers) or result.get("error") or "未知原因"
        detail = f"打补丁未成功：{reason}"
        if "not permitted" in reason.lower() or "eperm" in reason.lower():
            detail += (
                "。当前上下文没有修改 App 包的权限：macOS 的应用管理保护不允许 launchd / 后台任务"
                "写别的 App 包（同一用户从终端或 GUI 里跑则正常）。请在 Bar Control 破甲层或"
                "发布管理中心里执行修复——首次会弹一次系统授权，批准后即可自动完成。"
            )
    return {"ok": ok, "actions": _actions_from(result), "detail": detail}


def verify(ctx: PlatformContext) -> dict[str, Any]:
    """Verify the injection that is actually active for this machine.

    In runtime-patch mode the app bundle itself carries the managed-prompt
    marker, so the patch is judged by doctor's runtime.patched.  Keysmith's own
    verify is still run with --no-smoke, because in runtime-patch mode its
    wrapper smoke test re-derives a patch from the already-patched bundle file
    and can never find the original anchor (upstream keeps --no-smoke for this).
    """
    detection = detect(ctx)
    mode = ""
    doctor: dict[str, Any] = {}
    warnings: list[str] = []
    try:
        doctor = _run_keysmith(ctx, "doctor", *_common_args(ctx))
        mode = str((doctor.get("runtime") or {}).get("injection_mode") or "")
    except AutorepairError as error:
        warnings.append(str(error))
    args = list(_common_args(ctx))
    if mode == "runtime-patch":
        args.append("--no-smoke")
    try:
        result = _run_keysmith(ctx, "verify", *args)
    except AutorepairError as error:
        return {"ok": False, "actions": [], "detail": str(error), "warnings": warnings}
    ok = bool(result.get("ok"))
    blockers = result.get("blockers") or []
    detail = "验证通过" if ok else f"验证未通过：{'；'.join(str(item) for item in blockers) or '未知原因'}"
    if mode == "runtime-patch":
        patched = bool((doctor.get("runtime") or {}).get("patched"))
        if not patched:
            ok = False
            detail = "运行期文件里没有受管提示词标记，注入未生效"
        else:
            warnings.append(
                "runtime-patch 模式下 wrapper smoke 不适用（wrapper 需从已打补丁的 App 文件中反推原始锚点），"
                "已用 --no-smoke 验证运行期补丁标记"
            )
            if not result.get("ok"):
                warnings.append(f"keysmith verify 仍然报告：{detail}")
            detail = f"运行期补丁标记存在（{detection.get('app_version') or '未知版本'}）"
            ok = True
    return {"ok": ok, "actions": _actions_from(result), "detail": detail, "warnings": warnings, "raw": result}


def revert(ctx: PlatformContext) -> dict[str, Any]:
    args = list(_common_args(ctx))
    args.append("--dry-run" if ctx.dry_run else "--yes")
    result = _run_keysmith(ctx, "uninstall", *args)
    return {
        "ok": bool(result.get("ok")),
        "actions": _actions_from(result),
        "detail": "已撤走 Keysmith 注入" if result.get("ok") else str(result.get("error") or "撤走失败"),
    }
