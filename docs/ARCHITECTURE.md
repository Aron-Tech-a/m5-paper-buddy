# 架构与实现细节

> 本文面向要 fork、改、或把这套模式搬到其它硬件的人。一口气把**通信协议**、**daemon 内部**、**固件内部**、**我们踩过的坑**都摊开讲。

---

## 1. 系统总览

```
╭─────────────────╮ hook 事件   ╭──────────────────╮   JSON 行   ╭────────────────╮
│  Claude Code    │ ─(curl)──▶  │ bridge daemon    │ ──serial──▶ │ M5Paper 固件   │
│  CLI (多窗口)   │  localhost  │ (Python, HTTP)   │  或 BLE     │ (ESP32+M5EPD)  │
│                 │ ◀────────── │                  │ ◀────────── │                │
╰─────────────────╯ hook 响应    ╰──────────────────╯  permission ╰────────────────╯
                                         │ cmd / focus_session
                                         ▼
                               ┌─────────────────┐
                               │ ~/.claude-buddy │
                               │ daemon.pid /log │
                               └─────────────────┘
```

**三层结构**：

1. **Claude Code 端**：用户日常跑的 CLI。通过 `~/.claude/settings.json` 里的 hooks 配置，在 `SessionStart / UserPromptSubmit / PreToolUse / PostToolUse / Stop` 事件时 `curl -X POST` 到 daemon。
2. **Bridge daemon**（`tools/claude_code_bridge.py`）：长驻 Python 进程，HTTP server 收 hook + transport（USB/BLE）向 Paper 推 heartbeat，双向。
3. **M5Paper 固件**（`src/paper/`）：ESP32 程序，渲染 dashboard / 审批卡 / 设置页，处理按键 + 触屏，把响应回传给 daemon。

Daemon 是**唯一状态机**，固件本质是无状态的 view — 重启不丢东西（除了 NVS 里的几个用户偏好：DND / 语言 / 累积统计）。

---

## 2. 通信协议

### 2.1 Transport 层

两条物理通道，**帧格式完全一样**：**UTF-8 JSON + `\n` 行分隔**。

| | USB serial | BLE (Nordic UART Service) |
| --- | --- | --- |
| 速率 | 115200 baud | 协商 MTU（~180-185 bytes/notify） |
| TX 字符写法 | `Serial.write()` / `pyserial.write()` | `bleWrite()` 分 MTU 批量 `notify` |
| 加密 | 无 | LE Secure Connections + MITM + Bonding（passkey 6 位） |
| 初次配对 | 不需要 | macOS 系统对话框输 passkey（设备上显示） |

两者共存 — 固件同时监听 Serial 和 BLE 的 RX，回包也双写。Daemon 同一时刻只接一路（USB 优先，无则 BLE）。

### 2.2 Heartbeat：daemon → device

Daemon **触发式 + 10 秒保活**（`BUMP_EVENT`），在速率限制 1Hz 下发送。一条 heartbeat 是个单行 JSON：

```jsonc
{
  "total": 3,                 // 所有 session 数
  "running": 2,               // 正在跑的
  "waiting": 1,               // 阻塞在 PreToolUse 的
  "msg": "approve: Bash",     // 一句话摘要
  "entries": [                // 活动日志（最多 8 条，最新的第一个）
    "10:42 > 帮我跑单测",
    "10:42 Bash allow",
    "10:41 Edit done"
  ],
  "tokens": 45300,            // focused session 的当前上下文大小
  "tokens_today": 45300,      // 同上（历史原因两个字段都给）
  "budget": 200000,           // 上下文窗口上限，0 = 隐藏进度条
  "model": "Opus 4.7",        // focused session 的 model 短名
  "assistant_msg": "…",       // focused session 最近一条 assistant 文本

  "project": "m5-paper-buddy", // focused session 的项目名
  "branch": "main",
  "dirty": 2,

  "sessions": [               // 所有 session 的压缩行（dashboard 列表）
    {"sid":"a1b2c3d4", "full":"a1b2c3d4-xxx-refactor",
     "proj":"m5-paper-buddy", "branch":"main", "dirty":2,
     "running":true, "waiting":false, "focused":true}
    // ... 最多 5 条
  ],

  "prompt": {                 // 当前在队首的待审批（可选）
    "id": "req_1776498646197",
    "tool": "Edit",
    "kind": "permission",     // 或 "question"
    "hint": "src/paper/main.cpp",
    "body": "…完整 diff / 命令…",
    "project": "m5-paper-buddy",
    "sid": "a1b2c3d4",
    "options": ["React","Vue","Svelte"]   // 仅 kind=question
  }
}
```

