# 非 Claude 上游接入方案（CodeBuddy / DeepSeek / dsh / WorkBuddy）

本文给出“先验证、后改造”的低风险接入路径，目标是不改固件协议，复用现有 daemon。

## 1. 三阶段落地

### 阶段 A（先可用）
- 只接 4 个事件：`SessionStart` / `PreToolUse` / `PostToolUse` / `Stop`
- 先打通审批闭环（PreToolUse 阻塞 + allow/deny 回传）
- dashboard 先允许信息不完整

### 阶段 B（补体验）
- 逐步补 `transcript_path` / model / tokens / activity 字段
- 让设备端看板接近 Claude 原生体验

### 阶段 C（远程与安全）
- 启用远程审批 relay（`/remote/v1/pending` + `/remote/v1/decision`）
- 打开 HMAC 鉴权、防重放、TTL、审计策略

## 2. 不改固件的接入方式

不要求上游工具直接懂 m5-paper-buddy 协议，只要求能发事件 JSON。

```
Upstream hooks/events
  -> tools/upstream_hook_adapter.py (字段映射)
  -> POST http://127.0.0.1:9876/hook
  -> tools/claude_code_bridge.py
  -> USB serial / BLE
  -> M5Paper firmware
```

## 3. 设备“绑定”机制说明

绑定不是“agent 直接绑硬件”，而是“daemon 绑设备 + hooks 绑 daemon”：

1. 上游 hook 把事件 POST 到 daemon `/hook`
2. daemon 自动选 transport  
   - 优先 USB：扫描 `/dev/cu.usbserial-*`、`/dev/ttyUSB*`  
   - 无 USB 则 BLE：扫描 `Claude-xxxx`，连接 NUS
3. 设备回传 `permission` / `focus_session` 指令给 daemon
4. daemon 把审批结果返回给上游 hook 调用方

## 4. 最小映射模板

- dsh：`tools/mappings/dsh_minimal_mapping.json`
- WorkBuddy：`tools/mappings/workbuddy_minimal_mapping.json`

示例（dry-run 先检查字段映射）：

```bash
cat /tmp/event.json | python3 tools/upstream_hook_adapter.py \
  --source dsh \
  --dry-run
```

`--source dsh` / `--source workbuddy` 默认会自动加载内置 mapping；
需要覆盖时再显式传 `--mapping`。

转发到 daemon：

```bash
cat /tmp/event.json | python3 tools/upstream_hook_adapter.py \
  --source dsh \
  --mapping tools/mappings/dsh_minimal_mapping.json
```

## 5. 验证清单（阶段 A）

- `PreToolUse` 是否真实阻塞等待审批结果  
- `allow/deny` 是否正确恢复/拒绝上游工具执行  
- 并发审批是否 FIFO，不串单  
- daemon 不可用时，是否能回退为空响应（不拖死主流程）

建议先在本机 USB 模式验证，再切 BLE，再切远程 relay。
