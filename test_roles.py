# -*- coding: utf-8 -*-
"""浏览器级验证（三客户端 · srcdoc + postMessage）

   走过的弯路，值得记下来：
   1) 父页面直接读 iframe.contentWindow.COLLAB 拿不到 —— 主脚本用 const 声明，
      词法绑定不挂 window。
   2) 注入桥接 window.__api 后，又要跟 iframe 加载时序赛跑，一次没就绪就全盘失败。
   3) 改成 iframe.src 指向服务端另一个注入过 agent 的页面，postMessage 依然不稳。

   最终方案：父页面 fetch 应用 HTML → 拼上 agent → 写进 iframe 的 srcdoc。
   内容本地生成、无额外网络往返，agent-up 握手确认就绪后再发指令。
"""
import json
import os
import re
import subprocess
import time

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
BASE = r"D:\d\sichuan-route-ppt"
D = os.path.join(BASE, "deploy", "static")
PORT = 8080
ROOM = "RB" + str(int(time.time()) % 100000)
PW = "sctrip888"

AGENT = """<script>
(function(){
  function snap(){
    try {
      return { ok:true, mode:COLLAB.mode, role:COLLAB.role, me:COLLAB.name,
               members:COLLAB.peers.length, picked:ST.picked.slice(),
               days:ST.days, locked:COLLAB.locked,
               roleTxt:ROLE_TXT[COLLAB.role] || COLLAB.role,
               hasToken: !!COLLAB.token };
    } catch(e){ return { ok:false, err:String(e) }; }
  }
  function reply(src, obj){ try { src.postMessage(obj, '*'); } catch(e){} }
  function connected(){
    try { return COLLAB.ws && COLLAB.ws.readyState === 1; } catch(e){ return false; }
  }
  /* join 后要等握手真正完成（readyState===1），
     只看 COLLAB.mode 会在 'connecting' 时就放行，指令打在未打开的连接上 */
  function afterReady(cb, n){
    n = n || 0;
    if (connected() || (n > 80 && COLLAB.mode === 'sse')){ cb(); return; }
    setTimeout(function(){ afterReady(cb, n + 1); }, 100);
  }
  window.addEventListener('message', function(ev){
    var d = ev.data || {}, src = ev.source;
    if (!d || !d.cmd) return;
    if (d.cmd === 'join'){
      try { connectCollab(d.room, d.name, d.pw || ''); }
      catch(e){ reply(src, {tag:d.tag, err:String(e)}); return; }
      afterReady(function(){ reply(src, {tag:d.tag, data:snap()}); });
      return;
    }
    if (d.cmd === 'snap'){ reply(src, {tag:d.tag, data:snap()}); return; }
    if (d.cmd === 'lock'){
      afterReady(function(){
        try { COLLAB.ws.send(JSON.stringify({t:'lock', pw:d.pw}));
              /* 等锁真的生效再回执，否则快照会取到 locked=false */
              (function waitLock(n){
                if (COLLAB.locked || n > 40){
                  reply(src, {tag:d.tag, data:snap()}); return;
                }
                setTimeout(function(){ waitLock(n + 1); }, 100);
              })(0);
        } catch(e){ reply(src, {tag:d.tag, err:String(e)}); }
      });
      return;
    }
    if (d.cmd === 'addSpot'){
      var a = snap();
      if (!a.ok){ reply(src, {tag:d.tag, err:a.err}); return; }
      var before = ST.picked.slice();
      try { toggleSpot(d.id); } catch(e){}
      reply(src, {tag:d.tag,
        data:{ changed: ST.picked.join() !== before.join(), snap:snap() }});
      return;
    }
    if (d.cmd === 'setDays'){
      try {
        ST.days = d.days;
        reportOp('set', 'days:' + d.days); save(); renderDIY();
        reply(src, {tag:d.tag, data:snap()});
      } catch(e){ reply(src, {tag:d.tag, err:String(e)}); }
      return;
    }
  });
  function up(){ try { parent.postMessage({type:'agent-up'}, '*'); } catch(e){} }
  window.addEventListener('load', up);
  setTimeout(up, 150); setTimeout(up, 600); setTimeout(up, 1500);
})();
</script>"""

