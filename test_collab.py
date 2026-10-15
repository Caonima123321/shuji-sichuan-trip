# -*- coding: utf-8 -*-
"""协作后端自测：用标准库手写 WebSocket 客户端，验证
   1) 握手（Sec-WebSocket-Accept 是否正确）
   2) 双客户端能否互相收到状态广播
   3) 操作是否被权威应用（add/del/set/check）
   4) HTTP 轮询降级是否可用
   5) 落盘 rooms.json
"""
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HOST = "127.0.0.1"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

PASS, FAIL = [], []


def ok(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  [" + str(extra) + "]") if extra else ""))


class WS(object):
    def __init__(self, room, name, cid=None, token=None, pw=None):
        self.s = socket.create_connection((HOST, PORT), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        q = "room=%s&name=%s" % (room, urllib.parse.quote(name))
        if cid:
            q += "&cid=" + cid
        if token:
            q += "&token=" + token
        if pw:
            q += "&pw=" + urllib.parse.quote(pw)
        req = ("GET /?%s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n" % (q, HOST, PORT, key))
        self.s.sendall(req.encode())
        # 读握手响应
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.s.recv(1)
        self.head = buf.decode("latin-1")
        expect = base64.b64encode(
            hashlib.sha1((key + GUID).encode()).digest()).decode()
        self.accept_ok = ("sec-websocket-accept: " + expect).lower() in self.head.lower()
        self.buf = b""

    def send(self, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        n = len(data)
        head = bytearray([0x81])
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126); head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127); head += struct.pack(">Q", n)
        mask = os.urandom(4)
        head += mask
        self.s.sendall(bytes(head) + bytes(c ^ mask[i % 4] for i, c in enumerate(data)))

    def _need(self, n):
        while len(self.buf) < n:
            chunk = self.s.recv(4096)
            if not chunk:
                raise ConnectionError("closed")
            self.buf += chunk

    def recv(self, timeout=4.0):
        self.s.settimeout(timeout)
        try:
            self._need(2)
            b1, b2 = self.buf[0], self.buf[1]
            op = b1 & 0x0F
            n = b2 & 0x7F
            off = 2
            if n == 126:
                self._need(4); n = struct.unpack(">H", self.buf[2:4])[0]; off = 4
            elif n == 127:
                self._need(10); n = struct.unpack(">Q", self.buf[2:10])[0]; off = 10
            self._need(off + n)
            payload = self.buf[off:off + n]
            self.buf = self.buf[off + n:]
            if op == 9:
                return self.recv(timeout)
            if op != 1:
                return None
            return json.loads(payload.decode("utf-8"))
        except socket.timeout:
            return None

    def drain(self):
        out = []
        while True:
            m = self.recv(0.6)
            if m is None:
                break
            out.append(m)
        return out

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass


ROOM = "T" + base64.b16encode(os.urandom(3)).decode()

print("=== 1. 握手 ===")
a = WS(ROOM, "李昊然")
ok("WebSocket 握手成功（Accept 校验通过）", a.accept_ok,
   a.head.split("\r\n")[0].strip() if a.head else "")

w = a.recv()
ok("收到 welcome", bool(w and w.get("t") == "welcome"), w and w.get("t"))
ok("welcome 带房间号", bool(w and w.get("room") == ROOM))
ok("welcome 带初始状态", bool(w and isinstance(w.get("st"), dict)))
me = w.get("cid") if w else None

print("\n=== 2. 第二个客户端加入并互相可见 ===")
b = WS(ROOM, "陈墨")
wb = b.recv()
ok("第二个客户端收到 welcome", bool(wb and wb.get("t") == "welcome"))
peers = (wb or {}).get("peers") or []
names = sorted(p["name"] for p in peers)
ok("双方都在在线名单里", names == ["李昊然", "陈墨"], names)

pm = a.recv(2.0)
ok("第一个客户端收到 peers 更新", bool(pm and pm.get("t") == "peers"),
   pm and pm.get("t"))

print("\n=== 3. 操作权威应用 + 广播 ===")
for m in a.drain():
    pass
a.send({"t": "op", "op": "add", "id": "dujiangyan"})
r1 = a.recv()
ok("操作方收到状态广播", bool(r1 and r1.get("t") == "state"),
   r1 and (r1.get("op"), r1.get("id")))
ok("状态里已包含该景点", bool(r1 and "dujiangyan" in r1["st"]["picked"]),
   r1 and r1["st"]["picked"])
ok("版本号自增", bool(r1 and r1.get("v", 0) >= 1), r1 and r1.get("v"))

r1b = b.recv()
ok("另一客户端也收到同一次变更", bool(r1b and r1b.get("t") == "state"))
ok("另一客户端看到相同景点", bool(r1b and "dujiangyan" in r1b["st"]["picked"]))

print("\n=== 4. 并发添加（服务端权威，不丢操作）===")
b.drain()
a.send({"t": "op", "op": "add", "id": "sgn_czg"})
b.send({"t": "op", "op": "add", "id": "dangling"})
time.sleep(1.0)
final = None
for _ in range(6):
    m = a.recv(1.5)
    if m and m.get("t") == "state":
        final = m["st"]
ok("两人同时加的两个景点都保留",
   bool(final and {"dujiangyan", "sgn_czg", "dangling"} <= set(final["picked"])),
   final and sorted(final["picked"]))

print("\n=== 5. 删除 / 天数 / 清单 ===")
a.drain(); b.drain()
a.send({"t": "op", "op": "del", "id": "sgn_czg"})
a.recv()
a.send({"t": "op", "op": "set", "id": "days:7"})
r = a.recv()
ok("天数改为 7", bool(r and r["st"]["days"] == 7), r and r["st"]["days"])
a.send({"t": "op", "op": "check", "id": "0-1:1"})
r = a.recv()
ok("清单勾选已同步", bool(r and r["st"]["checks"].get("0-1") is True),
   r and r["st"]["checks"])
a.send({"t": "op", "op": "del", "id": "sgn_czg"})
r = a.recv()
ok("景点已删除", bool(r and "sgn_czg" not in r["st"]["picked"]),
   r and r["st"]["picked"])

print("\n=== 6. HTTP 轮询降级 ===")
try:
    body = json.dumps({"room": ROOM, "who": "王凯旋", "op": "add",
                       "id": "jiajin"}).encode()
    req = urllib.request.Request(
        "http://%s:%d/api/op" % (HOST, PORT), data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        j = json.loads(resp.read().decode())
    ok("HTTP POST 操作成功", bool(j.get("ok")), j)
    with urllib.request.urlopen(
            "http://%s:%d/api/room?room=%s" % (HOST, PORT, ROOM), timeout=8) as resp:
        j2 = json.loads(resp.read().decode())
    ok("HTTP GET 读到同一份状态", "jiajin" in j2["st"]["picked"], j2["st"]["picked"])
    r = a.recv(3.0)
    ok("HTTP 提交也会推给 WebSocket 客户端",
       bool(r and r.get("t") == "state" and "jiajin" in r["st"]["picked"]))
except Exception as e:
    ok("HTTP 轮询降级", False, "%s: %s" % (type(e).__name__, e))

print("\n=== 7. 落盘 ===")
time.sleep(0.6)
p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rooms.json")
ok("rooms.json 已生成", os.path.exists(p))
if os.path.exists(p):
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    ok("房间已持久化", ROOM in data, list(data)[:4])
    ok("持久化内容含最新操作",
       bool(data.get(ROOM) and "jiajin" in data[ROOM]["st"]["picked"]))

print("\n=== 8. 断线清理 ===")
b.close()
time.sleep(1.2)
found = False
for _ in range(5):
    m = a.recv(1.5)
    if m and m.get("t") == "peers":
        if len(m["peers"]) == 1:
            found = True
            break
ok("断开后在线名单只剩 1 人", found)

a.close()

# ==================================================================
# 9. 房间密码与角色权限
# ==================================================================
print("\n=== 9. 房间密码与令牌 ===")
LOCKROOM = "L" + base64.b16encode(os.urandom(3)).decode()

# 第一个进无密码房间的人自动成为主持人（保持向后兼容）
h = WS(LOCKROOM, "主持人")
wh = h.recv()
ok("无密码房间可自由加入", bool(wh and wh.get("t") == "welcome"))
ok("首个加入者自动成为主持人", (wh or {}).get("role") == "host",
   (wh or {}).get("role"))
HOST_TOKEN = (wh or {}).get("token")
ok("下发令牌", bool(HOST_TOKEN), (HOST_TOKEN or "")[:8])

# 主持人设密码
h.send({"t": "lock", "pw": "host:secret123"})
r = h.recv()
ok("主持人可设置房间密码", bool(r and r.get("t") in ("notice", "locked")),
   r and r.get("t"))

time.sleep(0.4)

# 无密码加入应被拒
bad = WS(LOCKROOM, "闯入者")
d = bad.recv(3.0)
ok("无密码被拒绝加入", bool(d and d.get("t") == "denied"), d)
bad.close()

# 错误密码应被拒
bad2 = WS(LOCKROOM, "猜密码", pw="wrongpass")
d2 = bad2.recv(3.0)
ok("错误密码被拒绝", bool(d2 and d2.get("t") == "denied"), d2)
bad2.close()

# 正确密码加入
v = WS(LOCKROOM, "只读同学", pw="view:secret123")
wv = v.recv()
ok("正确密码可加入", bool(wv and wv.get("t") == "welcome"), wv and wv.get("t"))
ok("密码前缀决定角色=只读", (wv or {}).get("role") == "view", (wv or {}).get("role"))

e = WS(LOCKROOM, "编辑同学", pw="secret123")
we = e.recv()
ok("无前缀密码默认给编辑权限", (we or {}).get("role") == "edit",
   (we or {}).get("role"))

# 用令牌重连（免密）
tok = (wv or {}).get("token")
h.drain()
v2 = WS(LOCKROOM, "只读同学", token=tok)
w2 = v2.recv()
ok("凭令牌免密重连", bool(w2 and w2.get("t") == "welcome"), w2 and w2.get("t"))
ok("令牌恢复原角色", (w2 or {}).get("role") == "view", (w2 or {}).get("role"))
v2.close()

print("\n=== 10. 越权操作被服务端拒绝 ===")
v.drain()
v.send({"t": "op", "op": "add", "id": "panda"})
res = v.recv(3.0)
ok("只读成员加景点被拒", bool(res and res.get("t") == "denied"), res)
time.sleep(0.3)
# 确认真的没写进去
vr = urllib.request.urlopen("http://%s:%d/api/room?room=%s" % (HOST, PORT, LOCKROOM),
                            timeout=8)
st = json.loads(vr.read().decode())["st"]
ok("被拒的操作没有落库", "panda" not in st["picked"], st["picked"])

e.drain()
e.send({"t": "op", "op": "add", "id": "qingcheng"})
re_ = e.recv(3.0)
ok("编辑成员加景点成功", bool(re_ and re_.get("t") == "state"), re_ and re_.get("t"))

# 编辑不能清空（clear 是主持人专属）
e.drain()
e.send({"t": "op", "op": "clear"})
rc = e.recv(3.0)
ok("编辑成员清空行程被拒", bool(rc and rc.get("t") == "denied"), rc)

# 编辑不能改密码
e.drain()
e.send({"t": "lock", "pw": "hacked"})
rl = e.recv(3.0)
ok("非主持人改密码被拒", bool(rl and rl.get("t") == "denied"), rl)

# HTTP 越权也要拦
try:
    body = json.dumps({"room": LOCKROOM, "who": "坏人", "op": "add",
                       "id": "jiajin", "token": tok}).encode()
    req = urllib.request.Request("http://%s:%d/api/op" % (HOST, PORT), data=body,
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=8)
    ok("HTTP 只读越权被拒", False, "居然成功了")
except urllib.error.HTTPError as ex:
    ok("HTTP 只读越权被拒", ex.code == 403, "HTTP %d" % ex.code)
except Exception as ex:
    ok("HTTP 只读越权被拒", False, str(ex))

print("\n=== 11. /api/auth 换取令牌 ===")
try:
    u = "http://%s:%d/api/auth?room=%s&pw=%s" % (HOST, PORT, LOCKROOM,
                                                 urllib.parse.quote("view:secret123"))
    j = json.loads(urllib.request.urlopen(u, timeout=8).read().decode())
    ok("auth 接口返回令牌与角色", bool(j.get("ok") and j.get("token")),
       (j.get("role"), bool(j.get("token"))))
    u2 = "http://%s:%d/api/auth?room=%s&pw=nope" % (HOST, PORT, LOCKROOM)
    try:
        urllib.request.urlopen(u2, timeout=8)
        ok("auth 错误密码返回 401", False, "居然成功了")
    except urllib.error.HTTPError as ex:
        ok("auth 错误密码返回 401", ex.code == 401, "HTTP %d" % ex.code)
except Exception as ex:
    ok("/api/auth", False, str(ex))

# ==================================================================
# 12. SSE 通道
# ==================================================================
print("\n=== 12. SSE 通道 ===")
SSEROOM = "S" + base64.b16encode(os.urandom(3)).decode()
sse_lines = []
sse_err = []


def sse_reader():
    try:
        # 姓名必须做百分号编码：URL 里直接塞中文，urllib 会抛 UnicodeEncodeError
        qs = urllib.parse.urlencode({"room": SSEROOM, "name": "SSE同学"})
        req = urllib.request.Request(
            "http://%s:%d/api/stream?%s" % (HOST, PORT, qs))
        with urllib.request.urlopen(req, timeout=25) as resp:
            ct = resp.headers.get("Content-Type", "")
            sse_lines.append("CT:" + ct)
            buf = b""
            t0 = time.time()
            while time.time() - t0 < 18:
                chunk = resp.read(1)
                if not chunk:
                    break
                buf += chunk
                while b"\n\n" in buf:
                    raw, buf = buf.split(b"\n\n", 1)
                    raw = raw.strip()
                    if raw.startswith(b"data: "):
                        sse_lines.append(raw[6:].decode("utf-8", "ignore"))
    except Exception as ex:
        sse_err.append("%s: %s" % (type(ex).__name__, ex))


th = threading.Thread(target=sse_reader, daemon=True)
th.start()
time.sleep(2.0)
first = None
for ln in sse_lines:
    if not ln.startswith("CT:"):
        first = ln
        break
ok("SSE 返回 text/event-stream",
   any(x.startswith("CT:text/event-stream") for x in sse_lines),
   [x for x in sse_lines if x.startswith("CT:")])
ok("SSE 首帧是 welcome", bool(first and '"welcome"' in first), (first or "")[:70])

# 通过 HTTP 改一次状态，看 SSE 是否收到推送
try:
    body = json.dumps({"room": SSEROOM, "who": "李昊然", "op": "add",
                       "id": "dangling"}).encode()
    req = urllib.request.Request("http://%s:%d/api/op" % (HOST, PORT), data=body,
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=8).read()
except Exception as ex:
    print("  (HTTP 提交异常:", ex, ")")

time.sleep(2.5)
pushed = [x for x in sse_lines if '"state"' in x]
ok("SSE 收到状态推送", len(pushed) > 0, "收到 %d 帧" % len(pushed))
ok("SSE 推送内容含新景点",
   any("dangling" in x for x in pushed), (pushed[0][:90] if pushed else ""))
ok("SSE 读取无异常", not sse_err, sse_err)

print("\n=== 13. 会话与密码持久化 ===")
time.sleep(0.5)
p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rooms.json")
try:
    data = json.load(open(p, encoding="utf-8"))
    ok("密码哈希已落盘", bool(data.get(LOCKROOM, {}).get("pw")),
       str(data.get(LOCKROOM, {}).get("pw"))[:16])
    ok("密码不以明文落盘", "secret123" not in json.dumps(data),
       "明文泄漏！" if "secret123" in json.dumps(data) else "OK")
except Exception as ex:
    ok("落盘检查", False, str(ex))

h.close(); v.close(); e.close()

print("\n共%d项 通过%d" % (len(PASS) + len(FAIL), len(PASS)))
if FAIL:
    print("失败:", FAIL)
    sys.exit(1)
