# -*- coding: utf-8 -*-
"""蜀迹 · 协作后端（纯 Python 标准库，无第三方依赖）

功能：
  - 静态托管 App（单文件版为主页）
  - WebSocket（RFC 6455 手写实现）推送共享路线状态
  - 断线时自动降级为 HTTP 轮询，保证弱网也能用
  - 房间状态落盘 rooms.json，重启不丢

协议（JSON 文本帧）：
  客户端 → 服务端
    {"t":"join","room":"ABCD","name":"李昊然","cid":"..."}
    {"t":"state","room":"..","st":{...},"base":3}
    {"t":"op","room":"..","op":"add","id":"panda","who":"李昊然"}
    {"t":"op","room":"..","op":"del","id":"panda","who":"..."}
    {"t":"ping"}
  服务端 → 客户端
    {"t":"welcome","cid":"..","st":{...},"v":3,"peers":[..],"log":[..]}
    {"t":"state","st":{...},"v":4,"from":"cid","who":"..","op":".."}
    {"t":"peers","peers":[..]}
    {"t":"log","log":[{t,who,op,id,v}]}
    {"t":"pong"}
"""
import base64
import hashlib
import http.server
import io
import json
import os
import queue
import socket
import socketserver
import struct
import sys
import threading
import time
import urllib.parse

# Windows 上 stdout 可能是 ascii，中文日志会抛 UnicodeEncodeError，
# 严重时会把 SSE 线程直接打死（异常发生在响应头写出之前）。
for _stream in ("stdout", "stderr"):
    try:
        getattr(sys, _stream).reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Render 会注入 PORT；本地默认 8080
PORT = int(os.environ.get("PORT") or os.environ.get("SHUJI_PORT") or "8080")
BASE = os.path.dirname(os.path.abspath(__file__))
# 静态前端目录（本地 share/ 与部署 static/ 两种布局都兼容）
ROOT = os.path.join(BASE, "static")
if not os.path.isdir(ROOT):
    ROOT = BASE
# Render 的文件系统是临时的；写不了就退化为纯内存（不影响实时协作）
STORE = os.environ.get("SHUJI_STORE") or os.path.join(BASE, "rooms.json")
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

LOCK = threading.RLock()
ROOMS = {}          # room -> {"st":dict, "v":int, "log":[...], "peers":{cid:{"name":..,"seen":..}}}
CLIENTS = {}        # cid -> Client
CLIENT_LOCK = threading.RLock()
MAX_LOG = 60
PEER_TTL = 45
SSE_TTL = 60          # SSE 连接最长存活秒数

# 角色权限：能做什么
ROLE_PERMS = {
    "host": {"add", "del", "set", "check", "sync", "clear", "kick", "lock"},
    "edit": {"add", "del", "set", "check", "sync"},
    "view": set(),
}
ROLE_NAME = {"host": "主持人", "edit": "可编辑", "view": "只读"}


def role_can(role, op):
    return op in ROLE_PERMS.get(role or "view", set())


def hash_pw(pw):
    return hashlib.sha256(("shuji::" + pw).encode("utf-8")).hexdigest()


def new_token():
    return base64.b16encode(os.urandom(12)).decode("ascii").lower()


# ==================================================================
# 房间状态
# ==================================================================
def load_rooms():
    global ROOMS
    if os.path.exists(STORE):
        try:
            with io.open(STORE, encoding="utf-8") as f:
                ROOMS = json.load(f)
            print("已载入房间 %d 个" % len(ROOMS))
        except Exception as e:
            print("rooms.json 读取失败，忽略：", e)
            ROOMS = {}


_SAVE_WARNED = [False]


def save_rooms():
    """落盘失败不影响协作：Render 等平台的容器文件系统是临时的"""
    try:
        tmp = STORE + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump(ROOMS, f, ensure_ascii=False)
        os.replace(tmp, STORE)
    except Exception as e:
        if not _SAVE_WARNED[0]:
            _SAVE_WARNED[0] = True
            print("提示：状态无法落盘（%s），已退化为纯内存模式。" % e)


