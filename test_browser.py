# -*- coding: utf-8 -*-
"""浏览器级双端协作验证（iframe 方案）
   关键：主脚本用 const 声明 COLLAB/ST，词法绑定不会挂到 window，
   所以 iframe 里注入一行桥接 window.__api = {COLLAB, ST, toggleSpot, ...}。"""
import json
import os
import re
import subprocess
import time

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
BASE = r"D:\d\sichuan-route-ppt"
D = os.path.join(BASE, "deploy", "static")
PORT = 8080
ROOM = "IF" + str(int(time.time()) % 100000)

BRIDGE = ("<script>window.__api={COLLAB:COLLAB,ST:ST,toggleSpot:toggleSpot,"
          "reportOp:reportOp,save:save,renderDIY:renderDIY};</script>")

HARNESS = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>wait</title>
<style>html,body{margin:0}iframe{width:390px;height:740px;border:1px solid #ccc}</style>
</head><body>
<iframe id="fa"></iframe><iframe id="fb" style="display:none"></iframe>
<script>
var ROOM = '%(room)s';
var A = document.getElementById('fa'), B = document.getElementById('fb');
A.src = '/_t_a.html?room=' + ROOM + '&name=' + encodeURIComponent('李昊然') + '#diy';
function api(f){ return f.contentWindow.__api; }
function snap(f, tag){
  try {
    var a = api(f);
    if (!a) return { err: 'no api' };
    return {
      mode: a.COLLAB.mode, me: a.COLLAB.name,
      peers: a.COLLAB.peers.map(function(p){ return p.name; }),
      members: a.COLLAB.peers.length,
      picked: a.ST.picked.slice(), days: a.ST.days
    };
  } catch(e){ return { err: String(e) }; }
}
var out = { steps: [] };
setTimeout(function(){ out.A0 = snap(A); out.steps.push('A 入房'); }, 2600);
setTimeout(function(){
  B.src = '/_t_b.html?room=' + ROOM + '&name=' + encodeURIComponent('陈墨') + '#diy';
  out.steps.push('启动 B');
}, 3300);
setTimeout(function(){
  try {
    var a = api(B);
    if (a.ST.picked.indexOf('panda') < 0) a.toggleSpot('panda');
    a.ST.days = 7; a.reportOp('set', 'days:7'); a.save(); a.renderDIY();
    out.steps.push('B 加景点+改天数');
  } catch(e){ out.steps.push('ERR B 操作 ' + e.message); }
}, 5400);
setTimeout(function(){
  out.B1 = snap(B); out.A1 = snap(A); out.room = ROOM;
  document.title = 'R' + JSON.stringify(out);
}, 7200);
</script></body></html>"""


def main():
    html = open(os.path.join(D, "app.html"), encoding="utf-8").read()
    for tag in ("a", "b"):
        open(os.path.join(D, "_t_%s.html" % tag), "w", encoding="utf-8",
             newline="").write(html.replace("</body>", BRIDGE + "</body>"))
    open(os.path.join(D, "_iframes.html"), "w", encoding="utf-8",
         newline="").write(HARNESS % {"room": ROOM})

    r = subprocess.run(
        [EDGE, "--headless=new", "--disable-gpu", "--no-sandbox",
         "--virtual-time-budget=13000", "--window-size=900,820", "--dump-dom",
         "http://127.0.0.1:%d/_iframes.html" % PORT],
        capture_output=True, timeout=240)
    dom = (r.stdout or b"").decode("utf-8", "ignore")
    m = re.search(r"<title>R(.*?)</title>", dom, re.S)
    if not m:
        print("未取到结果，DOM 长度", len(dom))
        return
    d = json.loads(m.group(1))

    print("房间:", d.get("room"))
    print("步骤:", " | ".join(d.get("steps", [])))
    print("\n  A 加入后 :", d.get("A0"))
    print("  B 改动后 :", d.get("B1"))
    print("  A 收到后 :", d.get("A1"))

    A0, A1, B1 = d.get("A0") or {}, d.get("A1") or {}, d.get("B1") or {}
    PASS, FAIL = [], []

    def ok(n, c, e=""):
        (PASS if c else FAIL).append(n)
        print("  %s %s%s" % ("PASS" if c else "FAIL", n, ("  [" + str(e) + "]") if e else ""))

    print("\n=== 结果 ===")
    ok("A 建立实时连接", A0.get("mode") == "ws", A0.get("mode"))
    ok("B 建立实时连接", B1.get("mode") == "ws", B1.get("mode"))
    ok("A 最终看到 2 人在线", A1.get("members") == 2, A1.get("members"))
    ok("B 看到 2 人在线", B1.get("members") == 2, B1.get("members"))
    ok("A 名单含陈墨", "陈墨" in (A1.get("peers") or []), A1.get("peers"))
    ok("B 名单含李昊然", "李昊然" in (B1.get("peers") or []), B1.get("peers"))
    ok("B 加的景点同步到 A", "panda" in (A1.get("picked") or []), A1.get("picked"))
    ok("B 改的天数同步到 A", A1.get("days") == 7, A1.get("days"))
    ok("B 自己也看到新景点", "panda" in (B1.get("picked") or []), B1.get("picked"))

    print("\n共%d项 通过%d" % (len(PASS) + len(FAIL), len(PASS)))
    if FAIL:
        print("失败:", FAIL)


main()
