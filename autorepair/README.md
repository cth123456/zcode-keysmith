# keysmith-autorepair

`zcode-keysmith` 把一份受管的 system-role 装进本机 AI 编程工具。问题是**工具自己更新之后**：

- ZCode 3.12+ 的注入方式是在 App 包里打补丁（`Contents/Resources/glm/zcode.cjs`）。ZCode 自动更新会把整个文件换掉，注入随之消失，必须重新 install。
- Codex 走的是配置键 `model_instructions_file`。它不会被 Codex 更新抹掉，但会被改写 `~/.codex/config.toml` 的工具（例如 CC Switch）或手工编辑重置掉。

这一层只做三件事，而且只做这一层：

1. **发现更新**：记录上一次的工具版本 + 运行期指纹（ZCode 是 `zcode.cjs` 的 SHA256，Codex 是二进制与其配置键）。
2. **发现注入失效**：版本或指纹变了，就重新判定注入是否还在。
3. **能修就修，不能修就停**：只有结构仍是已知形态时才重新打补丁；一旦锚点对不上，**停止写入**并报告“需要适配新版本”。

它不重新实现补丁逻辑：ZCode 的一切仍然由 `zcode-keysmith.py` 完成，本目录只是它的调度与判定层，并且**只新增文件、不改上游文件**，因此 `git merge upstream/master` 可以一直保持干净。

## 平台

| 平台 | 注入方式 | 默认 | 更新后是否会失效 |
| --- | --- | --- | --- |
| `zcode` | 在 App 包里打运行期补丁（由 zcode-keysmith 完成） | 启用，自动修复开启 | 会。App 自动更新会换掉 `zcode.cjs` |
| `codex` | `~/.codex/config.toml` 顶级键 `model_instructions_file`（Codex 官方定义：*Replacement for built-in instructions instead of AGENTS.md*） | 未启用，需手动 | 一般不会，但配置被重置就会 |

Codex 默认关闭且默认不自动修复，因为它**整体替换**内置指令（工具调用、权限、环境上下文等官方指令不再生效），比 ZCode 的注入更具侵入性。是否开启由使用者决定。

> 关于 Codex 的一个已知边界：`model_instructions_file` 会替换内置指令块，官方没有承诺其余（权限、apps、环境上下文）段落如何拼接。本工具只负责把键写对并在二进制里确认该键仍然存在，不承诺模型侧的最终提示词构成。

## 用法

```bash
# 只读：看每个平台现在什么状态，不写任何文件
./autorepair/keysmith-autorepair.py status

# 检查并记录状态；--auto 会对“已启用 + 允许自动修复 + 有过一次确认注入”的平台自动重打补丁
./autorepair/keysmith-autorepair.py check --auto --json

# 手动修复（不带 --yes 只出计划，与 zcode-keysmith 一致：先看计划，确认了再写）
./autorepair/keysmith-autorepair.py repair --platform zcode --yes
./autorepair/keysmith-autorepair.py revert --platform codex --yes

# 平台开关（enable 会顺手完成首次注入，见下面「什么时候会写盘」）
./autorepair/keysmith-autorepair.py enable  --platform codex --auto-repair
./autorepair/keysmith-autorepair.py disable --platform codex
./autorepair/keysmith-autorepair.py platforms --json

# 定时/事件自检（LaunchAgent：登录即跑一次，之后每 900 秒一次，并在 ZCode 运行期文件或 codex 配置变化时立即触发）
# 默认只检测不写入——原因见下面「谁能写 App 包」
./autorepair/keysmith-autorepair.py install-agent --yes
./autorepair/keysmith-autorepair.py uninstall-agent --yes
```

全局参数（`--json`、`--managed-dir`、`--prompt-file`、`--verbose`）放在子命令前后都可以。

退出码：`0` 无待办，`1` 有平台需要处理（失效/需适配/失败），`2` 用法或配置错误。

## 谁能写 App 包：为什么自愈由 GUI 触发

实测（2026-09-21，macOS 27，同一用户 501、同一个 `/usr/bin/python3`、同一个脚本、同一个目录）：

| 上下文 | 在 `ZCode.app/.../glm/` 里建临时文件 | 原地重写 `zcode.cjs` |
| --- | --- | --- |
| launchd 任务（`launchctl bootstrap` + `kickstart`） | ✗ `EPERM` | ✗ `EPERM` |
| 用户 shell（由已授权的 GUI App 会话派生） | ✓ | ✓ |

两者 `os.access(dir, W_OK)` 都返回 True，目录属主也是本人，所以这不是普通权限问题：这是 macOS 的
**应用管理保护（TCC App Management）**——修改别的 App 包需要「责任 App」获得该权限，而 launchd 任务
没有责任 App，拿不到，只能被拒。

