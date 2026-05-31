#!/usr/bin/env python3
"""
Claude Code Kindle Monitor
- POST /hook        <- Claude Code hook events
- POST /statusline  <- Claude Code statusLine JSON
- GET  /            <- Kindle page (JS polling, no meta-refresh)
- GET  /sessions    <- JSON list of known sessions
- GET  /data.json?session=<id>  <- pre-rendered data for JS to inject
- GET  /state.json  <- raw state (debug)
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import html
import json
import os
import threading
import time

PORT = int(os.environ.get("PORT", "8787"))
HOST = os.environ.get("HOST", "0.0.0.0")

LOCK = threading.Lock()
# session_id -> {status, last_event, events, last_update}
SESSIONS = {}
ACTIVE_SESSION = None   # most recently updated session_id

# ── helpers ───────────────────────────────────────────────────────────────────

def clip(value, limit=180):
    if value is None:
        return ""
    text = str(value).replace("\n", " ").replace("\r", " ")
    return text if len(text) <= limit else text[:limit] + "…"

def num(value, default=0):
    try:
        return float(value)
    except Exception:
        return default

def pct(value):
    return max(0, min(100, int(num(value, 0))))

def safe_event(event):
    keep = [
        "hook_event_name", "tool_name", "notification_type",
        "title", "message", "error", "reason", "cwd",
        "permission_mode", "source", "duration_ms", "agent_type",
    ]
    out = {k: clip(event[k]) for k in keep if k in event}
    tool_input = event.get("tool_input")
    if isinstance(tool_input, dict):
        si = {}
        for k in ["command", "description", "file_path", "pattern",
                  "path", "glob", "url", "query", "subagent_type"]:
            if k in tool_input:
                si[k] = clip(tool_input[k])
        if si:
            out["tool_input"] = si
    out["_time"] = time.strftime("%H:%M:%S")
    return out

def label_for(event):
    name = event.get("hook_event_name", "")
    tool = event.get("tool_name", "")
    if name == "SessionStart":      return "启动 / 恢复会话"
    if name == "UserPromptSubmit":  return "收到新任务"
    if name == "PreToolUse":        return f"准备执行 {tool}" if tool else "准备执行工具"
    if name == "PermissionRequest": return f"等待授权：{tool}" if tool else "等待授权"
    if name == "PermissionDenied":  return f"自动模式拒绝：{tool}" if tool else "自动模式拒绝"
    if name == "Notification":
        ntype = event.get("notification_type", "")
        if ntype == "permission_prompt": return "等待授权"
        if ntype == "idle_prompt":       return "空闲提醒"
        return "通知"
    if name == "PostToolUse":        return f"完成 {tool}" if tool else "工具完成"
    if name == "PostToolUseFailure": return f"工具失败：{tool}" if tool else "工具失败"
    if name == "SubagentStart":      return f"子代理启动：{event.get('agent_type', '')}"
    if name == "SubagentStop":       return f"子代理完成：{event.get('agent_type', '')}"
    if name == "PreCompact":         return "正在压缩上下文"
    if name == "PostCompact":        return "上下文压缩完成"
    if name == "Stop":               return "本轮完成"
    if name == "StopFailure":        return "出错 / API 失败"
    if name == "SessionEnd":         return "会话结束"
    return name or "等待事件"

def detail_for(event):
    parts = []
    ti = event.get("tool_input") or {}
    for k in ["description", "command", "file_path", "pattern", "query", "url"]:
        if ti.get(k):
            parts.append(f"{k}: {ti[k]}")
    for k in ["message", "error", "reason"]:
        if event.get(k):
            parts.append(event[k])
    return clip(" | ".join(parts), 260)

def session_project(sess):
    status = sess.get("status") or {}
    workspace = status.get("workspace") or {}
    cwd = workspace.get("current_dir") or status.get("cwd") or ""
    if not cwd:
        ev = sess.get("last_event") or {}
        cwd = ev.get("cwd") or ""
    return os.path.basename(cwd) if cwd else "unknown"

def get_or_create_session(session_id):
    global ACTIVE_SESSION
    if session_id not in SESSIONS:
        SESSIONS[session_id] = {
            "status": {},
            "last_event": {},
            "events": [],
            "last_update": time.time(),
        }
    ACTIVE_SESSION = session_id
    return SESSIONS[session_id]

# ── rendered data for JS ───────────────────────────────────────────────────────

def build_data(session_id):
    with LOCK:
        sess = SESSIONS.get(session_id) or {}
        status = dict(sess.get("status") or {})
        event  = dict(sess.get("last_event") or {})
        events = list(sess.get("events") or [])
        last_update = sess.get("last_update") or 0

    model = (status.get("model") or {}).get("display_name") or "Claude Code"

    workspace = status.get("workspace") or {}
    cwd = workspace.get("current_dir") or status.get("cwd") or event.get("cwd") or ""
    project = os.path.basename(cwd) if cwd else "unknown"
    repo = (workspace.get("repo") or {}).get("name") or project

    cost_val = num((status.get("cost") or {}).get("total_cost_usd"), 0)
    dur_ms   = num((status.get("cost") or {}).get("total_duration_ms"), 0)
    minutes  = int(dur_ms // 60000)
    seconds  = int((dur_ms % 60000) // 1000)

    ctx    = status.get("context_window") or {}
    ctx_p  = pct(ctx.get("used_percentage"))

    rl     = status.get("rate_limits") or {}
    fh     = (rl.get("five_hour") or {}).get("used_percentage")
    sd     = (rl.get("seven_day") or {}).get("used_percentage")
    rate_parts = []
    if fh is not None: rate_parts.append(f"5h {pct(fh)}%")
    if sd is not None: rate_parts.append(f"7d {pct(sd)}%")

    # events HTML (server-side for simplicity)
    rows = []
    for item in events[-8:][::-1]:
        t  = html.escape(item.get("_time", ""))
        lb = html.escape(label_for(item))
        dt = html.escape(detail_for(item))
        rows.append(
            f'<div class="event">'
            f'<div class="event-title">{t} · {lb}</div>'
            f'<div class="event-detail">{dt}</div>'
            f'</div>'
        )

    age = int(time.time() - last_update) if last_update else 0

    return {
        "model":      model,
        "project":    repo,
        "label":      label_for(event),
        "detail":     detail_for(event),
        "ctx_pct":    ctx_p,
        "cost":       f"{cost_val:.3f}",
        "duration":   f"{minutes}m {seconds}s",
        "rate":       " · ".join(rate_parts) if rate_parts else "—",
        "events_html": "".join(rows) if rows else '<div class="event-detail">还没有事件</div>',
        "age":        age,
    }

def build_sessions_list():
    now = time.time()
    result = []
    for sid, sess in SESSIONS.items():
        result.append({
            "session_id": sid,
            "project":    session_project(sess),
            "age":        int(now - (sess.get("last_update") or now)),
        })
    result.sort(key=lambda x: x["age"])
    return result

# ── static HTML page (served once, JS does the rest) ─────────────────────────

PAGE_HTML = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Code Monitor</title>
<style>
  *{box-sizing:border-box;}
  body{margin:0;padding:16px;font-family:-apple-system,BlinkMacSystemFont,Helvetica,Arial,sans-serif;background:#fff;color:#000;}
  .top{display:flex;justify-content:space-between;align-items:baseline;gap:12px;border-bottom:4px solid #000;padding-bottom:10px;margin-bottom:16px;}
  .model{font-size:26px;font-weight:800;}
  .project{font-size:20px;text-align:right;}
  .model-wrap{position:relative;}
  .model{font-size:26px;font-weight:800;cursor:pointer;user-select:none;}
  .session-dropdown{display:none;position:absolute;top:100%;left:0;min-width:220px;border:3px solid #000;background:#fff;z-index:100;margin-top:6px;}
  .session-dropdown.open{display:block;}
  .session-item{display:block;width:100%;font-size:20px;padding:12px 14px;border:none;border-bottom:2px solid #000;background:#fff;color:#000;text-align:left;cursor:pointer;font-family:inherit;line-height:1.2;}
  .session-item:last-child{border-bottom:none;}
  .session-item.active{background:#000;color:#fff;}
  .session-item-age{font-size:15px;opacity:0.7;}
  .status{font-size:48px;line-height:1.1;font-weight:900;margin-bottom:12px;}
  .detail{min-height:56px;font-size:20px;line-height:1.3;margin-bottom:20px;word-break:break-all;}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:20px;}
  @media(max-width:600px){.grid{grid-template-columns:1fr;}}
  .card{border:3px solid #000;padding:12px;min-height:80px;}
  .label{font-size:15px;text-transform:uppercase;letter-spacing:1px;}
  .value{font-size:30px;font-weight:800;margin-top:4px;}
  .bar{height:26px;border:3px solid #000;margin-top:6px;}
  .fill{height:100%;background:#000;width:0%;}
  .section-title{font-size:22px;font-weight:900;border-bottom:3px solid #000;padding-bottom:6px;margin-bottom:8px;}
  .event{border-bottom:2px solid #000;padding:7px 0;}
  .event-title{font-size:17px;font-weight:800;}
  .event-detail{font-size:15px;margin-top:3px;word-break:break-all;}
  .footer{margin-top:16px;font-size:13px;color:#444;}
  .footer.stale{font-size:15px;font-weight:800;color:#000;}
  #offline-overlay{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:#000;color:#fff;z-index:9999;flex-direction:column;justify-content:center;align-items:center;text-align:center;padding:32px;}
  #offline-overlay.show{display:flex;}
  .offline-title{font-size:64px;font-weight:900;line-height:1.1;}
  .offline-sub{font-size:22px;margin-top:20px;}
</style>
</head>
<body>

  <div id="offline-overlay">
    <div class="offline-title">服务器离线</div>
    <div class="offline-sub" id="offline-time">正在重连...</div>
  </div>

  <div class="top">
    <div class="model-wrap">
      <div class="model" id="model" onclick="toggleDropdown()">Claude Code &#9660;</div>
      <div class="session-dropdown" id="session-dropdown"></div>
    </div>
    <div class="project" id="project">未连接</div>
  </div>

  <div class="status" id="status-label">等待连接...</div>
  <div class="detail" id="detail-label"></div>

  <div class="grid">
    <div class="card">
      <div class="label">Context</div>
      <div class="value" id="ctx-value">—</div>
      <div class="bar"><div class="fill" id="ctx-bar"></div></div>
    </div>
    <div class="card">
      <div class="label">Cost</div>
      <div class="value" id="cost-value">—</div>
    </div>
    <div class="card">
      <div class="label">Duration</div>
      <div class="value" id="duration-value">—</div>
    </div>
    <div class="card">
      <div class="label">Rate limit</div>
      <div class="value" id="rate-value" style="font-size:20px">—</div>
    </div>
  </div>

  <div class="section-title">最近事件</div>
  <div id="events-list"><div class="event-detail">还没有事件</div></div>

  <div class="footer" id="footer">未连接</div>

<script>
var currentSession = '';
var failCount = 0;
var lastSuccess = null;
var FAIL_THRESHOLD = 3;
var _sessions = [];

function selectByIndex(i) {
    if (_sessions[i]) { selectSession(_sessions[i].session_id); }
}

function toggleDropdown() {
    var dd = document.getElementById('session-dropdown');
    var open = dd.classList.toggle('open');
    updateModelLabel(open);
}

function closeDropdown() {
    document.getElementById('session-dropdown').classList.remove('open');
    updateModelLabel(false);
}

function updateModelLabel(open) {
    var el = document.getElementById('model');
    if (!el) return;
    var base = el.textContent.replace(/\s*[▲▼▴▾]$/, '').replace(/\s*[▲▼▴▾]$/, '').trim();
    el.textContent = base + ' ' + (open ? '▲' : '▼');
}

document.addEventListener('click', function(e) {
    var wrap = document.querySelector('.model-wrap');
    if (wrap && !wrap.contains(e.target)) { closeDropdown(); }
});

function selectSession(id) {
    currentSession = id;
    closeDropdown();
    renderSessionDropdown();
    if (id) { fetchData(); }
}

function setText(id, val) {
    var el = document.getElementById(id);
    if (el && el.textContent !== val) { el.textContent = val; }
}

function setModel(name) {
    var dd = document.getElementById('session-dropdown');
    var open = dd && dd.classList.contains('open');
    var el = document.getElementById('model');
    if (el) { el.textContent = name + ' ' + (open ? '▲' : '▼'); }
}

function setHtml(id, val) {
    var el = document.getElementById(id);
    if (el) { el.innerHTML = val; }
}

function onXhrSuccess() {
    failCount = 0;
    lastSuccess = new Date();
    document.getElementById('offline-overlay').classList.remove('show');
}

function onXhrFail() {
    failCount++;
    if (failCount >= FAIL_THRESHOLD) {
        var overlay = document.getElementById('offline-overlay');
        overlay.classList.add('show');
        if (lastSuccess) {
            var ago = Math.floor((new Date() - lastSuccess) / 1000);
            setText('offline-time', ago + 's ago · reconnecting...');
        }
    }
}

function renderSessionDropdown() {
    if (!_sessions.length) {
        setText('session-current', '等待会话...');
        document.getElementById('session-dropdown').innerHTML = '';
        return;
    }
    var cur = _sessions.find(function(s) { return s.session_id === currentSession; });
    var curAge = cur ? (cur.age < 60 ? cur.age + 's' : Math.floor(cur.age / 60) + 'm') : '';
    setText('session-current', cur ? cur.project + '  ' + curAge : '选择会话');

    var parts = [];
    for (var i = 0; i < _sessions.length; i++) {
        var s = _sessions[i];
        var age = s.age < 60 ? s.age + 's' : Math.floor(s.age / 60) + 'm';
        var active = s.session_id === currentSession ? ' active' : '';
        var proj = s.project.replace(/&/g, '&amp;').replace(/</g, '&lt;');
        parts.push(
            '<button class="session-item' + active + '" onclick="selectByIndex(' + i + ')">' +
            proj + ' <span class="session-item-age">' + age + '</span>' +
            '</button>'
        );
    }
    document.getElementById('session-dropdown').innerHTML = parts.join('');
}

function fetchSessions() {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', '/sessions', true);
    xhr.timeout = 2000;
    xhr.onreadystatechange = function() {
        if (xhr.readyState !== 4) return;
        if (xhr.status !== 200) { onXhrFail(); return; }
        var sessions;
        try { sessions = JSON.parse(xhr.responseText); } catch(e) { onXhrFail(); return; }
        onXhrSuccess();
        _sessions = sessions;
        if (!currentSession && sessions.length > 0) {
            currentSession = sessions[0].session_id;
            fetchData();
        }
        renderSessionDropdown();
    };
    xhr.onerror = xhr.ontimeout = onXhrFail;
    xhr.send();
}

function fetchData() {
    if (!currentSession) return;
    var url = '/data.json?session=' + encodeURIComponent(currentSession);
    var xhr = new XMLHttpRequest();
    xhr.open('GET', url, true);
    xhr.timeout = 2000;
    xhr.onreadystatechange = function() {
        if (xhr.readyState !== 4) return;
        if (xhr.status !== 200) { onXhrFail(); return; }
        var d;
        try { d = JSON.parse(xhr.responseText); } catch(e) { onXhrFail(); return; }
        onXhrSuccess();

        setModel(d.model);
        setText('project',        d.project);
        setText('status-label',   d.label);
        setText('detail-label',   d.detail);
        setText('ctx-value',      d.ctx_pct + '%');
        setText('cost-value',     '$' + d.cost);
        setText('duration-value', d.duration);
        setText('rate-value',     d.rate);
        setHtml('events-list',    d.events_html);

        var footer = document.getElementById('footer');
        if (footer) {
            footer.textContent = d.age + 's ago';
            footer.className = d.age > 10 ? 'footer stale' : 'footer';
        }

        var bar = document.getElementById('ctx-bar');
        if (bar) { bar.style.width = d.ctx_pct + '%'; }
    };
    xhr.onerror = xhr.ontimeout = onXhrFail;
    xhr.send();
}

fetchSessions();
setInterval(fetchSessions, 5000);
setInterval(fetchData, 3000);
</script>
</body>
</html>"""

# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8", "ignore") or "{}")
        except Exception:
            return {}

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        data = self.read_json()
        session_id = data.get("session_id") or "default"

        with LOCK:
            sess = get_or_create_session(session_id)
            sess["last_update"] = time.time()

            if self.path == "/statusline":
                sess["status"] = data
            else:
                event = safe_event(data)
                sess["last_event"] = event
                sess["events"].append(event)
                sess["events"] = sess["events"][-50:]

        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path == "/sessions":
            with LOCK:
                lst = build_sessions_list()
            self.send_json(lst)
            return

        if parsed.path == "/data.json":
            sid = (qs.get("session") or [None])[0]
            if not sid:
                with LOCK:
                    sid = ACTIVE_SESSION or (list(SESSIONS.keys())[0] if SESSIONS else None)
            if not sid or sid not in SESSIONS:
                self.send_json({"error": "no session"}, 404)
                return
            self.send_json(build_data(sid))
            return

        if parsed.path == "/state.json":
            with LOCK:
                self.send_json({"sessions": SESSIONS, "active": ACTIVE_SESSION})
            return

        # default: serve the page
        body = PAGE_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ── hooks installer ───────────────────────────────────────────────────────────

def do_install_hooks(target_dir):
    import sys
    settings_dir  = os.path.join(target_dir, ".claude")
    settings_path = os.path.join(settings_dir, "settings.json")

    hook_url    = f"http://127.0.0.1:{PORT}/hook"
    status_url  = f"http://127.0.0.1:{PORT}/statusline"
    curl_hook   = f"curl -s -X POST {hook_url} -H 'Content-Type: application/json' -d @-"
    curl_status = f"curl -s -X POST {status_url} -H 'Content-Type: application/json' -d @-"

    hook_events = [
        "SessionStart", "SessionEnd",
        "UserPromptSubmit",
        "PreToolUse", "PostToolUse", "PostToolUseFailure",
        "PermissionRequest",
        "Notification",
        "SubagentStart", "SubagentStop",
        "PreCompact", "PostCompact",
        "Stop", "StopFailure",
    ]

    try:
        with open(settings_path) as f:
            settings = json.load(f)
    except FileNotFoundError:
        settings = {}
    except json.JSONDecodeError as e:
        print(f"错误：{settings_path} JSON 格式无效：{e}", file=sys.stderr)
        sys.exit(1)

    if "hooks" not in settings:
        settings["hooks"] = {}

    for event in hook_events:
        settings["hooks"][event] = [
            {"hooks": [{"type": "command", "command": curl_hook}]}
        ]

    settings["statusLine"] = {"type": "command", "command": curl_status}

    os.makedirs(settings_dir, exist_ok=True)
    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)

    print(f"已写入 {settings_path}")
    print(f"  钩子事件：{len(hook_events)} 个")
    print(f"  状态行：{curl_status}")
    print(f"  监控地址：http://127.0.0.1:{PORT}/")


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Claude Code Kindle Monitor")
    parser.add_argument(
        "--install-hooks",
        metavar="DIR",
        nargs="?",
        const=".",
        dest="install_hooks",
        help="在 DIR/.claude/settings.json 中写入 Claude Code hooks（默认当前目录）",
    )
    args = parser.parse_args()

    if args.install_hooks is not None:
        do_install_hooks(args.install_hooks)
        sys.exit(0)

    import socket
    try:
        local_ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        local_ip = "127.0.0.1"
    print(f"Claude Kindle Monitor 已启动")
    print(f"  本机:    http://127.0.0.1:{PORT}/")
    print(f"  Kindle:  http://{local_ip}:{PORT}/")
    print(f"  调试:    http://127.0.0.1:{PORT}/state.json")
    print(f"  安装钩子: python server.py --install-hooks [DIR]")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