def get_room(name):
    with LOCK:
        r = ROOMS.get(name)
        if r is None:
            r = {"st": {"picked": [], "days": 3, "ppl": 6, "checks": {}},
                 "v": 0, "log": [], "peers": {}, "pw": None, "tokens": {}}
            ROOMS[name] = r
        # 兼容旧数据
        r.setdefault("pw", None)
        r.setdefault("tokens", {})
        return r


def auth_room(room, token, password):
    """返回 (role, token, err)。

    房间密码是一个密码、三种角色。加入时写成 `角色:密码` 即可：
        host:abcd  → 主持人（可清空行程、可改密码）
        edit:abcd  → 可编辑
        view:abcd  → 只读
        abcd       → 默认给编辑权限（不写前缀时的兼容行为）

    注意：角色前缀只是「请求的角色」，不参与密码校验——
    否则 host:abcd 与 abcd 会被当成两个不同密码，同一房间没法给不同角色发密码。
    """
    r = get_room(room)
    with LOCK:
        if token and token in r["tokens"]:
            r["tokens"][token]["seen"] = time.time()
            return r["tokens"][token]["role"], token, None

        if not r["pw"]:
            # 房间还没设密码：第一个进来的是主持人，其余是编辑
            role = "host" if not r["peers"] else "edit"
            t = new_token()
            r["tokens"][t] = {"role": role, "seen": time.time()}
            return role, t, None

        if not password:
            return None, None, "该房间需要密码"

        # 拆出请求的角色，剩下的是真正的密码
        want, plain = "edit", password
        for k in ("host", "edit", "view"):
            if password.startswith(k + ":"):
                want, plain = k, password[len(k) + 1:]
                break
        if hash_pw(plain) != r["pw"]:
            return None, None, "密码错误"

        # 请求高权限的仍要验密码；只读是安全方向，允许自由降级
        role = want
        t = new_token()
        r["tokens"][t] = {"role": role, "seen": time.time()}
        return role, t, None


def strip_role(pw):
    """把 `角色:密码` 里的角色前缀剥掉，只留密码本体"""
    for k in ("host", "edit", "view"):
        if pw.startswith(k + ":"):
            return pw[len(k) + 1:]
    return pw


def set_room_pw(room, pw):
    """设置房间密码。这里也要剥前缀——否则主持人用 host:abcd 设密，
       存下的哈希是 'host:abcd'，而别人只会输入 'abcd'，永远对不上。"""
    r = get_room(room)
    with LOCK:
        r["pw"] = hash_pw(strip_role(pw)) if pw else None
        r["tokens"] = {}          # 改密后旧令牌全部失效
    save_rooms()


def blank_state(st):
    return {
        "picked": list(st.get("picked") or [])[:40],
        "days": max(1, min(30, int(st.get("days") or 3))),
        "ppl": max(1, min(60, int(st.get("ppl") or 6))),
        "checks": {str(k): bool(v) for k, v in (st.get("checks") or {}).items() if v},
    }


def push_log(room, who, op, sid=""):
    r = get_room(room)
    entry = {"t": time.strftime("%H:%M:%S"), "who": who or "匿名",
             "op": op, "id": sid, "v": r["v"]}
    r["log"].append(entry)
    del r["log"][:-MAX_LOG]
    return entry


def apply_op(room, cid, who, op, sid, role="edit"):
    """在服务端权威地应用一次操作，避免并发整表覆盖。
       role 由 auth_room 判定，越权直接拒绝。"""
    if not role_can(role, op):
        return None
    r = get_room(room)
    st = r["st"]
    picked = st.setdefault("picked", [])
    if op == "add":
        if sid and sid not in picked:
            picked.append(sid)
    elif op == "del":
        if sid in picked:
            picked.remove(sid)
    elif op == "clear":
        picked[:] = []
    elif op == "set":
        # sid = "days:7" / "ppl:6"
        k, _, val = sid.partition(":")
        if k in ("days", "ppl"):
            try:
                st[k] = int(val)
            except ValueError:
                pass
    elif op == "check":
        k, _, val = sid.partition(":")
        ch = st.setdefault("checks", {})
        if val == "1":
            ch[k] = True
        else:
            ch.pop(k, None)
    r["v"] += 1
    entry = push_log(room, who, op, sid)
    save_rooms()
    return entry