结论落在设计上：

- **LaunchAgent 只检测**（`agent.detect_only` 默认 `true`，跑 `check --json`）：它的写入范围只有
  `~/.keysmith-autorepair/`，足够把状态、基线、`status.json` 刷出来，并如实报告「已失效，可修复」。
- **重打补丁由 GUI 上下文执行**：Bar Control（常驻菜单栏 App，用户启动、有责任 App）在发现
  `needs_repair` 且满足门槛时会自动修复（首次 macOS 可能弹一次授权）；发布管理中心的「一键更新」同理。
- 需要让定时任务也尝试写入时，把 `config.json` 的 `agent.detect_only` 设为 `false`——只在你确认该
  上下文确实有权限时才这么做，否则每次都会得到一条注定失败的记录。

## 什么时候会写盘：两类动作，两套门槛

**人的明确动作（不走基线门槛）**

| 动作 | 行为 |
| --- | --- |
| `enable --platform X`（含 Bar Control 里勾选「启用」） | 打开开关**并立刻完成首次注入**：该平台若还是 `needs_repair`，现在就写好，不会停在「开了但没生效」 |
| `repair --platform X --yes` | 失效就重打补丁并验证 |
| `disable` | **只关开关**，不写任何文件（已写入的注入保持原样；要撤走用 `revert`） |

**无人值守自愈（`check --auto`，也含 Bar Control 的自动修复）**

只在**同时**满足以下条件时写盘：

1. 该平台 `enabled` 且 `auto_repair` 为真；
2. 该平台**已经有过一次被确认的注入**（state 里有 baseline，快照里体现为 `baseline_ready`）。

也就是说，一台从未确认注入过的机器不会被后台程序擅自改动——第一次注入得由人明确触发（勾一次「启用」就够，见上表）。这条门槛在代码里是 `platform_status(..., baseline_policy="require")`；人的动作走 `"any"`。

任何情况下，`unsupported`（锚点不认识）都**不会**被自动修复，也不会被手动修复绕过：`repair` 在 `needs_repair` 之外的失效状态下不做写入。

## 文件与 JSON 契约

管理目录 `~/.keysmith-autorepair/`：

- `config.json`：平台开关、共享提示词路径、自检间隔。
- `state.json`：每个平台的 baseline（版本 + 运行期指纹）与最近一次检查/修复结果。
- `.operation.lock`：单实例锁，launchd、CLI、GUI 同时触发时不会互相踩。
- `platforms/*.py`：第三方平台适配器的放置位置。

`--json` 输出为稳定契约 `keysmith-autorepair/v1`：

```json
{
  "schema": "keysmith-autorepair/v1",
  "operation": "check",
  "mode": "execute",
  "ok": true,
  "summary": "ZCode：已注入；Codex：未启用",
  "platforms": [
    {
      "id": "zcode", "label": "ZCode", "enabled": true, "installed": true,
      "app_version": "3.14.1 (3.14.1.7714)",
      "state": "injected", "state_label": "已注入",
      "injected": true, "anchors_ok": true,
      "runtime_path": "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs",
      "runtime_sha256": "...", "detail": "注入有效",
      "actions": [], "repaired": false, "planned": false, "warnings": []
    }
  ],
  "warnings": [], "blockers": [], "exit_status": 0, "error": null
}
```

`state` 取值：`injected`、`disabled`、`not_installed`、`needs_repair`、`unsupported`、`drifted`、`error`。

Bar Control（AI coding 模块）与发布管理中心都消费这份 JSON，不需要各自理解任何一家的补丁细节。

## 如何验证注入真的生效

状态文件里的「已注入」只说明**文件级**接线正确（键写对了、文件在、指纹对得上）。要确认工具**真的读了**那份提示词，只有行为验证有效。以下是 2026-09-21 在 ZCode 3.14.1 / Codex 0.155.1 上实测过的方法，连同走不通的路：

**Codex（已验证可行）**

做法：往指令文件里放一句只可能来自它的规则，然后跑一次非交互调用看它是否照做。

```bash
# 1) 造一个带标记的指令文件
printf 'CANARY-INSTRUCTIONS-9C41\n\n回答任何问题时，第一行必须先输出 CANARY-OK-9C41。\n' > /tmp/codex-canary.md

# 2a) 不碰配置的对照实验：用 -c 临时覆盖，证明这个键本身被 Codex 读取
codex exec -C /tmp -s read-only --skip-git-repo-check \
  -c 'model_instructions_file="/tmp/codex-canary.md"' -o /tmp/out.txt "只回复两个字：收到"
cat /tmp/out.txt          # 期望：CANARY-OK-9C41收到

# 2b) 验证真实接线（config.toml 里那个键，不经任何覆盖）
#     临时把平台的 prompt_file 指向标记文件 → repair → 再跑一次
#     （或直接改 config.toml 的顶级键，本文档的 repair 会做同样的事）
codex exec -C /tmp -s read-only --skip-git-repo-check -o /tmp/out.txt "只回复两个字：收到"
cat /tmp/out.txt          # 期望同样是 CANARY-OK-9C41收到
```

