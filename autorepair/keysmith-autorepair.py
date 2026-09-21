#!/usr/bin/env python3
"""keysmith-autorepair: keep a true system-prompt injection alive across updates.

The Auto Repair layer does not patch anything by itself.  For each supported AI
coding tool it answers three questions and nothing more:

1. did the tool update (version / runtime hash / managed config changed)?
2. is the prompt injection still in place?
3. if not, is the new layout one we know how to patch?

When the answer to (3) is no, it stops and says so instead of guessing.

Usage:
    keysmith-autorepair.py status  [--json]
    keysmith-autorepair.py check   [--json] [--auto]
    keysmith-autorepair.py repair  --platform zcode [--yes]
    keysmith-autorepair.py enable  --platform codex
    keysmith-autorepair.py install-agent --yes

Exit status: 0 = nothing to do, 1 = a platform needs attention, 2 = usage error.
Mutating operations print a plan unless --yes is given, matching zcode-keysmith.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import autorepair_core as core  # noqa: E402

LAUNCH_AGENT_LABEL = "com.cth123456.keysmith-autorepair"
LAUNCH_AGENT_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
LOG_PATH = Path.home() / "Library" / "Logs" / "keysmith-autorepair.log"


def build_context(args: argparse.Namespace, *, dry_run: bool) -> tuple[core.PlatformContext, Path, Path]:
    requested_dir = getattr(args, "managed_dir", None)
    managed_dir = core.expand_path(requested_dir) if requested_dir else core.DEFAULT_MANAGED_DIR
    config, config_path = core.load_config(managed_dir)
    state, state_path = core.load_state(managed_dir)
    prompt_file = getattr(args, "prompt_file", None)
    if prompt_file:
        config["prompt_file"] = str(core.expand_path(prompt_file))
    ctx = core.PlatformContext(
        managed_dir=managed_dir,
        config=config,
        state=state,
        dry_run=dry_run,
        verbose=bool(getattr(args, "verbose", False)),
        config_path=config_path,
        state_path=state_path,
    )
    return ctx, config_path, state_path


def selected_ids(modules: dict[str, object], args: argparse.Namespace) -> list[str]:
    requested = getattr(args, "platform", None)
    known = [pid for pid in modules if not pid.startswith("?")]
    if requested:
        missing = [pid for pid in requested if pid not in known]
        if missing:
            raise core.AutorepairError(f"未知平台：{', '.join(missing)}（可用：{', '.join(sorted(known)) or '（无）'}）")
        return list(requested)
    return sorted(known)


def collect_status(
    ctx: core.PlatformContext,
    modules: dict[str, object],
    ids: list[str],
    *,
    repair: bool,
    auto_only: bool = False,
) -> list[dict[str, object]]:
    """Probe (and optionally repair) each platform.

    auto_only is the unattended path: it repairs only platforms the user marked
    auto_repair AND that were already confirmed injected once (a recorded
    baseline).  A machine that was never injected is never patched unattended.
    """
    results = []
    for platform_id in ids:
        module = core.resolve_module(modules, platform_id)
        allow = False
        if repair:
            if auto_only:
                entry = ctx.platform_config(platform_id)
                state_entry = ctx.platform_state(platform_id)
                allow = bool(entry.get("auto_repair")) and bool(state_entry.get("baseline"))
            else:
                allow = True
        results.append(core.platform_status(module, ctx, repair=allow))
    return results


def emit(payload: dict[str, object], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    operation = payload.get("operation")
    print(f"[{operation}] {payload.get('summary')}")
    for item in payload.get("platforms") or []:
        if not isinstance(item, dict):
            continue
        version = item.get("app_version") or ""
        head = f"- {item.get('label')}（{item.get('id')}）：{item.get('state_label')}"
        if version:
            head += f"  {version}"
        print(head)
        detail = str(item.get("detail") or "").strip()
        if detail:
            print(f"    {detail}")
        for action in item.get("actions") or []:
            if isinstance(action, dict):
                print(f"    · {action.get('action')} {action.get('path')} {action.get('detail')}".rstrip())
        for warning in item.get("warnings") or []:
            print(f"    ! {warning}")
    for warning in payload.get("warnings") or []:
        print(f"! {warning}")
    for blocker in payload.get("blockers") or []:
        print(f"✗ {blocker}")
    if payload.get("error"):
        print(f"✗ {payload['error']}")


def render_agent_command(ctx: core.PlatformContext) -> list[str]:
    script = Path(__file__).resolve()
    argv = [sys.executable or "/usr/bin/python3", str(script), "check", "--auto", "--json"]
    if ctx.managed_dir != core.DEFAULT_MANAGED_DIR:
        argv.extend(["--managed-dir", str(ctx.managed_dir)])
    return argv


def write_launch_agent(ctx: core.PlatformContext, interval: int, *, dry_run: bool) -> dict[str, object]:
    watch_paths: list[str] = []
    if bool((ctx.config.get("agent") or {}).get("watch_runtime", True)):
        # Fire right after a tool update replaces its runtime or config.
        watch_paths.append("/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs")
        watch_paths.append(str(Path.home() / ".codex" / "config.toml"))
    payload: dict[str, object] = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": render_agent_command(ctx),
        "RunAtLoad": True,
        "StartInterval": int(interval),
        "StandardOutPath": str(LOG_PATH),
        "StandardErrorPath": str(LOG_PATH),
        "ProcessType": "Background",
    }
    if watch_paths:
        payload["WatchPaths"] = watch_paths
    actions = [
        {"action": "plan" if dry_run else "write", "path": str(LAUNCH_AGENT_PATH), "detail": "LaunchAgent"}
    ]
    if dry_run:
        return {"actions": actions, "detail": "计划：安装定时自检（未写入）"}
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LAUNCH_AGENT_PATH.open("wb") as handle:
        plistlib.dump(payload, handle)
    os.chmod(LAUNCH_AGENT_PATH, 0o644)
    _launchctl(["bootout", _domain_target(), str(LAUNCH_AGENT_PATH)], quiet=True)
    code, output = _launchctl(["bootstrap", _domain_target(), str(LAUNCH_AGENT_PATH)])
    if code != 0:
        code, output = _launchctl(["load", "-w", str(LAUNCH_AGENT_PATH)])
    actions.append(
        {
            "action": "load" if code == 0 else "error",
            "path": str(LAUNCH_AGENT_PATH),
            "detail": output.strip() or f"launchctl 退出码 {code}",
        }
    )
    return {"actions": actions, "detail": "已安装定时自检" if code == 0 else f"已写入 plist，加载失败：{output.strip()}"}


def remove_launch_agent(*, dry_run: bool) -> dict[str, object]:
    if dry_run:
        return {
            "actions": [{"action": "plan", "path": str(LAUNCH_AGENT_PATH), "detail": "移除 LaunchAgent"}],
            "detail": "计划：移除定时自检（未写入）",
        }
    actions: list[dict[str, object]] = []
    if LAUNCH_AGENT_PATH.is_file():
        code, output = _launchctl(["bootout", _domain_target(), str(LAUNCH_AGENT_PATH)], quiet=True)
        if code != 0:
            code, output = _launchctl(["unload", "-w", str(LAUNCH_AGENT_PATH)], quiet=True)
        LAUNCH_AGENT_PATH.unlink(missing_ok=True)
        actions.append({"action": "remove", "path": str(LAUNCH_AGENT_PATH), "detail": output.strip()})
    return {"actions": actions, "detail": "已移除定时自检"}


def _domain_target() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(argv: list[str], *, quiet: bool = False) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            ["/bin/launchctl", *argv],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return 1, str(error)
    output = ((completed.stdout or "") + (completed.stderr or "")).strip()
    if not quiet and output:
        print(output, file=sys.stderr)
    return completed.returncode, output


def save_config(ctx: core.PlatformContext) -> None:
    if ctx.config_path is not None:
        core.write_json_atomic(ctx.config_path, ctx.config)


def save_state(
    ctx: core.PlatformContext,
    platforms: list[dict[str, object]] | None = None,
    operation: str = "",
) -> None:
    """Persist state and refresh the compact snapshot GUI clients read."""
    if ctx.state_path is not None:
        ctx.state["updated_at"] = core.now_iso()
        core.write_json_atomic(ctx.state_path, ctx.state)
    if platforms is not None and not ctx.dry_run:
        core.write_status_snapshot(ctx.managed_dir, platforms, operation)


def _common_flags() -> argparse.ArgumentParser:
    """Flags accepted both before and after the subcommand.

    SUPPRESS defaults are what make that work: a subcommand that does not repeat
    the flag leaves the value parsed by the main parser intact.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=f"Emit stable JSON ({core.JSON_SCHEMA})")
    common.add_argument("--managed-dir", default=argparse.SUPPRESS, help="Managed directory. Default: ~/.keysmith-autorepair")
    common.add_argument("--prompt-file", default=argparse.SUPPRESS, help="Shared system-role.md used when injecting")
    common.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS)
    return common


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="keysmith-autorepair",
        parents=[_common_flags()],
        description="Keep AI coding tool system-prompt injections alive across app updates.",
    )
    parser.add_argument("--version", action="version", version="keysmith-autorepair 0.1.0")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", parents=[_common_flags()], help="Read-only: report every platform, write nothing")
    status.add_argument("--platform", action="append", default=None)

    check = sub.add_parser("check", parents=[_common_flags()], help="Check all platforms and persist the observed state")
    check.add_argument("--platform", action="append", default=None)
    check.add_argument("--auto", action="store_true", help="Also repair platforms with auto_repair enabled")

    repair = sub.add_parser("repair", parents=[_common_flags()], help="Re-apply the prompt injection (plan unless --yes)")
    repair.add_argument("--platform", action="append", required=True)
    repair.add_argument("--yes", action="store_true")

    revert = sub.add_parser("revert", parents=[_common_flags()], help="Remove the managed injection (plan unless --yes)")
    revert.add_argument("--platform", action="append", required=True)
    revert.add_argument("--yes", action="store_true")

    enable = sub.add_parser("enable", parents=[_common_flags()], help="Enable tracking/repair for a platform")
    enable.add_argument("--platform", action="append", required=True)
    auto = enable.add_mutually_exclusive_group()
    auto.add_argument("--auto-repair", dest="auto_repair", action="store_true", default=None)
    auto.add_argument("--no-auto-repair", dest="auto_repair", action="store_false", default=None)

    disable = sub.add_parser("disable", parents=[_common_flags()], help="Disable tracking/repair for a platform")
    disable.add_argument("--platform", action="append", required=True)

    sub.add_parser("platforms", parents=[_common_flags()], help="List discovered platform adapters")

    install_agent = sub.add_parser("install-agent", parents=[_common_flags()], help="Install the periodic self-check LaunchAgent")
    install_agent.add_argument("--interval", type=int, default=0, help="Seconds between checks (default from config)")
    install_agent.add_argument("--yes", action="store_true")

    uninstall_agent = sub.add_parser("uninstall-agent", parents=[_common_flags()], help="Remove the self-check LaunchAgent")
    uninstall_agent.add_argument("--yes", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    as_json = bool(getattr(args, "json", False))
    operation = getattr(args, "command", "unknown")
    managed_dir = getattr(args, "managed_dir", None)
    lock_dir = core.expand_path(managed_dir) if managed_dir else core.DEFAULT_MANAGED_DIR
    try:
        with core.operation_lock(lock_dir):
            return _dispatch(args, operation, as_json)
    except core.AutorepairError as error:
        payload = core.build_report(operation, "preview", [], ok=False, error=str(error))
        emit(payload, as_json=as_json)
        return 2
    except KeyboardInterrupt:
        return 130


def _dispatch(args: argparse.Namespace, operation: str, as_json: bool) -> int:
    if operation == "platforms":
        ctx, _, _ = build_context(args, dry_run=True)
        modules = core.discover_platforms(ctx.managed_dir)
        rows = []
        for platform_id, module in sorted(modules.items()):
            if isinstance(module, Exception):
                rows.append({"id": platform_id, "label": platform_id, "enabled": False, "detail": f"加载失败：{module}"})
                continue
            entry = ctx.platform_config(platform_id)
            rows.append(
                {
                    "id": platform_id,
                    "label": str(getattr(module, "PLATFORM_LABEL", platform_id)),
                    "enabled": bool(entry.get("enabled")),
                    "auto_repair": bool(entry.get("auto_repair")),
                    "origin": "builtin" if str(getattr(module, "__file__", "")).find(str(ctx.managed_dir)) < 0 else "user",
                    "detail": str(getattr(module, "__doc__", "").splitlines()[0] if getattr(module, "__doc__", "") else ""),
                }
            )
        payload = core.build_report(operation, "preview", [], extra={"available": rows})
        payload["platforms"] = rows
        emit(payload, as_json=as_json)
        return 0

    if operation in ("enable", "disable"):
        ctx, _, _ = build_context(args, dry_run=False)
        modules = core.discover_platforms(ctx.managed_dir)
        ids = selected_ids(modules, args)
        for platform_id in ids:
            entry = ctx.platform_config(platform_id)
            entry["enabled"] = operation == "enable"
            if operation == "enable" and getattr(args, "auto_repair", None) is not None:
                entry["auto_repair"] = bool(args.auto_repair)
            elif operation == "enable":
                entry.setdefault("auto_repair", False)
        if ctx.dry_run:
            raise core.AutorepairError("内部错误：enable/disable 不应处于 dry-run")
        save_config(ctx)
        results = collect_status(ctx, modules, ids, repair=False)
        save_state(ctx, results, operation)
        payload = core.build_report(operation, "execute", results)
        emit(payload, as_json=as_json)
        return 0

    if operation in ("install-agent", "uninstall-agent"):
        ctx, _, _ = build_context(args, dry_run=not args.yes)
        if operation == "install-agent":
            interval = args.interval or int((ctx.config.get("agent") or {}).get("interval_seconds") or 900)
            outcome = write_launch_agent(ctx, interval, dry_run=not args.yes)
        else:
            outcome = remove_launch_agent(dry_run=not args.yes)
        payload = core.build_report(
            operation,
            "preview" if not args.yes else "execute",
            [],
            extra={"actions": outcome["actions"], "detail": outcome["detail"]},
        )
        emit(payload, as_json=as_json)
        return 0

    if operation == "status":
        ctx, _, _ = build_context(args, dry_run=True)
        modules = core.discover_platforms(ctx.managed_dir)
        ids = selected_ids(modules, args)
        results = collect_status(ctx, modules, ids, repair=False)
        payload = core.build_report(operation, "preview", results)
        payload["exit_status"] = core.exit_code_for(results)
        emit(payload, as_json=as_json)
        return int(payload["exit_status"])

    if operation == "check":
        ctx, _, _ = build_context(args, dry_run=False)
        modules = core.discover_platforms(ctx.managed_dir)
        ids = selected_ids(modules, args)
        if args.auto:
            results = collect_status(ctx, modules, ids, repair=True, auto_only=True)
        else:
            results = collect_status(ctx, modules, ids, repair=False)
        save_config(ctx)
        save_state(ctx, results, operation)
        payload = core.build_report(operation, "execute", results)
        payload["exit_status"] = core.exit_code_for(results)
        emit(payload, as_json=as_json)
        return int(payload["exit_status"])

    if operation in ("repair", "revert"):
        ctx, _, _ = build_context(args, dry_run=not args.yes)
        modules = core.discover_platforms(ctx.managed_dir)
        ids = selected_ids(modules, args)
        results = []
        for platform_id in ids:
            module = core.resolve_module(modules, platform_id)
            if operation == "repair":
                results.append(core.platform_status(module, ctx, repair=True))
            else:
                outcome = module.revert(ctx)
                results.append(
                    {
                        "id": platform_id,
                        "label": str(getattr(module, "PLATFORM_LABEL", platform_id)),
                        "enabled": bool(ctx.platform_config(platform_id).get("enabled")),
                        "state": core.STATE_OK if outcome.get("ok") else core.STATE_ERROR,
                        "state_label": core.STATE_LABELS[core.STATE_OK if outcome.get("ok") else core.STATE_ERROR],
                        "injected": False,
                        "actions": outcome.get("actions") or [],
                        "detail": outcome.get("detail") or "",
                    }
                )
        if not ctx.dry_run:
            save_state(ctx, results, operation)
        payload = core.build_report(operation, "preview" if ctx.dry_run else "execute", results)
        payload["exit_status"] = core.exit_code_for(results)
        emit(payload, as_json=as_json)
        return int(payload["exit_status"])

    raise core.AutorepairError(f"未知命令：{operation}")


if __name__ == "__main__":
    raise SystemExit(main())
