# -*- coding: utf-8 -*-
"""跨公网端到端验证（协议级）

   背景：本环境的 Edge 子进程没有外网权限（连 example.com 都 Failed to fetch），
   所以浏览器级跨网测试在这里跑不了。改为在 Python 里**逐条复刻浏览器客户端的
   协议交互**，对真实的 Render 实例验证：
     1. 三个客户端各自建立 WSS 长连接
     2. 主持人设房间密码
     3. 用 view: 前缀加入 → 服务端判为只读
     4. 只读成员发 add → 服务端拒绝（对应前端 toggleSpot 被拦）
     5. 编辑成员 add/set → 服务端广播 → 主持人端状态更新
   连的地址、查询参数格式、消息体结构都取自 app.html 的实际实现。
"""
import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import time
import urllib.parse
import urllib.request

HOST = "shuji-collab.onrender.com"
PORT = 443
ROOM = "XN" + base64.b16encode(os.urandom(3)).decode()
PW = "crossnet888"
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

PASS, FAIL = [], []


def ok(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  [" + str(extra) + "]") if extra else ""))


class WS(object):
    """最小 WebSocket 客户端（与 test_collab.py 一致，走 TLS）"""

    def __init__(self, room, name, pw=None, token=None, timeout=25):
        raw = socket.create_connection((HOST, PORT), timeout=timeout)
        ctx = ssl.create_default_context()
        self.s = ctx.wrap_socket(raw, server_hostname=HOST)
        key = base64.b64encode(os.urandom(16)).decode()
        # 查询参数格式完全照抄 app.html 的 wsURL()
        q = {"room": room, "name": name,
             "cid": "c" + base64.b16encode(os.urandom(4)).decode().lower()}
        if token:
            q["token"] = token
        elif pw:
            q["pw"] = pw
        path = "/?" + urllib.parse.urlencode(q)
        req = ("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\nOrigin: https://%s\r\n\r\n"
               % (path, HOST, key, HOST))
        self.s.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            c = self.s.recv(1)
            if not c:
                raise IOError("握手时连接关闭")
            buf += c
        head = buf.split(b"\r\n\r\n")[0].decode("latin-1")
        if "101" not in head.split("\r\n")[0]:
            raise IOError("握手失败: " + head.split("\r\n")[0])
        acc = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        if acc not in head:
            raise IOError("Sec-WebSocket-Accept 不匹配")
        self.buf = b""

    def _recv_exact(self, n):
        while len(self.buf) < n:
            c = self.s.recv(65536)
            if not c:
                raise IOError("连接关闭")
            self.buf += c
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, obj):
        payload = json.dumps(obj, ensure_ascii=False).encode()
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            head = struct.pack("!BB", 0x81, 0x80 | n)
        elif n < 65536:
            head = struct.pack("!BBH", 0x81, 0x80 | 126, n)
        else:
            head = struct.pack("!BBQ", 0x81, 0x80 | 127, n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.s.sendall(head + mask + masked)

    def recv(self, timeout=15):
        self.s.settimeout(timeout)
        try:
            b1, b2 = self._recv_exact(2)
            op = b1 & 0x0F
            ln = b2 & 0x7F
            if ln == 126:
                ln = struct.unpack("!H", self._recv_exact(2))[0]
            elif ln == 127:
                ln = struct.unpack("!Q", self._recv_exact(8))[0]
            data = self._recv_exact(ln)
            if op == 0x8:
                return None
            if op == 0x9:
                self.s.sendall(struct.pack("!BB", 0x8A, 0x80 | len(data)) +
                               os.urandom(4) +
                               bytes(x ^ os.urandom(1)[0] for x in data))
                return self.recv(timeout)
            return json.loads(data.decode("utf-8"))
        except socket.timeout:
            return None

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass


def wait_for(ws, want, tries=30, timeout=6):
    """等到某个类型的消息；顺手记住最后一条 state"""
    last = None
    for _ in range(tries):
        m = ws.recv(timeout)
        if not m:
            continue
        last = m
        if m.get("t") == want:
            return m
    return last


print("=" * 62)
print("跨公网端到端验证")
print("  后端   : https://%s" % HOST)
print("  房间   : %s   密码: %s" % (ROOM, PW))
print("=" * 62)

# --- 1. 三个客户端连上 ---
print("\n=== 1. 建立 WSS 长连接 ===")
t0 = time.time()
H = WS(ROOM, "李昊然")
wh = H.recv(25)
ok("主持人 WSS 握手成功", bool(wh and wh.get("t") == "welcome"),
   "%.0fms  %s" % ((time.time() - t0) * 1000, (wh or {}).get("t")))
ok("首个加入者成为主持人", (wh or {}).get("role") == "host", (wh or {}).get("role"))
HTOKEN = (wh or {}).get("token")
ok("服务端下发令牌", bool(HTOKEN), (HTOKEN or "")[:8])

# --- 2. 设密码 ---
print("\n=== 2. 主持人设置房间密码 ===")
H.send({"t": "lock", "pw": "host:" + PW})
lk = wait_for(H, "locked", tries=12)
ok("密码已生效", bool(lk and lk.get("locked")), (lk or {}).get("locked"))

# --- 3. 错误密码被拒 ---
print("\n=== 3. 密码校验 ===")
bad = WS(ROOM, "闯入者", pw="wrongpass")
bm = bad.recv(15)
ok("错误密码被拒", bool(bm and bm.get("t") == "denied"), (bm or {}).get("msg"))
bad.close()

# --- 4. view: 前缀 → 只读 ---
print("\n=== 4. 角色前缀 ===")
V = WS(ROOM, "只读同学", pw="view:" + PW)
wv = V.recv(20)
ok("只读成员加入成功", bool(wv and wv.get("t") == "welcome"), (wv or {}).get("t"))
ok("view: 前缀判为只读", (wv or {}).get("role") == "view", (wv or {}).get("role"))

E = WS(ROOM, "编辑同学", pw=PW)
we = E.recv(20)
ok("无前缀默认可编辑", (we or {}).get("role") == "edit", (we or {}).get("role"))

# 让 H 收一下 peers
time.sleep(1.0)
print("\n=== 5. 在线成员互相可见 ===")
# peers 是陆续广播的（2 人 → 3 人），取最后一次而不是第一帧
m = None
for _ in range(10):
    x = H.recv(3)
    if x and x.get("t") == "peers":
        m = x
        if len(x.get("peers") or []) >= 3:
            break
# 权威核对：直接问服务端
try:
    _u = "https://%s/api/room?room=%s" % (HOST, ROOM)
    _j = json.loads(urllib.request.urlopen(_u, timeout=25).read().decode())
    _n = len(_j.get("peers") or [])
except Exception:
    _n = -1
ok("服务端在线名单为 3 人", _n == 3, "api/room 返回 %s" % _n)
ok("广播里也看到了 3 人",
   bool(m) and len(m.get("peers") or []) == 3,
   len((m or {}).get("peers") or []))

# --- 6. 只读越权 ---
print("\n=== 6. 只读成员越权被服务端拒绝 ===")
V.send({"t": "op", "op": "add", "id": "panda"})
dv = wait_for(V, "denied", tries=10)
ok("只读 add 被拒", bool(dv and dv.get("t") == "denied"),
   (dv or {}).get("msg"))

H.send({"t": "op", "op": "add", "id": "panda"})
st = wait_for(H, "state", tries=10)
ok("编辑/主持 add 成功", bool(st and "panda" in (st.get("st", {}).get("picked") or [])),
   (st or {}).get("st", {}).get("picked"))

# --- 7. 跨公网同步 ---
print("\n=== 7. 改动跨公网广播 ===")
E.send({"t": "op", "op": "add", "id": "jiajin"})
E.send({"t": "op", "op": "set", "id": "days:7"})

got_h = got_v = None
for _ in range(25):
    mh = H.recv(3)
    if mh and mh.get("t") == "state":
        got_h = mh
    mv = V.recv(3)
    if mv and mv.get("t") == "state":
        got_v = mv
    if got_h and got_v and "jiajin" in (got_h["st"]["picked"]) \
       and got_h["st"].get("days") == 7:
        break

ok("主持人收到编辑的改动", bool(got_h and "jiajin" in (got_h.get("st", {}).get("picked") or [])),
   (got_h or {}).get("st", {}).get("picked"))
ok("只读成员也收到改动", bool(got_v and "jiajin" in (got_v.get("st", {}).get("picked") or [])),
   (got_v or {}).get("st", {}).get("picked"))
ok("天数同步为 7", bool(got_h and got_h.get("st", {}).get("days") == 7),
   (got_h or {}).get("st", {}).get("days"))

# --- 8. 令牌免密重连 ---
print("\n=== 8. 令牌免密重连（换网络后恢复会话）===")
V.close()
time.sleep(0.8)
V2 = WS(ROOM, "只读同学", token=(wv or {}).get("token"))
w2 = V2.recv(20)
ok("凭令牌免密重连", bool(w2 and w2.get("t") == "welcome"), (w2 or {}).get("t"))
ok("令牌恢复原角色=只读", (w2 or {}).get("role") == "view", (w2 or {}).get("role"))

# --- 9. HTTP 兜底通道 ---
print("\n=== 9. HTTP 轮询兜底（SSE/WS 被墙时用）===")
u = "https://%s/api/auth?room=%s&pw=%s" % (HOST, ROOM, urllib.parse.quote(PW))
try:
    j = json.loads(urllib.request.urlopen(u, timeout=25).read().decode())
    ok("/api/auth 换令牌", bool(j.get("token")), j.get("role"))
except Exception as ex:
    ok("/api/auth 换令牌", False, str(ex))

u2 = "https://%s/api/poll?room=%s&v=0" % (HOST, ROOM)
try:
    j2 = json.loads(urllib.request.urlopen(u2, timeout=25).read().decode())
    ok("轮询能读到同一份状态",
       "jiajin" in (j2.get("st", {}).get("picked") or []),
       j2.get("st", {}).get("picked"))
except Exception as ex:
    ok("轮询读取", False, str(ex))

for w in (H, E, V2):
    w.close()

print("\n共%d项 通过%d" % (len(PASS) + len(FAIL), len(PASS)))
if FAIL:
    print("失败:", FAIL)