def sync_state(room, cid, who, st):
    """客户端整表同步（用于初次加入或冲突兜底）"""
    r = get_room(room)
    r["st"] = blank_state(st)
    r["v"] += 1
    entry = push_log(room, who, "sync", "")
    save_rooms()
    return entry


def alive_peers(room):
    r = get_room(room)
    now = time.time()
    dead = [c for c, p in r["peers"].items() if now - p.get("seen", 0) > PEER_TTL]
    for c in dead:
        r["peers"].pop(c, None)
    return [{"cid": c, "name": p.get("name") or "匿名",
             "role": p.get("role") or "edit"} for c, p in r["peers"].items()]


def room_public(room):
    """房间的对外信息（不含密码本身）"""
    r = get_room(room)
    with LOCK:
        return {"room": room, "v": r["v"], "locked": bool(r["pw"])}


# ==================================================================
# WebSocket 帧
# ==================================================================
def ws_encode(payload, opcode=1):
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    n = len(payload)
    head = bytearray([0x80 | opcode])
    if n < 126:
        head.append(n)
    elif n < 65536:
        head.append(126); head += struct.pack(">H", n)
    else:
        head.append(127); head += struct.pack(">Q", n)
    return bytes(head) + payload


def ws_read_exact(rfile, n):
    buf = b""
    while len(buf) < n:
        chunk = rfile.read(n - len(buf))
        if not chunk:
            raise ConnectionError("连接已关闭")
        buf += chunk
    return buf


def ws_decode(rfile):
    """读一帧，返回 (opcode, payload)。只处理未分片的常见情况。"""
    b1, b2 = ws_read_exact(rfile, 2)
    opcode = b1 & 0x0F
    masked = b2 & 0x80
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack(">H", ws_read_exact(rfile, 2))[0]
    elif n == 127:
        n = struct.unpack(">Q", ws_read_exact(rfile, 8))[0]
    if n > 4 * 1024 * 1024:
        raise ConnectionError("帧过大")
    mask = ws_read_exact(rfile, 4) if masked else b"\x00\x00\x00\x00"
    data = ws_read_exact(rfile, n) if n else b""
    if masked:
        data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
    return opcode, data


# ==================================================================
# 客户端
# ==================================================================
class Client(object):
    def __init__(self, conn, cid, room, name, role="edit"):
        self.conn = conn
        self.cid = cid
        self.room = room
        self.name = name
        self.role = role
        self.q = queue.Queue(maxsize=200)
        self.alive = True
        self.last_seen = time.time()

    def send(self, obj):
        try:
            self.q.put_nowait(obj)
        except queue.Full:
            pass


SSE_CLIENTS = []      # [(room, Queue)]


def broadcast(room, obj, except_cid=None):
    with CLIENT_LOCK:
        targets = [c for c in CLIENTS.values() if c.room == room and c.cid != except_cid]
    for c in targets:
        c.send(obj)
    # 同步推给 SSE 订阅者
    for r, qq in list(SSE_CLIENTS):
        if r == room:
            try:
                qq.put_nowait(obj)
            except queue.Full:
                pass


def writer_loop(cl):
    """把队列里的消息写成帧推给客户端"""
    try:
        while cl.alive:
            try:
                obj = cl.q.get(timeout=20)
            except queue.Empty:
                cl.conn.sendall(ws_encode("", opcode=9))   # ping 保活
                continue
            if obj is None:
                break
            cl.conn.sendall(ws_encode(json.dumps(obj, ensure_ascii=False)))
    except Exception:
        pass
    finally:
        cl.alive = False
        drop_client(cl)


def drop_client(cl):
    with CLIENT_LOCK:
        CLIENTS.pop(cl.cid, None)
    with LOCK:
        r = ROOMS.get(cl.room)
        if r:
            r["peers"].pop(cl.cid, None)
            left = [{"cid": c, "name": p.get("name") or "匿名"}
                    for c, p in r["peers"].items()]
        else:
            left = []
    broadcast(cl.room, {"t": "peers", "peers": left})
    try:
        cl.conn.close()
    except Exception:
        pass
    print("  - %s 离开房间 %s（在线 %d）" % (cl.name, cl.room, len(left)))