固件忽略未知字段 → 完全**向后兼容**扩展。

### 2.3 Device → daemon

设备只回三类命令（也是单行 JSON）：

```jsonc
// Hook 响应 / 队列命令
{"cmd":"permission", "id":"req_xxx", "decision":"once"}     // PUSH 同意
{"cmd":"permission", "id":"req_xxx", "decision":"deny"}     // DOWN 拒绝
{"cmd":"permission", "id":"req_xxx", "decision":"option:2"} // 触屏点第 3 个选项

// 把 dashboard focus 换成指定 session
{"cmd":"focus_session", "sid":"a1b2c3d4-xxx-refactor"}

// Ack 各种命令
{"ack":"owner", "ok":true, "n":0}
{"ack":"status", "ok":true, "data":{...}}
```

### 2.4 握手

Daemon 每次连上 device（serial 打开或 BLE 订阅成功）就发：

```jsonc
{"cmd":"owner", "name":"op7418"}
{"time": [1776507293, 28800]}        // epoch_sec, tz_offset_sec
{... heartbeat ...}
```

Device 会 ack 第一个 `owner`，写到 NVS 里当 pet name 展示用。

### 2.5 WiFi 阶段远程审批 relay（PoC）

第一阶段不动现有 serial/BLE 主链路，只在 daemon 侧开放远程审批接口：

- `GET /remote/v1/pending`：拉取当前 **FIFO 队首**审批（没有则 `request=null`）
- `POST /remote/v1/decision`：提交 `allow/deny`

请求摘要字段（最小集）：

```jsonc
{
  "request_id": "req_1776498646197",
  "session_id": "a1b2c3d4-xxx-refactor",
  "project": "m5-paper-buddy",
  "tool": "Bash",
  "hint": "npm test",
  "summary": "$ npm test ...",
  "expires_at": 1776498676,
  "ttl_sec": 27
}
```

安全（PoC 已实现）：

- 共享密钥 `BUDDY_REMOTE_SECRET`
- HMAC-SHA256 头签名：`device + timestamp + nonce + body`
- 时间窗校验（防重放）
- nonce 去重（防重放）
- 仅允许审批当前 FIFO 队首（不破坏原队列语义）

---

## 3. Daemon 内部

### 3.1 模块分工

```
claude_code_bridge.py (~600 行)
├── Transport 抽象
│   ├── SerialTransport  : pyserial
│   └── BLETransport     : bleak (asyncio loop on 独立线程)
├── 状态层（全局 + STATE_LOCK 保护）
│   ├── SESSIONS_RUNNING / TOTAL / WAITING   set[sid]
│   ├── SESSION_META     dict[sid → {cwd, project, branch, dirty, checked_at}]
│   ├── SESSION_ASSISTANT dict[sid → 最近 assistant 文本]
│   ├── SESSION_MODEL     dict[sid → "Opus 4.7" 等]
│   ├── SESSION_CONTEXT   dict[sid → input+output tokens]
│   ├── PENDING_PROMPTS   dict[prompt_id → prompt_obj]   # FIFO 队列
│   ├── ACTIVE_PROMPT     队首指针
│   └── FOCUSED_SID       用户 tap 的 session
├── HTTP server (Claude Code hooks 入口)
│   └── HookHandler  dispatch by hook_event_name
├── Heartbeat thread  (10s 保活 + BUMP 触发)
└── Transcript 解析
    ├── extract_last_assistant  最近 assistant 文本
    ├── extract_session_model   model 字段（hook payload 不带，在 transcript 里）
    └── extract_session_context input_tokens + output_tokens（≈ 上下文占用）
```

### 3.2 并发模型

- **主线程**：HTTP server（多线程 `HTTPServer`，每请求一 handler 线程）
- **Heartbeat 线程**：`threading.Thread`，阻塞在 `BUMP_EVENT.wait(timeout=10)`
- **Transport reader 线程**：
  - Serial：阻塞 read 循环
  - BLE：独立线程里 `asyncio.run()`，`BleakClient` 的 `start_notify` 回调 push 字节
- **Transport writer**：`SERIAL_LOCK` 串行化；BLE 用 `run_coroutine_threadsafe` 调度到 asyncio loop