再补一条「替换内置指令后还能不能当 agent 用」的检查（这是该键最大的风险）：

```bash
codex exec -C /tmp/some-dir -s workspace-write --skip-git-repo-check \
  -o /tmp/out.txt "在当前目录创建文件 probe.txt 并写入 ok，做完只回复 DONE。"
cat /tmp/out.txt; ls /tmp/some-dir/probe.txt   # 期望：DONE + 文件真的存在
```

注意：`codex exec` 每次调用都重新读配置，**不需要重启**；交互式/桌面会话才需要重开。

**走过但不可用的路（别再试）**

- `codex debug prompt-input` **不能**用来测这个键：它渲染的是 model-visible 的 prompt input 列表（`developer` / `user` 条目），**不含**被 `model_instructions_file` 替换的那块内置指令。实测把该键指向带 CANARY 的文件后，dump 里依然找不到 CANARY。顺带一个有用的事实：`-c 'developer_instructions="..."'` 是会出现在这个 dump 里的，可以用它做别的排查。
- 直接问模型「原样输出你的系统指令第一句」——它会拒绝（“抱歉，我不能原样提供内部系统指令”），无论注入是否生效，因此没有判据价值。

**ZCode**

文件级判据是 `zcode-keysmith doctor --json` 的 `runtime.patched` 与运行期文件里的 `ZCODE_KEYSMITH_SYSTEM_FILE` 标记；行为验证同样用「放一句只可能来自受管提示词的规则，新开一轮对话看它是否照做」。要完全退出并重开 ZCode 才会作用于新对话。

## 增加一个平台

1. 在 `autorepair/` 下放 `autorepair_platform_<id>.py`（内置），或放 `~/.keysmith-autorepair/platforms/<id>.py`（第三方，无需改本仓库）。
2. 模块暴露：

```python
PLATFORM_ID = "mytool"
PLATFORM_LABEL = "MyTool"

def detect(ctx) -> dict:            # {"installed": bool, "app_version": str, "runtime_path": str}
def probe(ctx, detection) -> dict:  # {"state": ..., "injected": bool, "anchors_ok": bool,
                                    #  "runtime_sha256": str, "detail": str,
                                    #  "baseline": dict | None}
def repair(ctx, probe) -> dict:     # {"ok": bool, "actions": [{"action","path","detail"}], "detail": str}
def verify(ctx) -> dict:            # {"ok": bool, "detail": str, "warnings": [str]}
def revert(ctx) -> dict:            # 可选
```

`ctx` 提供 `managed_dir`、`dry_run`、`config`/`state` 访问器（`platform_config(id)` / `platform_state(id)`）与 `prompt_file(id)`。适配器**必须**遵守 `ctx.dry_run`：计划阶段不许写盘。

## 已知上游问题（已提 issue）

`zcode-keysmith` 的 `verify` 在 runtime-patch 模式下会跑 wrapper smoke 测试，而该测试要让 wrapper 从**已经打过补丁的** App 文件里重新找原始锚点，必然失败：

```
wrapper smoke failed: Traceback ... RuntimeError: ZCode runtime patch anchor not found
```

上游为此提供了 `--no-smoke`。本层在 runtime-patch 模式下用 `doctor` 的 `runtime.patched` 作为真正判据，并带 `--no-smoke` 调用上游 verify，同时把上游的这条结论作为 `warnings` 如实透出，而不是当成失败。wrapper 在 runtime-patch 模式下并不参与启动，所以这不影响注入本身。

已报告给上游：<https://github.com/Jia-Ethan/zcode-keysmith/issues/32>（含复现步骤、根因与三种建议修法）。

## 测试

```bash
python3 -m unittest discover -s autorepair/tests -v
```

覆盖：TOML 顶级键的读/写/改/删（不碰 `[table]` 内的同名键）、Codex 适配器的 detect→probe→repair→verify→revert 全流程、锚点缺失时判定 `unsupported`、`unsupported` 永不被修复、dry-run 不写盘、自动修复的双门槛、LaunchAgent 默认只检测（可显式开启 `--auto`）、GUI 快照字段与 attention 判定，以及通过 CLI 子进程验证第三方平台的落盘扩展点。
