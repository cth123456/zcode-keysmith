<!-- markdownlint-disable MD013 -->
# zcode-keysmith GUI — 工程规范（SPEC）

版本 `0.1.0-beta.1`，channel `candidate` / `development`，未签名 Pre-release。本规范描述桌面客户端的架构与不可违背的约束；所有条目都能在代码中找到对应实现。本轮目标是第一份 unsigned 桌面候选（`desktop-v0.1.0-beta.1`），sidecar 包 **master 上的 CLI 0.3.2**，不是 Latest tag `v0.3.1` zip。tag 本身不在本规范里假装已经存在。

## 1. 定位与边界

GUI 是 `../zcode-keysmith.py` 的可视化封装，不是重实现：

- **所有**文件写入由 CLI 完成；GUI 不直接读写任何部署目标（`~/.zcode-keysmith`、`system-role.md`、wrapper、LaunchAgent plist、`glm/zcode.cjs`）。
- GUI 只消费 `zcode-keysmith/v1` JSON 契约，不解析人类可读文本输出，不搬 Codex 文本 parser。
- 所有业务调用带 `--json`；**不传 `--lang`**。CLI 保持 subcommand 式，除已有 `--json` 外不改文本输出、不改成 flag 式。
- 页面固定四块：Dashboard = `doctor --json`；Deploy = `install --dry-run` / `install --yes`；Manage = `uninstall`（无 recover，展示 `.bak_`）；Settings。无 Scenarios / Fixtures / recover 按钮。
- 测试与烟雾探测必须使用 `--dry-run` 或临时目录 / 临时 `--launch-agent`，不得读或写本机 `~/Library/LaunchAgents/com.jia.zcode-keysmith.env.plist`。

## 2. 技术栈

| 层 | 选型 |
|---|---|
| 壳 | Tauri 2（`tauri.conf.json`，identifier `com.jia-ethan.zcode-keysmith-gui`） |
| 前端 | React 19 + Vite 6 + Tailwind CSS 4 + Radix UI + Motion |
| CLI 载体 | PyInstaller onefile sidecar `zcode-keysmith-cli`（`scripts/build-sidecar.mjs`） |
| i18n | react-i18next（`zh-CN` / `en`，`src/i18n/`）；GUI 语言只改界面，不传给 CLI |
| 反馈 | sonner toast |

## 3. 构建信息

- GUI 版本号唯一来源是 `gui/package.json`（`0.1.0-beta.1`）；CLI 版本唯一来源是仓库根 `VERSION`（本轮 sidecar = `0.3.2`）。
- `vite.config.js` 在 dev/build/test 时注入 `__ZCODE_KEYSMITH_BUILD_INFO__`：`desktopVersion` 来自 `package.json`，`sourceCommit` 来自 `ZCODE_KEYSMITH_SOURCE_COMMIT`（40 位 hex，否则拒绝）。
- `src/lib/buildInfo.js` 只做归一化：拿得到 commit 时 channel 为 `candidate`，否则 `development`；不硬编码任何版本。
- commit 只标注构建来源，不代表已发布 tag。

## 4. 进程边界（`src-tauri/src/cli_runner.rs`）

Rust 侧对 CLI 的全部责任：

1. **定位**：sidecar 优先（与主程序同目录的 `zcode-keysmith-cli`[`.exe`]）→ `ZCODE_KEYSMITH_CLI` 环境变量 → 主程序目录 / `~/.zcode-keysmith-gui` / `~/zcode-keysmith` / `~/ZCodeProject/zcode-keysmith` / `~/.local/bin` / `~/bin` / `/usr/local/bin` / `/opt/homebrew/bin` 中的 `zcode-keysmith` / `zcode-keysmith.py` → PATH。`.py` 走 Python（`ZCODE_KEYSMITH_PYTHON` 覆盖），runtime 标记为 `bundled` / `executable` / `python`。
2. **启动**：`Command::new(program).args(argv)`——argv 数组，**永不** shell 字符串拼接。`kill_on_drop(true)`。Unix `process_group(0)`；Windows `CREATE_NEW_PROCESS_GROUP`。
3. **限量**：stdout/stderr 各 2 MiB 上限；超限继续排空管道（避免子进程阻塞在满管道上），但标记截断并以「输出不完整」失败关闭。
4. **限时**：默认 30 s；`cli_version` 探测 15 s；前端写操作 120 s。超时杀**整棵进程树**：Unix `kill(-pid, SIGKILL)`（覆盖 PyInstaller bootloader 子孙）；Windows `taskkill /PID <pid> /T /F`。
5. **管道 drain**：leader `wait()` 之后用 500 ms `finish_read_task` 收 stdout/stderr。reader 每读一块就把快照推进 `watch`。超时则 abort reader，再给 100 ms 取消宽限期；宽限期拿不到 join 结果时用快照，已读到的 stdout/stderr 不丢。**leader 已退出、子孙仍持有管道时**，对启动时保存的 pid 进程组再发 SIGKILL（`timeout_covers_pipes_after_leader_exit` 同时核对子孙仍在该组），不得再查已 reap 的 `child.id()`。
6. **解码**：UTF-8 lossy。