**踩过的坑**：BLE `on_connect` 回调**不能**在 asyncio loop 线程里同步调用 `write`（会死锁 —— 写是通过 `run_coroutine_threadsafe` 往同一个 loop 提交 coroutine，但 loop 被回调阻塞）。修复：`on_connect` 另起一个线程跑。

### 3.3 FIFO 审批队列

Hook handler 里的 `_pretool` 会：

1. 生成唯一 `prompt_id`（`req_{ms}_{pid}`）
2. 创建 `threading.Event` + `PENDING[prompt_id]` 存 handler
3. 把 prompt_obj 塞进 `PENDING_PROMPTS`（dict 保插入顺序 → 天然 FIFO）
4. 如果 `ACTIVE_PROMPT is None`，这条成为队首
5. `event.wait(timeout=30)`
6. 唤醒 or 超时后，从 `PENDING_PROMPTS` 移除；如果本条正好是队首，把队列里下一条（最早插入的）提升成新队首
7. 根据 decision 构造 `hookSpecificOutput` JSON 返回

这个设计的好处：并发多个 PreToolUse 各有独立 Event，互不串扰；UI 上看到的始终是最老那条（FIFO）。

### 3.4 Bypass permissions 快速通道

`hook_event_name == PreToolUse` 但 `payload.permission_mode == "bypassPermissions"` 时，daemon 直接返回 allow，**不 block 30 秒**。Paper 上 ACTIVITY 记一行 `Bash (bypass)`。

这避免了一个早期 UX 坑：一个开了超级权限的窗口每个 tool 调用都 block 30s 等你按按钮，等到 daemon 超时回 `{}`，Claude Code fall-back 到默认策略照样跑，**但你的 bash 命令真的要等 30s 才执行**。

### 3.5 Rate limiting

固件端 2s 重绘 + 30s idle 节流已经够防 flood，但 daemon 侧还加了 heartbeat 1Hz 硬限（`MIN_INTERVAL = 1.0`）。起因是早期一个繁忙窗口每秒 5-10 次 hook → 每次 BUMP → 每次都推 heartbeat → ESP32 主循环被 JSON 解析 + 重绘喂饱 → IDLE 任务饿死 → TG1 / RTC WDT 双复位。

---

## 4. 固件内部

### 4.1 核心文件

```
src/paper/
├── main.cpp          # ~1350 行，UI + 状态机 + 触屏 + 设置页 + i18n
├── data_paper.h      # TamaState 定义 + JSON 解析（含 _applyJson / _LineBuf）
├── xfer_paper.h      # cmd 响应（name / owner / unpair / status）
├── buddy_frames.h    # ASCII 猫 6 个状态
├── ../ble_bridge.*   # 共享：Nordic UART Service 实现
└── ../stats.h        # 共享：NVS-backed 计数器 + 偏好
```

### 4.2 TamaState

一个 struct 装完设备要看的**全部状态**（约 2.3KB）：

- 会话数（total / running / waiting）
- Transcript 行数组 `lines[8][92]`
- 当前 prompt（id / tool / hint / body / kind / options / project / sid）
- Sessions 列表 `sessions[5]`（每条 sid / full / project / branch / dirty / running / waiting / focused）
- 设置 / 遥测：model / assistantMsg / budget / tokens

Daemon 的 heartbeat 字段和这个 struct **一一对应**（JSON key → 字段名映射）。`_applyJson` 用 `ArduinoJson v7` 解析一次就更新全部。

### 4.3 绘图策略

**画布层**：

```cpp
M5EPD_Canvas canvas(&M5.EPD);
canvas.createCanvas(540, 960);     // portrait, rotation=90
canvas.loadFont("/cjk.ttf", LittleFS);
for (sz : {TS_SM, TS_MD, TS_LG, TS_XL, TS_XXL, TS_HUGE})
  canvas.createRender(sz, 128);    // glyph cache
```

**刷新模式**：

| 模式 | 时间 | 质感 | 用途 |
| --- | --- | --- | --- |
| `UPDATE_MODE_DU` | ~260ms | 1-bit 阈值化，有锯齿 | 早期做 partial，后弃用 |
| `UPDATE_MODE_GL16` | ~450ms | 16 灰度无闪烁 | ✅ **当前 partial**，保住 TTF 抗锯齿 |
| `UPDATE_MODE_GC16` | ~450ms + 闪一下 | 16 灰度+清残影 | ✅ 模式切换、全刷 |

**节流逻辑**（`main.cpp::loop()`）：

