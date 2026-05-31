# Claude Code Monitor 架构说明

## 概述

一个轻量级 HTTP 服务，用于监控 Claude Code 会话状态。通过 Claude Code 的 hook 机制收集事件和状态数据，提供 Web 页面展示（针对 Kindle 优化）和 JSON API。

- **端口**: 8787（默认，可通过 `PORT` 环境变量修改）
- **协议**: HTTP 纯文本（内部用，无 TLS）

---

## 核心数据结构

### `SESSIONS`（全局字典，线程安全）

```
session_id -> {
    "status":      {},    # 最近一次 POST /statusline 的完整 JSON
    "last_event":  {},    # 最近一次 POST /hook 的 event
    "events":      [],    # 所有 event 的环形缓冲区（最多 50 条）
    "last_update": float, # 最后一次收到任何 POST 的时间戳
}
```

### `ACTIVE_SESSION`（全局变量）

记录最近更新的 `session_id`，用于 API 默认选择会话。

所有读写操作通过 `LOCK`（`threading.Lock()`）保护。

---

## API 端点

### `POST /hook` —— 接收 Claude Code 事件钩子

- **数据来源**: Claude Code 的 hook 事件（SessionStart, PreToolUse, PostToolUse 等）
- **行为**:
  1. 从 POST body 提取事件字段（通过 `safe_event()` 过滤白名单）
  2. 更新 `last_event` 和 `events[]`（最多保留 50 条）
  3. 返回 `204 No Content`

### `POST /statusline` —— 接收 Claude Code 状态数据

- **数据来源**: Claude Code 的 `statusLine` 配置
- **行为**:
  1. 将 POST body 的完整 JSON 存入 `sess["status"]`
  2. 不生成事件记录
  3. 返回 `204 No Content`

### `GET /` —— Kindle 主页面

- 返回完整的 HTML 页面（约 250 行内联 HTML+CSS+JS）
- 页面通过 JS 轮询 `/sessions` 和 `/data.json` 更新数据

### `GET /sessions` —— 会话列表（JSON）

```json
[
  {"session_id": "...", "project": "...", "age": 123},
  ...
]
```

### `GET /data.json?session=<id>` —— 会话详情（JSON）

```json
{
  "model":       "Claude Opus 4.7",
  "project":     "cc-monitor",
  "label":       "收到新任务",
  "detail":      "description: xxx",
  "ctx_pct":     45,
  "cost":        "0.123",
  "duration":    "3m 21s",
  "rate":        "5h 12% · 7d 5%",
  "events_html": "<div class=\"event\">...</div>",
  "age":         5
}
```

### `GET /state.json` —— 调试端点

返回原始的 `{sessions, active}` 字典。

---

## 数据处理流程

### 事件安全过滤（`safe_event()`）

1. 从原始事件中提取白名单字段（`hook_event_name`, `tool_name`, `notification_type` 等）
2. 如果存在 `tool_input`，额外提取 `command`, `file_path`, `pattern` 等子字段
3. 所有字段截断到 180 字符
4. 添加 `_time`（格式化的 HH:MM:SS 时间戳）

### 状态构建（`build_data()`）

每次 GET `/data.json` 时实时组装：

1. 从 `status` 提取: model, workspace, cost, duration, context_window, rate_limits
2. 从 `last_event` 提取: 当前状态标签和详情
3. 从 `events[]` 提取: 最近 8 条事件（倒序）
4. 计算: 上下文使用率、费用总额、耗时、频率限制、距上次更新秒数

---

## Hook 安装器（`--install-hooks`）

```
python server.py --install-hooks [DIR]
```

在 `DIR/.claude/settings.json` 中写入:

1. **13 个 hook 事件**: SessionStart/End, UserPromptSubmit, PreToolUse, PostToolUse, PostToolUseFailure, PermissionRequest, Notification, SubagentStart/Stop, PreCompact, PostCompact, Stop, StopFailure
2. **statusLine 配置**: `{"type": "command", "command": "curl -s -X POST .../statusline ..."}`

每个 hook 和 statusLine 都通过 `curl` 将 stdin 的 JSON 管道发送到本机服务器。

---

## 前端（Kindle 优化）

### 样式要点
- 黑白配色，粗边框，大字体（48px 状态、30px 指标）
- 无 meta-refresh，全靠 JS 轮询（适配 Kindle 浏览器）

### JS 轮询策略
| 轮询对象 | 间隔 | 目的 |
|---------|------|------|
| `/sessions` | 5s | 检测新会话 |
| `/data.json` | 3s | 刷新当前会话数据 |

### 离线检测
- 连续 3 次请求失败 → 显示全屏黑色离线遮罩
- 显示距上次成功连接的秒数

### 会话切换
- 点击 model 名称展开下拉菜单
- 支持键盘导航（`selectByIndex(i)`）
- 点击外部区域自动关闭

---

## 关键设计决策

1. **单线程锁模型**: 全局 `LOCK` 保护所有 `SESSIONS` 读写，简单可靠
2. **事件缓冲**: 每个会话保留最多 50 条事件，防止内存泄漏
3. **服务端渲染事件 HTML**: `build_data()` 直接生成 HTML 字符串，前端只需 `innerHTML`
4. **无持久化**: 所有数据在内存中，服务重启后丢失
5. **日志静默**: `log_message()` 被重写为空方法，避免日志污染

---

## 文件结构

```
server.py               # 单文件，包含所有逻辑
.claude/settings.json   # hook 配置（由 --install-hooks 生成或手动编写）
```

---

## 启动方式

```bash
# 启动服务器
python server.py

# 安装 hook（在当前目录的 .claude/settings.json）
python server.py --install-hooks .

# 指定端口
PORT=8080 python server.py
```