def handle_message(cl, msg):
    t = msg.get("t")
    cl.last_seen = time.time()
    with LOCK:
        r = ROOMS.get(cl.room)
        if r:
            p = r["peers"].get(cl.cid)
            if p:
                p["seen"] = cl.last_seen
                p["name"] = cl.name

    if t == "ping":
        cl.send({"t": "pong"})

    elif t == "state":
        # 整表同步
        if not role_can(cl.role, "sync"):
            cl.send({"t": "denied", "msg": "只读成员不能覆盖行程"})
            return
        entry = sync_state(cl.room, cl.cid, cl.name, msg.get("st") or {})
        r = get_room(cl.room)
        broadcast(cl.room, {"t": "state", "st": r["st"], "v": r["v"],
                            "from": cl.cid, "who": cl.name, "op": "sync"})
        cl.send({"t": "log", "log": r["log"]})

    elif t == "op":
        op = msg.get("op")
        sid = msg.get("id") or ""
        entry = apply_op(cl.room, cl.cid, cl.name, op, sid, cl.role)
        if entry is None:
            cl.send({"t": "denied", "msg": "你的权限（%s）不能执行该操作"
                     % ROLE_NAME.get(cl.role, cl.role)})
            return
        r = get_room(cl.room)
        broadcast(cl.room, {"t": "state", "st": r["st"], "v": r["v"],
                            "from": cl.cid, "who": cl.name, "op": op,
                            "id": sid, "t2": entry["t"]})

    elif t == "lock":
        # 只有主持人能设/改/清除房间密码
        if cl.role != "host":
            cl.send({"t": "denied", "msg": "只有主持人能修改房间密码"})
            return
        pw = str(msg.get("pw") or "")[:64]
        set_room_pw(cl.room, pw)
        broadcast(cl.room, {"t": "locked", "locked": bool(pw)})
        cl.send({"t": "notice", "msg": "房间密码已更新" if pw else "已关闭房间密码"})

    elif t == "hello":
        if msg.get("name"):
            cl.name = str(msg["name"])[:16]
            with LOCK:
                rr = ROOMS.get(cl.room)
                if rr and cl.cid in rr["peers"]:
                    rr["peers"][cl.cid]["name"] = cl.name
        broadcast(cl.room, {"t": "peers", "peers": alive_peers(cl.room)})