```cpp
bool interactive = inPrompt || settingsOpen;
uint32_t partialGap = interactive ? 2000UL : 30000UL;   // idle dashboard 30s
bool canPartial = (now - lastPartialRefreshMs) >= partialGap;
bool shouldFull = (now - lastFullRefreshMs) >= 120000UL; // GC16 每 2 分钟

// 模式切换（prompt 出现/消失、设置页开/关、语言切换、长按 UP）bypass 节流
if (promptId changed) lastPartialRefreshMs = 0;
```

### 4.4 UTF-8 换行

M5EPD 自带 `canvas.textWidth()` 对 CJK glyph 返回值不可靠。自己写了个 **codepoint-aware 宽度估算 + 换行器**（`wrapText`）：

1. 按 UTF-8 字节数判断码点长度（1/2/3/4 字节）
2. ASCII 码点宽 ≈ 0.55 × textSize；多字节 ≈ textSize（方块字）
3. 累加到 maxWidthPx 溢出时回退找最近空格断；CJK 无空格就在当前码点直接断

这个估算**略偏大**（overshoot），所以宁可多换一行也不裁掉。

```cpp
int estWidth(const char* s, int textSize) {
  int w = 0;
  while (*p) {
    int cpLen = utf8_cp_len(*p);   // 1..4
    w += (cpLen == 1) ? (textSize * 55 / 100) : textSize;
    p += cpLen;
  }
  return w;
}
```

### 4.5 ASCII Buddy 对齐

TTF 是比例字体，直接 `drawString` 会把 ASCII 猫挤成一坨。解决：**每个字符单独 draw 进固定 cellW × lineH 的网格**，保证列对齐。

```cpp
for (int row = 0; row < 5; row++)
  for (int col = 0; col < 12; col++) {
    char ch[2] = {line[col], 0};
    if (ch[0] && ch[0] != ' ')
      canvas.drawString(ch, x0 + col*cellW + cellW/2, y0 + row*lineH);
  }
```

### 4.6 触屏处理

GT911 中断驱动。核心循环：

```cpp
if (M5.TP.available()) {
  M5.TP.update();
  if (!M5.TP.isFingerUp()) {
    tp_finger_t f = M5.TP.readFinger(0);
    lastX = f.x; lastY = f.y; hadTouch = true;
  } else if (hadTouch) {
    hadTouch = false;
    // hit-test against optionRects[] / sessionRects[] / settingsTrigger / ...
  }
  M5.TP.flush();
}
```

**踩过的坑**：`getFingerNum()` 在手指抬起后还锁住返回最后一次计数（不归零），所以必须用 `isFingerUp()` 判抬起事件。

### 4.7 i18n

简单得离谱：一个宏 + 一个全局变量。

```cpp
static uint8_t uiLang = 0;   // 0=EN, 1=ZH, 持久化到 NVS
#define LX(en, zh) (uiLang == 1 ? (zh) : (en))

canvas.drawString(LX("PROJECT", "项目"), 24, 108);
```

切换时强制 GC16 全刷（`lastFullRefreshMs = 0`），避免灰度残影把两个语言的字重叠。

### 4.8 NVS 持久化

Key namespace: `"buddy"`。

| Key | 类型 | 用途 |
| --- | --- | --- |
| `dnd` | bool | DND 模式开关 |
| `lang` | uchar | UI 语言（0=EN, 1=ZH） |
| `appr` | ushort | 累积 approval 数 |
| `deny` | ushort | 累积 deny 数 |
| `lvl` | uchar | token 等级 |
| `tok` | uint | 累积 output tokens |
| `owner` | str | daemon push 的用户名 |
| `petname` | str | 宠物名（接口预留） |

NVS 写慢且损耗磨损寿命（~100K 写），所以只在**重要事件**（approval/denial/level-up）时写，不做计时轮询写。

---

## 5. 分区布局

自定义 `partitions-m5paper.csv`（原 `no_ota.csv` 给的 LittleFS 空间不够放 3.4MB 字体）：

| Name | Type | SubType | Offset | Size | 用途 |
| --- | --- | --- | --- | --- | --- |
| nvs | data | nvs | 0x9000 | 20 KB | Preferences |
| otadata | data | ota | 0xe000 | 8 KB | （不做 OTA 但必须留） |
| app0 | app | factory | 0x10000 | 3 MB | 固件本体（实际 ~1.3 MB） |
| spiffs | data | spiffs | 0x310000 | 13 MB | LittleFS（cjk.ttf） |

