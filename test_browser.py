# -*- coding: utf-8 -*-
"""浏览器级验证（双客户端实时同步）

   这份是早期版本的替代品。旧版有两处不可靠，都换掉了：
     1. 靠固定 setTimeout 卡时间 → 改成轮询等真实条件
     2. 父页面读 iframe.contentWindow.__api → const 是词法绑定不挂 window，
        改成 srcdoc 注入 agent + postMessage
   另外注意 agent 源码含 "</script>"，塞进父页面脚本时必须转义，否则
   HTML 解析器提前闭合脚本，整个 harness 静默失效。
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
ROOM = "B2" + str(int(time.time()) % 100000)

AGENT = """<script>
(function(){
  function snap(){
    try {
      return { ok:true, mode:COLLAB.mode, role:COLLAB.role, me:COLLAB.name,
               members:COLLAB.peers.length, picked:ST.picked.slice(),
               days:ST.days, hasToken: !!COLLAB.token };
    } catch(e){ return { ok:false, err:String(e) }; }
  }
  function reply(src, obj){ try { src.postMessage(obj, '*'); } catch(e){} }
  function joined(){ try { return !!(COLLAB.token && COLLAB.role); } catch(e){ return false; } }
  window.addEventListener('message', function(ev){
    var d = ev.data || {}, src = ev.source;
    if (!d || !d.cmd) return;
    if (d.cmd === 'join'){
      try { connectCollab(d.room, d.name, d.pw || ''); }
      catch(e){ reply(src, {tag:d.tag, err:String(e)}); return; }
      (function w(n){
        if (joined() || n > 300){ reply(src, {tag:d.tag, data:snap()}); return; }
        setTimeout(function(){ w(n+1); }, 100);
      })(0);
      return;
    }
    if (d.cmd === 'snap'){ reply(src, {tag:d.tag, data:snap()}); return; }
    if (d.cmd === 'addSpot'){
      var before = ST.picked.slice();
      try { toggleSpot(d.id); } catch(e){}
      reply(src, {tag:d.tag,
        data:{ changed: ST.picked.join() !== before.join(), snap:snap() }});
      return;
    }
    if (d.cmd === 'setDays'){
      try { ST.days = d.days; reportOp('set','days:'+d.days); save(); renderDIY();
            reply(src, {tag:d.tag, data:snap()}); }
      catch(e){ reply(src, {tag:d.tag, err:String(e)}); }
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
<iframe id="fa"></iframe><iframe id="fb"></iframe>
<script>
var ROOM='__ROOM__';
var A=document.getElementById('fa'), B=document.getElementById('fb');
var out={steps:[], up:0, errs:[], ready:{}};
window.addEventListener('error', function(e){ out.errs.push(String(e.message)); });
var seq=0, waiting={}, frames={A:A, B:B};

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

(async function(){
  try {
    var raw = await (await fetch('/app.html',{cache:'no-store'})).text();
    var doc = raw.replace('</body>', __AGENT__ + '</body>');
    A.srcdoc = doc; B.srcdoc = doc;

    await new Promise(function(res){
      (function w(n){
        if ((out.ready.A && out.ready.B) || n > 150){ res(); return; }
        setTimeout(function(){ w(n+1); }, 100);
      })(0);
    });
    out.steps.push('两文档就绪');

    var ja = await send('A','join',{room:ROOM, name:'甲同学', pw:''});
    out.steps.push('A 入房: ' + (ja&&ja.data ? ja.data.mode+'/'+ja.data.role : 'no reply'));
    var jb = await send('B','join',{room:ROOM, name:'乙同学', pw:''});
    out.steps.push('B 入房: ' + (jb&&jb.data ? jb.data.mode+'/'+jb.data.role : 'no reply'));
    await sleep(800);

    /* 等对方页面真的收到广播，而不是猜一个固定时长。
       reportOp 把消息发出去就返回了，服务端广播回来还有一段延迟。*/
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

    // A 加景点 → 等 B 收到
    await send('A','addSpot',{id:'panda'});
    out.B_afterAdd = await until('B', function(d){
      return (d.picked || []).indexOf('panda') >= 0;
    });
    out.steps.push('A 加景点后 B 的 picked=' + JSON.stringify((out.B_afterAdd||{}).picked));

    // B 改天数 → 等 A 收到
    await send('B','setDays',{days:7});
    out.A_afterDays = await until('A', function(d){ return d.days === 7; });
    out.steps.push('B 改天数后 A 的 days=' + (out.A_afterDays||{}).days);

    var sa2 = await send('A','snap'), sb2 = await send('B','snap');
    out.A = sa2 && sa2.data; out.B = sb2 && sb2.data;
    out.room = ROOM;
  } catch(e){ out.fatal = String(e && e.message || e); }
  document.title = 'R' + JSON.stringify(out);
})();
</script></body></html>"""


def main():
    lit = json.dumps(AGENT).replace("</", "<\\/")
    open(os.path.join(D, "_b2.html"), "w", encoding="utf-8", newline="").write(
        HARNESS.replace("__ROOM__", ROOM).replace("__AGENT__", lit))

    r = subprocess.run(
        [EDGE, "--headless=new", "--disable-gpu", "--no-sandbox",
         "--virtual-time-budget=60000", "--window-size=1000,700", "--dump-dom",
         "http://127.0.0.1:%d/_b2.html" % PORT],
        capture_output=True, timeout=300)
    dom = (r.stdout or b"").decode("utf-8", "ignore")
    m = re.search(r"<title>R(.*?)</title>", dom, re.S)
    if not m:
        print("未取到结果，DOM 长度", len(dom))
        return
    d = json.loads(m.group(1))

    print("房间: %s | agent-up: %s | 就绪: %s" % (d.get("room"), d.get("up"), d.get("ready")))
    for s in d.get("steps", []):
        print("   ", s)
    if d.get("fatal"):
        print("致命错误:", d["fatal"])
    if d.get("errs"):
        print("页面错误:", d["errs"])

    A, B = d.get("A") or {}, d.get("B") or {}
    Ba, Aa = d.get("B_afterAdd") or {}, d.get("A_afterDays") or {}
    PASS, FAIL = [], []

    def ok(n, c, e=""):
        (PASS if c else FAIL).append(n)
        print("  %s %s%s" % ("PASS" if c else "FAIL", n, ("  [" + str(e) + "]") if e else ""))

    print("\n=== 结果 ===")
    ok("两个客户端都连上", all(x.get("mode") == "ws" for x in (A, B)),
       "%s/%s" % (A.get("mode"), B.get("mode")))
    ok("互相看到对方在线", A.get("members") == 2 and B.get("members") == 2,
       "%s/%s" % (A.get("members"), B.get("members")))
    ok("名字各自正确", A.get("me") == "甲同学" and B.get("me") == "乙同学",
       "%s/%s" % (A.get("me"), B.get("me")))
    ok("A 加的景点同步到 B", "panda" in (Ba.get("picked") or []), Ba.get("picked"))
    ok("B 改的天数同步到 A", Aa.get("days") == 7, Aa.get("days"))
    ok("最终两边状态一致",
       (A.get("picked") or []) == (B.get("picked") or []) and A.get("days") == B.get("days"),
       "A=%s/%s  B=%s/%s" % (A.get("picked"), A.get("days"), B.get("picked"), B.get("days")))
    ok("两边都持有令牌", bool(A.get("hasToken")) and bool(B.get("hasToken")),
       "%s/%s" % (A.get("hasToken"), B.get("hasToken")))

    print("\n共%d项 通过%d" % (len(PASS) + len(FAIL), len(PASS)))
    if FAIL:
        print("失败:", FAIL)


main()