# ==================================================================
# HTTP + 握手
# ==================================================================
class Handler(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def guess_type(self, path):
        if path.endswith(".html"):
            return "text/html; charset=utf-8"
        if path.endswith((".jpg", ".jpeg")):
            return "image/jpeg"
        if path.endswith(".png"):
            return "image/png"
        if path.endswith(".json"):
            return "application/json; charset=utf-8"
        return super().guess_type(path)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        if "/api/" in (self.path or "") or "/health" in (self.path or ""):
            return
        sys.stdout.write("[%s] %s %s\n" % (time.strftime("%H:%M:%S"),
                                           self.client_address[0], fmt % args))
        sys.stdout.flush()

    # ---------- HTTP API（轮询降级用） ----------
    def json_out(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/health":
            b = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return

        if parsed.path == "/api/poll":
            room = (q.get("room") or ["LOBBY"])[0][:12].upper()
            since = int((q.get("v") or ["0"])[0] or 0)
            r = get_room(room)
            peers = alive_peers(room)
            if r["v"] > since:
                self.json_out({"v": r["v"], "st": r["st"], "peers": peers,
                               "log": r["log"][-12:]})
            else:
                self.json_out({"v": r["v"], "st": None, "peers": peers,
                               "log": r["log"][-12:]})
            return

        if parsed.path == "/api/stream":
            self.do_sse(q)
            return

        if parsed.path == "/api/auth":
            room = (q.get("room") or ["LOBBY"])[0][:12].upper()
            token = (q.get("token") or [""])[0][:64]
            pw = urllib.parse.unquote((q.get("pw") or q.get("password") or [""])[0],
                                         encoding="utf-8", errors="replace")[:64]
            role, token, err = auth_room(room, token, pw)
            r = get_room(room)
            if err:
                self.json_out({"ok": False, "err": err,
                               "locked": bool(r["pw"])}, 401)
                return
            self.json_out({"ok": True, "role": role, "token": token,
                           "locked": bool(r["pw"])})
            return

        if parsed.path == "/api/room":
            room = (q.get("room") or ["LOBBY"])[0][:12].upper()
            r = get_room(room)
            self.json_out({"room": room, "v": r["v"], "st": r["st"],
                           "peers": alive_peers(room), "log": r["log"][-20:]})
            return

        # ---------- WebSocket 握手 ----------
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self.do_ws(parsed, q)
            return

        super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/op":
            self.json_out({"err": "not found"}, 404)
            return
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            msg = json.loads(raw.decode("utf-8"))
        except Exception:
            self.json_out({"err": "bad json"}, 400)
            return
        room = str(msg.get("room") or "LOBBY")[:12].upper()
        who = str(msg.get("who") or "匿名")[:16]
        role, token, err = auth_room(room, str(msg.get("token") or ""),
                                     str(msg.get("pw") or ""))
        if err:
            self.json_out({"ok": False, "err": err}, 401)
            return
        if msg.get("op") == "sync":
            if not role_can(role, "sync"):
                self.json_out({"ok": False, "err": "只读成员不能覆盖行程"}, 403)
                return
            sync_state(room, "http", who, msg.get("st") or {})
        else:
            entry = apply_op(room, "http", who, msg.get("op"),
                             str(msg.get("id") or ""), role)
            if entry is None:
                self.json_out({"ok": False, "err": "你的权限（%s）不能执行该操作"
                               % ROLE_NAME.get(role, role)}, 403)
                return
        r = get_room(room)
        # 推给所有 WebSocket 在线者
        broadcast(room, {"t": "state", "st": r["st"], "v": r["v"],
                         "from": "http", "who": who, "op": msg.get("op")})
        self.json_out({"ok": True, "v": r["v"], "role": role})

    def do_sse(self, q):
        """SSE 通道：校园网/代理拦截 WebSocket 时的兜底推送"""
        room = (q.get("room") or ["LOBBY"])[0][:12].upper()
        token = (q.get("token") or [""])[0][:64]
        pw = urllib.parse.unquote((q.get("pw") or q.get("password") or [""])[0],
                                         encoding="utf-8", errors="replace")[:64]
        name = urllib.parse.unquote((q.get("name") or ["匿名"])[0], encoding="utf-8",
                                   errors="replace")[:16] or "匿名"

        role, token, err = auth_room(room, token, pw)
        if err:
            self.json_out({"ok": False, "err": err}, 401)
            return

        r = get_room(room)
        with LOCK:
            r["peers"][token] = {"name": name, "seen": time.time(), "role": role}
        qq = queue.Queue(maxsize=100)
        SSE_CLIENTS.append((room, qq))
        print("  ~ %s 通过 SSE 加入 %s（%s）" % (name, room, ROLE_NAME.get(role, role)))

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            hello = {"t": "welcome", "room": room, "st": r["st"], "v": r["v"],
                     "peers": alive_peers(room), "log": r["log"],
                     "role": role, "token": token, "locked": bool(r["pw"])}
            self.wfile.write(("data: " + json.dumps(hello, ensure_ascii=False) +
                              "\n\n").encode("utf-8"))
            self.wfile.flush()

            deadline = time.time() + SSE_TTL
            while time.time() < deadline:
                try:
                    msg = qq.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")       # 心跳，防代理超时
                    self.wfile.flush()
                    continue
                self.wfile.write(("data: " + json.dumps(msg, ensure_ascii=False) +
                                  "\n\n").encode("utf-8"))
                self.wfile.flush()
        except Exception:
            pass
        finally:
            try:
                SSE_CLIENTS.remove((room, qq))
            except ValueError:
                pass
            with LOCK:
                rr = ROOMS.get(room)
                if rr:
                    rr["peers"].pop(token, None)
            broadcast(room, {"t": "peers", "peers": alive_peers(room)})

    def do_ws(self, parsed, q):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self.send_error(400, "missing key")
            return
        accept = base64.b64encode(
            hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()

        room = (q.get("room") or ["LOBBY"])[0][:12].upper()
        cid = (q.get("cid") or [""])[0][:24] or base64.b16encode(
            os.urandom(8)).decode("ascii").lower()
        name = urllib.parse.unquote((q.get("name") or ["匿名"])[0], encoding="utf-8",
                                   errors="replace")[:16] or "匿名"
        token = (q.get("token") or [""])[0][:64]
        password = urllib.parse.unquote((q.get("pw") or q.get("password") or [""])[0],
                                         encoding="utf-8", errors="replace")[:64]

        role, token, err = auth_room(room, token, password)
        if err:
            # 认证失败：先把拒绝原因发出去，再正常关闭
            try:
                self.connection.sendall(ws_encode(json.dumps(
                    {"t": "denied", "msg": err}, ensure_ascii=False)))
            except Exception:
                pass
            self.connection.close()
            print("  ✗ %s 被拒绝加入 %s：%s" % (name, room, err))
            return

        conn = self.connection
        conn.settimeout(120)
        cl = Client(conn, cid, room, name, role)
        with CLIENT_LOCK:
            CLIENTS[cid] = cl
        r = get_room(room)
        with LOCK:
            r["peers"][cid] = {"name": name, "seen": time.time(), "role": role}
            peers = [{"cid": c, "name": p.get("name") or "匿名",
                      "role": p.get("role") or "edit"}
                     for c, p in r["peers"].items()]
            locked = bool(r["pw"])
        print("  + %s 加入房间 %s（%s，在线 %d）"
              % (name, room, ROLE_NAME.get(role, role), len(peers)))

        cl.send({"t": "welcome", "cid": cid, "room": room, "st": r["st"],
                 "v": r["v"], "peers": peers, "log": r["log"],
                 "role": role, "token": token, "locked": locked})
        broadcast(room, {"t": "peers", "peers": peers}, except_cid=cid)

        wt = threading.Thread(target=writer_loop, args=(cl,), daemon=True)
        wt.start()

        rfile = conn.makefile("rb")
        try:
            while cl.alive:
                opcode, data = ws_decode(rfile)
                if opcode == 8:          # close
                    break
                if opcode == 9:          # ping → pong
                    conn.sendall(ws_encode(data, opcode=10))
                    continue
                if opcode == 10:         # pong
                    cl.last_seen = time.time()
                    continue
                if opcode != 1:
                    continue
                try:
                    msg = json.loads(data.decode("utf-8"))
                except Exception:
                    continue
                if isinstance(msg, dict):
                    handle_message(cl, msg)
        except Exception:
            pass
        finally:
            cl.alive = False
            cl.send(None)
            drop_client(cl)

    # HTTP/1.1 下必须以 Content-Length 收尾；SimpleHTTPRequestHandler 已处理


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def lan_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.append(s.getsockname()[0]); s.close()
    except Exception:
        pass
    return ips


def selfcheck():
    """部署自检：确认静态资源齐全"""
    need = ["index.html"]
    for f in need:
        if not os.path.exists(os.path.join(ROOT, f)):
            print("⚠ 缺少 %s" % f)
    imgs = []
    ad = os.path.join(ROOT, "assets")
    if os.path.isdir(ad):
        imgs = [x for x in os.listdir(ad) if x.lower().endswith((".jpg", ".png"))]
    print("静态目录: %s" % ROOT)
    print("  页面: %s" % ", ".join(sorted(
        x for x in os.listdir(ROOT) if x.lower().endswith(".html"))))
    print("  图片: %d 张" % len(imgs))


if __name__ == "__main__":
    os.chdir(ROOT)
    load_rooms()
    selfcheck()
    with Server(("0.0.0.0", PORT), Handler) as httpd:
        print("已监听 0.0.0.0:%d" % PORT)
        for ip in lan_ips():
            print("  局域网访问 → http://%s:%d/" % (ip, PORT))
        print("  本机访问   → http://127.0.0.1:%d/" % PORT)
        print("协作房间   → http://<地址>:%d/?room=ABCD" % PORT)
        httpd.serve_forever()