ESP32 Arduino 的 LittleFS 驱动识别的是 `subtype spiffs`（历史原因），所以表里名字是 spiffs 但实际 mount 为 LittleFS。

---

## 6. 我们踩过的坑集锦

| 症状 | 根因 | 修复 |
| --- | --- | --- |
| 设备收到 heartbeat 就 crash 重启 | Claude 回复含中文 → UTF-8 码点被 `wrapText` 按字节切一半 → `drawString` 查 glyph 表越界 → WDT | 先 daemon 侧 ASCII-strip 保命；根治：打 CJK 字体 + codepoint-wrap |
| 高频 hook 流下设备 WDT 硬复位 | `BUMP_EVENT` 每 hook 触发 → heartbeat 洪流 | daemon `MIN_INTERVAL = 1.0`，BUMP 合并 |
| BLE 连上后第一波写全 timeout | `on_connect` 在 asyncio loop 线程调用同步 `write()` → `run_coroutine_threadsafe` 自己等自己死锁 | 回调另起线程跑 |
| `pio uploadfs` 报 "Bad CPU type" | PlatformIO 自带 `mklittlefs` 是 x86_64 binary | `brew install mklittlefs` + 替换 symlink（安装脚本自动处理） |
| 模型栏显示成一条竖线 | `tama.modelName` 空，em dash "—" fallback 在 GenSenRounded 里渲染得贴着列分隔线 | 空时不画 fallback；且 model 改从 transcript assistant 消息里扒 |
| 进度条卡 0% / 显示 2.5M/1M | 跨 session 累加 output tokens 无意义 | 改成 focused session **当前上下文**（最后一轮 input+output） |
| 触屏 tap 不灵 | 用 `getFingerNum()` 判抬起，它不归零 | 改用 `isFingerUp()` |
| 接 Claude Code bypass 模式时每个 bash 卡 30s | `permission_mode: bypassPermissions` 但 hook 照样 block | daemon 识别该模式直接回 allow |
| 多个 PreToolUse 并发时只看到最新的 | 全局 `ACTIVE_PROMPT` 被后来者覆盖 | 改 FIFO 队列，一次一个，自动推进 |
| Idle 时文字糊 | DU 1-bit 模式阈值化掉了 TTF 灰度 | 改用 GL16 16-gray 无闪烁 |
| GitHub Contributors 里有 Felix | 最初 force-push 时带着上游的祖先 commit；commit trailer 里有 `noreply@anthropic.com` | 重建 repo history，单 commit，无 co-author trailer |

---

## 7. 部署拓扑

```
[一台 Mac]
  └── Claude Code × N 窗口
        ↓ hooks (curl localhost:9876)
  └── ~/.claude-buddy/ 进程（daemon，单例，9876 端口）
        ↓ serial / BLE
  └── M5Paper × 1
```

**多设备**未实现（daemon 只驱一台 Paper）。如果要：把 Transport 升级成列表，每条走一台；heartbeat 广播一致；命令按 device id 路由。

**多机共享 Paper**不现实（daemon 和 Paper 物理耦合）。改成 WiFi mode 后每台机各自连同一台 Paper 才能做 — 那时就得有 session id 的全局命名空间，现在的 `session_id` 是 hook payload 里原样传的，应该够 unique。

---

## 8. 怎么扩展

- **加一个 hook 事件**：daemon `_on_xxx` 方法 + heartbeat 新字段 + 固件 `_applyJson` 解析一个新字段。
- **加一个工具的 body 渲染**：daemon `body_from_tool()` 里加一个 `if tool == "NewTool":` 分支。
- **换一种字体**：替换 `data/cjk.ttf`，改 `canvas.loadFont("/xxx.ttf")`，`createRender` size 常量可能要调。
- **支持另一块板子**（比如 Kindle、Lilygo T5）：改 `partitions-*.csv`、`platformio.ini`、`ble_bridge.cpp` / `main.cpp` 里调用的 `M5.*` API。`stats.h` / `data_paper.h` 基本可以原样。
- **加个命令**（如 `cmd:screenshot`）：`xfer_paper.h` 加 case + daemon reader_loop 发送路径。

---

## 参考

- [anthropics/claude-desktop-buddy](https://github.com/anthropics/claude-desktop-buddy) — 协议形状的来源
- [M5Stack M5EPD 库](https://github.com/m5stack/M5EPD)
- [ArduinoJson v7](https://arduinojson.org/v7/)
- [bleak](https://github.com/hbldh/bleak)（Python BLE）
- [PlatformIO](https://platformio.org/)