暴露给前端的 Tauri command：`cli_run` / `detect_cli` / `cli_version` / `cli_runtime` / `read_manifest`（`src-tauri/src/lib.rs`）。`read_manifest` 只读托管目录下精确文件名 `config.json`。

## 5. 前端数据层

### 5.1 契约解析（`src/lib/parser.js`）

- `extractJson`：只取 stdout 首个完整顶层 JSON 对象（容忍前后噪声；未闭合 = 不完整）。
- `parseContract`：`timed_out` / 非 JSON / `schema` 存在且 `!== "zcode-keysmith/v1"` ⇒ `ContractError`（失败关闭）。`ContractError` 保留 `stdout` / `stderr` / `exitCode` / `timedOut`，供 Dashboard `StatusFailure` 展示。非零 exit 但 JSON 完整时照常返回，交给视图层。
- `gateReport(report)`：**唯一** proceed 判定——`exitCode !== 0`、`blockers.length > 0`、`ok === false` 任一成立即 `{ok: false, reasons}`。
- 视图模型按 operation 分流：`parseWriteReport`（install / uninstall）、`parseDoctorReport`。无 recover / restore / scenarios parser。
- 参数构造 `build*Args` 只产出字符串数组：`install` / `doctor` / `uninstall` 加可选 `--managed-dir` / `--launch-agent` / `--zcode-runtime` / `--node-command` / `--system-file`。永不附加 `--lang`。

### 5.2 API 封装（`src/lib/api.js`）

- 每个 invoke 都包在操作租约里（`beginOperation` / `endOperation`）；退出排队中直接 reject。
- 写操作的 execute 阶段走 `cliRunExclusive`（`beginExclusiveOperation`）；preview 与读操作走共享租约。
- 所有业务调用带 `--json`；写操作成对出现：`previewDeploy` / `previewUninstall`（`--dry-run`）→ `executeDeploy` / `executeUninstall`（追加 `--yes`，120 s 超时）。
- `resolveCli`：手动路径优先验证（version + runtime），否则走 Rust 侧 sidecar 优先探测。
- `CliError` 保留 `stdout` / `stderr` / `exitCode` / `timedOut`。

### 5.3 状态与生命周期（`src/lib/store.js` + `windowLifecycle.js`）

- **全局写互斥**：`beginExclusiveOperation` 原子获取；已有操作或退出排队时返回 `null`。由 `api.js` 的 `cliRunExclusive` 在 install / uninstall 写路径上实际持有。
- **操作租约**：UI 统一读 `operationInProgress` 作为交互锁；租约未清零拒绝视图切换。
- **关闭屏障**：`onCloseRequested` → `requestExitWhenIdle`；有活动租约时排队（queued close），最后一个租约结束后销毁窗口（`destroy()` 失败回退 `close()`）；销毁期间屏障保持封闭，迟到的 sidecar 不会启动。**无托盘**——关闭即退出。
- **单实例**：`tauri-plugin-single-instance`，二次启动 unminimize + show + focus 主窗口。
- **窗口**：1200×800，最小 900×600，`devtools: false`；CSP `default-src 'self'` + 本地 IPC，无远程资源。

### 5.4 设置持久化（`src/lib/settings.js`）

localStorage 单键 `zcode-keysmith-gui:settings`：`cliPath`（留空 = 自动探测）、`defaultManagedDir`、`lang`、`theme`。

## 6. 页面

| 页面 | 数据来源 | 关键状态 |
|---|---|---|
| **Dashboard** | `doctor --json` | CLI 版本 / runtime、managed wrapper / system file、runtime patchable、`.bak_` 列表。失败走 `StatusFailure`：timeout / exit / stdout / stderr，不只一句 `error.message` |
| **Deploy**（3 步向导） | 表单（managed dir / 内置或本地 system-role）→ `install --dry-run --json` preview → 确认后 `--yes` | preview gate、execute 报告；成功后刷新 doctor |
| **Manage** | `doctor --json` | 仅 uninstall：`preview → ConfirmDialog → execute`。无 recover 按钮。展示 `.bak_` |
| **Settings** | `resolveCli`、localStorage、build info | CLI 路径覆盖与探测结果（path / version / runtime）、默认托管目录、语言与主题、版本与 commit 展示 |