HARNESS = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>wait</title>
<style>html,body{margin:0}iframe{width:300px;height:520px;border:1px solid #ddd}</style>
</head><body>
<iframe id="fh"></iframe><iframe id="fv" style="display:none"></iframe>
<iframe id="fe" style="display:none"></iframe>
<script>
var ROOM='__ROOM__', PW='__PW__';
var H=document.getElementById('fh'), V=document.getElementById('fv'), E=document.getElementById('fe');
var out={steps:[], up:0, errs:[], ready:{}};
window.addEventListener('error', function(e){ out.errs.push(String(e.message)); });
var seq=0, waiting={}, frames={H:H, V:V, E:E};

function ask(which, cmd, extra){
  var tag='q'+(++seq), msg={cmd:cmd, tag:tag}, f=frames[which];
  for (var k in (extra||{})) msg[k]=extra[k];
  f.contentWindow.postMessage(msg, '*');
  return tag;
}
function waitTag(tag, ms){
  return new Promise(function(res){
    waiting[tag]=res;
    setTimeout(function(){ if (waiting[tag]){ delete waiting[tag]; res(null); } }, ms||20000);
  });
}
window.addEventListener('message', function(ev){
  var d = ev.data || {};
  if (d.type === 'agent-up'){
    out.up++;
    for (var k in frames){
      try { if (ev.source === frames[k].contentWindow) out.ready[k]=true; } catch(e){}
    }
    return;
  }
  if (d.tag && waiting[d.tag]){ var r=waiting[d.tag]; delete waiting[d.tag]; r(d); }
});
function send(which, cmd, extra, ms){ return waitTag(ask(which, cmd, extra), ms); }
function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
/* 等某个客户端真的进入期望状态，而不是猜一个固定时长。
   服务端广播回来的延迟不固定，本机快时侥幸通过、负载一高就假失败。*/
async function until(which, test, ms){
  ms = ms || 20000;
  var t0 = Date.now();
  while (Date.now() - t0 < ms){
    var r = await send(which, 'snap', {}, 8000);
    if (r && r.data && test(r.data)) return r.data;
    await sleep(200);
  }
  var last = await send(which, 'snap', {}, 8000);
  return last && last.data;
}

(async function(){
  try {
    var raw = await (await fetch('/app.html', {cache:'no-store'})).text();
    var doc = raw.replace('</body>', __AGENT__ + '</body>');
    out.docLen = doc.length;

    /* 三个文档同时挂上，让它们并行加载；入房顺序由 H → V → E 的 join 决定 */
    H.srcdoc = doc; V.srcdoc = doc; E.srcdoc = doc;

    /* 等三个 agent 都报到 */
    await new Promise(function(res){
      (function w(n){
        if (out.ready.H && out.ready.V && out.ready.E) { res(); return; }
        if (n > 100) { res(); return; }
        setTimeout(function(){ w(n + 1); }, 100);
      })(0);
    });
    out.steps.push('三个文档就绪: ' + JSON.stringify(out.ready));

    var j1 = await send('H', 'join', {room:ROOM, name:'李昊然', pw:''});
    out.steps.push('H 入房: ' + (j1 && j1.data ? j1.data.mode + '/' + j1.data.role : 'no reply'));

    var lk = await send('H', 'lock', {pw:'host:' + PW});
    out.steps.push('H 设密码: ' + (lk && lk.data ? 'locked=' + lk.data.locked : 'no reply'));

    var j2 = await send('V', 'join', {room:ROOM, name:'只读同学', pw:'view:' + PW});
    out.steps.push('V 入房: ' + (j2 && j2.data ? j2.data.mode + '/' + j2.data.role : 'no reply'));

    var j3 = await send('E', 'join', {room:ROOM, name:'编辑同学', pw:PW});
    out.steps.push('E 入房: ' + (j3 && j3.data ? j3.data.mode + '/' + j3.data.role : 'no reply'));
    await sleep(1200);

    var rv = await send('V', 'addSpot', {id:'panda'});
    out.roBlocked = (rv && rv.data) ? (rv.data.changed === false) : null;
    out.steps.push('只读尝试加景点: changed=' + out.roBlocked);

    await send('E', 'addSpot', {id:'panda'});
    await send('E', 'setDays', {days:7});
    out.steps.push('编辑加景点+改天数');

    // 等三方都收敛到期望状态
    function done(d){
      return (d.picked || []).indexOf('panda') >= 0 && d.days === 7;
    }
    out.H = await until('H', done);
    out.V = await until('V', done);
    out.E = await until('E', done);
    out.steps.push('三方状态: H=' + JSON.stringify((out.H||{}).picked) + '/' + (out.H||{}).days
                 + ' V=' + JSON.stringify((out.V||{}).picked) + '/' + (out.V||{}).days);
    out.room = ROOM;
  } catch(e){ out.fatal = String(e && e.message || e); }
  document.title = 'R' + JSON.stringify(out);
})();
</script></body></html>"""


def main():
    # 关键坑：agent 源码里含 "</script>" 字面量。直接塞进父页面的 <script> 里，
    # HTML 解析器会在此处提前闭合脚本，整个 harness 静默失效（不报错、只是不动）。
    # 先 JSON 编码，再把 "</" 转成 "<\/"（JS 里等价，HTML 不再识别为结束标签）。
    agent_lit = json.dumps(AGENT)
    agent_lit = agent_lit.replace("</", "<\\/")
    html = HARNESS.replace("__ROOM__", ROOM).replace("__PW__", PW)
    html = html.replace("__AGENT__", agent_lit)
    # 确认真的没有裸的 </script> 残留在 harness 的脚本里
    assert "</script>" not in html.split("__NEVER__")[0].replace(
        "</script></body></html>", ""), "harness 里仍有裸的 </script>"
    open(os.path.join(D, "_rframe.html"), "w", encoding="utf-8", newline="").write(html)

    r = subprocess.run(
        [EDGE, "--headless=new", "--disable-gpu", "--no-sandbox",
         "--virtual-time-budget=40000", "--window-size=1000,700", "--dump-dom",
         "http://127.0.0.1:%d/_rframe.html" % PORT],
        capture_output=True, timeout=300)
    dom = (r.stdout or b"").decode("utf-8", "ignore")
    m = re.search(r"<title>R(.*?)</title>", dom, re.S)
    if not m:
        print("未取到结果，DOM 长度", len(dom))
        return
    d = json.loads(m.group(1))

    print("房间: %s | 密码: %s | agent-up: %s | 就绪: %s"
          % (d.get("room"), PW, d.get("up"), d.get("ready")))
    print("步骤:")
    for s in d.get("steps", []):
        print("   ", s)
    if d.get("fatal"):
        print("致命错误:", d["fatal"])
    if d.get("errs"):
        print("页面错误:", d["errs"])
    for k in ("H", "V", "E"):
        print("  %s: %s" % (k, d.get(k)))

    H, V, E = d.get("H") or {}, d.get("V") or {}, d.get("E") or {}
    PASS, FAIL = [], []

    def ok(n, c, e=""):
        (PASS if c else FAIL).append(n)
        print("  %s %s%s" % ("PASS" if c else "FAIL", n, ("  [" + str(e) + "]") if e else ""))

    print("\n=== 结果 ===")
    ok("三方都有响应", all(x.get("ok") for x in (H, V, E)),
       "%s/%s/%s" % (H.get("ok"), V.get("ok"), E.get("ok")))
    ok("三方都建立连接", all(x.get("mode") in ("ws", "sse") for x in (H, V, E)),
       "%s/%s/%s" % (H.get("mode"), V.get("mode"), E.get("mode")))
    ok("主持人角色正确", H.get("role") == "host", H.get("roleTxt"))
    ok("只读成员角色正确", V.get("role") == "view", V.get("roleTxt"))
    ok("编辑成员角色正确", E.get("role") == "edit", E.get("roleTxt"))
    ok("房间已加密", H.get("locked") is True, H.get("locked"))
    ok("三方互相可见（3 人在线）",
       H.get("members") == 3 and V.get("members") == 3 and E.get("members") == 3,
       "%s/%s/%s" % (H.get("members"), V.get("members"), E.get("members")))
    ok("只读成员的修改被拦住", d.get("roBlocked") is True, d.get("roBlocked"))
    # 注意：只读成员「看到」panda 是对的——那是编辑加进来后同步给它的。
    # 要断言的是它自己那一次点击没有生效（roBlocked），而不是它看不到。
    # 被拦住 = 它自己那次点击没生效；同时它应该看到编辑后来加进去的 panda
    ok("只读成员自己的改动被拦住，但能收到别人的改动",
       d.get("roBlocked") is True and "panda" in (V.get("picked") or []),
       "roBlocked=%s picked=%s" % (d.get("roBlocked"), V.get("picked")))
    ok("编辑改动同步到主持人", "panda" in (H.get("picked") or []), H.get("picked"))
    ok("编辑改动同步到只读成员", "panda" in (V.get("picked") or []), V.get("picked"))
    ok("天数改动同步到主持人", H.get("days") == 7, H.get("days"))
    ok("天数改动同步到只读成员", V.get("days") == 7, V.get("days"))
    ok("三方都持有令牌", all(x.get("hasToken") for x in (H, V, E)),
       "%s/%s/%s" % (H.get("hasToken"), V.get("hasToken"), E.get("hasToken")))

    print("\n共%d项 通过%d" % (len(PASS) + len(FAIL), len(PASS)))
    if FAIL:
        print("失败:", FAIL)


main()