报告渲染统一走 `ReportView`（actions / backups / warnings / blockers），原始 JSON 可展开（`RawJson`）。

## 7. 视觉识别

浅色 clay + 深色 tech blue，与 codex-keysmith 的视觉 token 对齐：

| Token | Light | Dark |
|---|---|---|
| `--accent` | `#d97757`（hover `#c6613f`） | `#6a9bcc`（hover `#8ab4dd`） |
| `--bg-primary` | `#faf9f5`（米色纸面） | `#0f0f0f`（深空黑） |
| brand 渐变 | `#d97757 → #c6613f → #d4a27f` | `#6a9bcc → #8b7fcc → #a78bfa` |

- muted 文本两主题对比度 ≥ 4.5:1（`globals.css` 中有注释标注）。
- 玻璃卡片：`backdrop-filter` + 点阵纹理 + 内高光。
- 环境层：三团主题色光晕缓慢漂移（`AmbientBg`），`prefers-reduced-motion` 下静止。
- 紧凑层级、状态 pill、toast 反馈保留。

## 8. Sidecar 构建契约（`scripts/build-sidecar.mjs`）

- 仅本机原生构建：`aarch64-apple-darwin` / `x86_64-pc-windows-msvc`；host 与 target 不一致直接报错（PyInstaller onefile 不支持交叉）。不构建 Linux / Intel Mac。
- **frozen 资源**：打包前检查 `zcode-keysmith.py` 的 `REPO_ROOT` 在 frozen 时解析 `sys._MEIPASS`；缺失则打等效 source patch，契约变了则拒绝打包。`examples/` 以 `--add-data` 进入 `sys._MEIPASS`。
- 构建环境净化：`PYTHONNOUSERSITE=1`，删除 `PYTHONHOME` / `PYTHONPATH` / `PYTHONUSERBASE`；`PYTHON` 环境变量指定解释器（需 `pip install -r requirements-build.txt`）。
- 产物原子落位 `src-tauri/binaries/zcode-keysmith-cli-<triple>[.exe]`（先复制到临时名再 rename，Unix 下 `chmod 755`）。
- 烟雾测试：`--version` 必须等于根 `VERSION`（本轮 `0.3.2`）；隔离临时目录下 `doctor --json --managed-dir/--launch-agent/--zcode-runtime/--node-command` 必须产出 `schema: zcode-keysmith/v1`。空目录没有安装，doctor 以 exit 1 和 blockers 结束，这是契约，不是打包失败。stdout/stderr 若出现本机 `~/Library/LaunchAgents/com.jia.zcode-keysmith.env.plist` 路径，构建失败。
- `npm run bundle` 是唯一打包入口：先构建 sidecar，再加载 `tauri.bundle.conf.json` 启用 bundle 并声明 `externalBin`。常驻配置 `bundle.active=false`，裸 `tauri build` 只产 executable；即使显式传 `--bundles`，默认 `beforeBundleCommand` 也会拒绝。overlay 覆盖该 hook 后仍按 `TAURI_ENV_TARGET_TRIPLE` 校验目标 sidecar 存在且可执行。
- macOS 目标由 `tauri.macos.conf.json` 声明 `app` + `dmg`；Windows 由 `tauri.windows.conf.json` 声明 NSIS currentUser + WebView2 downloadBootstrapper。无签名、无公证、无 auto-update。
- 桌面候选 CI：`.github/workflows/desktop-candidate.yml`，macos-15 arm64 DMG + windows-2025 NSIS，原生 sidecar，`--version` 与隔离目录 `doctor --json`。不跑 Codex scenario / fixture 烟测。

## 9. 不变量（改动必须保持）

1. GUI 不写部署目标文件；一切写入经 CLI `--yes`。
2. argv 数组边界，无 shell 拼接。
3. 截断 / 超时 / 非契约 JSON 一律失败关闭。
4. `gateReport` 是唯一的 proceed 判定。
5. Manage 只有 uninstall；无 recover UI、无 Scenarios、不传 `--lang`。
6. 无网络、无遥测、无凭证接触。
7. 单实例、写互斥、关闭屏障、无托盘。
8. 版本与 channel 不写死；GUI 版本来自 `package.json`，bundled CLI 来自根 `VERSION`。
9. 任何测试 / sidecar 烟测不得触碰本机活跃 LaunchAgent。
10. sidecar 绑 master 上的 CLI 源码，不绑 `v0.3.1` zip。
