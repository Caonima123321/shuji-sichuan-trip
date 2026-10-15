# -*- coding: utf-8 -*-
"""功能自测：注入断言脚本，用 Edge headless 跑一遍交互，把结果写进 body 属性后 dump 回来。"""
import io, os, re, subprocess

BASE = r"D:\d\sichuan-route-ppt\sichuan-trip"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

src = io.open(os.path.join(BASE, "index.html"), encoding="utf-8").read()

test = """<script>
window.__err = [];
window.addEventListener('error', e => window.__err.push(String(e.message)));
window.addEventListener('load', function(){
  setTimeout(function(){
    var R = [];
    function ok(name, cond, extra){ R.push((cond?'PASS':'FAIL')+' '+name+(extra?' ['+extra+']':'')); }
    try{
      // 1 初始渲染
      ok('推荐卡片渲染', document.querySelectorAll('#recList .rt').length === 8,
         'rt='+document.querySelectorAll('#recList .rt').length);
      ok('景点库渲染', document.querySelectorAll('#spotList .spot').length >= 20,
         'spots='+document.querySelectorAll('#spotList .spot').length);
      ok('清单渲染', document.querySelectorAll('#listBody .ck').length > 20,
         'ck='+document.querySelectorAll('#listBody .ck').length);

      // 2 主题卡（一日/三日/七日）
      ok('主题卡渲染', document.querySelectorAll('#lanes .lane').length === 3,
         'lane='+document.querySelectorAll('#lanes .lane').length);
      ok('主题卡带实景背景', [].slice.call(document.querySelectorAll('#lanes .lane img'))
           .every(function(m){ return m.naturalWidth > 0; }));
      ok('主题卡显示起价', /¥\d/.test(document.querySelector('#lanes .lane .pr').textContent),
         document.querySelector('#lanes .lane .pr').textContent.trim());
      ok('路线按天数组显示', document.querySelectorAll('#recList .rgrp').length === 3,
         'grp='+document.querySelectorAll('#recList .rgrp').length);
      document.querySelector('#lanes .lane[data-d="7"]').click();
      ok('切到七日档', document.querySelectorAll('#recList .rt').length === 8,
         'rt='+document.querySelectorAll('#recList .rt').length);
      ok('非当前档位淡化', document.querySelectorAll('#recList .rt.dim').length === 5,
         'dim='+document.querySelectorAll('#recList .rt.dim').length);
      document.querySelector('#recList .rt[data-d="7"][data-ri="0"]').click();

      // 3 载入推荐路线 -> 应跳到 DIY 且已选景点（第 40 行已点上七日档第一条）
      ok('载入推荐到DIY', document.getElementById('v-diy').classList.contains('active'));
      var picked = document.querySelectorAll('#planList .item').length;
      ok('行程已生成', picked > 3, 'items='+picked);
      ok('天数自动同步', document.getElementById('dayNum').textContent.replace(/\\s/g,'') === '7天',
         document.getElementById('dayNum').textContent.replace(/\\s/g,''));
      ok('DAY 分组存在', document.querySelectorAll('#planList .plan').length >= 1,
         'days='+document.querySelectorAll('#planList .plan').length);

      // 4 费用页
      document.querySelector('.tab[data-v="cost"]').click();
      var body = document.getElementById('costBody').textContent;
      ok('费用页有金额', /¥[\\d,]+/.test(body));
      ok('含明细表', body.indexOf('预估总里程') > -1);
      var kpis = document.querySelectorAll('#costBody .kpi').length;
      ok('四项KPI', kpis === 4, 'kpi='+kpis);

      // 5 增删景点联动
      document.querySelector('.tab[data-v="diy"]').click();
      var before = document.querySelectorAll('#planList .item').length;
      document.querySelector('#spotList .spot:not(.in)').click();
      var after = document.querySelectorAll('#planList .item').length;
      ok('点＋可加景点', after === before + 1, before+'->'+after);
      var first = document.querySelector('#spotList .spot.in');
      if (first) first.click();
      ok('再点可移除', document.querySelectorAll('#planList .item').length === before,
         'now='+document.querySelectorAll('#planList .item').length);

      // 6 人数/天数步进
      var p0 = document.getElementById('pplNum').textContent;
      document.querySelector('[data-act="ppl+"]').click();
      ok('人数可增加', document.getElementById('pplNum').textContent !== p0,
         p0+'->'+document.getElementById('pplNum').textContent);

      // 7 保存路线
      document.querySelector('.tab[data-v="cost"]').click();
      document.getElementById('saveTrip').click();
      document.querySelector('.tab[data-v="me"]').click();
      ok('可保存到我的', document.querySelectorAll('#meBody .card').length >= 1,
         'saved='+document.querySelectorAll('#meBody .card').length);

      // 8 清单勾选
      document.querySelector('.tab[data-v="list"]').click();
      document.querySelector('#listBody .bx').click();
      var ckHead = document.getElementById('ckHead');
      ok('清单可勾选', !!ckHead && /(^|\D)1\s*\//.test(ckHead.textContent),
         ckHead ? ckHead.textContent.trim() : 'ckHead 缺失');

      // 9 状态栏
      ok('时钟已刷新', /^\\d{2}:\\d{2}$/.test(document.getElementById('sbTime').textContent),
         document.getElementById('sbTime').textContent);
      ok('电量已刷新', /\\d+%/.test(document.getElementById('batPct').textContent),
         document.getElementById('batPct').textContent);

      // 10 轮播与文物
      ok('轮播渲染', document.querySelectorAll('#carousel .slide').length === 4,
         'slides='+document.querySelectorAll('#carousel .slide').length);
      ok('轮播指示点', document.querySelectorAll('#dots i').length === 4);
      ok('实景图集', document.querySelectorAll('#gallery .gi').length >= 10,
         'gi='+document.querySelectorAll('#gallery .gi').length);
      var imgs = [].slice.call(document.querySelectorAll('img'));
      // 允许 assets/ 相对路径与 data: 内嵌；外部 http 或失效路径都算不合格
      var broken = imgs.filter(function(m){
        var s0 = m.getAttribute('src');
        return s0 && !/^assets\//.test(s0) && !/^data:image\//.test(s0);
      });
      ok('图片均为本地资源', broken.length === 0, 'bad='+broken.length);
      // 真实加载校验：naturalWidth 为 0 说明文件缺失或路径写错
      // 全部视图都要查：隐藏视图里的破图同样是 bug，之前就是这里放过了 8 张
      var dead = imgs.filter(function(m){
        return m.getAttribute('src') && m.naturalWidth === 0;
      }).map(function(m){ return m.getAttribute('src'); });
      ok('全部图片真实加载', dead.length === 0,
         'dead='+dead.length+(dead.length?' → '+dead.slice(0,3).join(','):''));
      ok('文物卡带实景照片', document.querySelectorAll('#relicList .rph img').length === 6,
         'rph='+document.querySelectorAll('#relicList .rph img').length);
      ok('圆形图标九宫', document.querySelectorAll('#orbits .orb').length === 10,
         'orb='+document.querySelectorAll('#orbits .orb').length);
      document.querySelector('.tab[data-v="me"]').click();
      ok('文物卡片渲染', document.querySelectorAll('#relicList .relic').length === 6,
         'relic='+document.querySelectorAll('#relicList .relic').length);
      // 图源切换
      document.querySelector('.tab[data-v="me"]').click();
      var before = document.querySelector('#lanes .lane img').getAttribute('src');
      document.querySelector('#srcSegs button[data-src2="plan"]').click();
      var after = document.querySelector('#lanes .lane img').getAttribute('src');
      ok('图源可切换', before !== after, (before||'').slice(-18)+' → '+(after||'').slice(-18));
      document.querySelector('#srcSegs button[data-src2="new"]').click();
      ok('图源切回', document.querySelector('#lanes .lane img').getAttribute('src') === before);
      ok('轮播随图源更新',
         document.querySelectorAll('#carousel .slide').length === 4,
         'slides='+document.querySelectorAll('#carousel .slide').length);

      ok('纹样SVG已注入', document.querySelectorAll('#orbits svg path, #orbits svg circle').length > 10,
         'paths='+document.querySelectorAll('#orbits svg path, #orbits svg circle').length);

      // 11 无 JS 报错
      ok('无 JS 运行错误', window.__err.length === 0, window.__err.join(' | '));
    }catch(err){ R.push('FAIL 异常: '+err.message); }
    var pass = R.filter(x=>x.indexOf('PASS')===0).length;
    document.body.setAttribute('data-test', '共'+R.length+'项 通过'+pass+' | ' + R.join(' ;; '));
  }, 700);
});
</script>"""

tmp = os.path.join(BASE, "_t.html")
io.open(tmp, "w", encoding="utf-8", newline="").write(src.replace("</body>", test + "</body>"))

r = subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--no-sandbox",
                    "--virtual-time-budget=9000", "--window-size=420,900",
                    "--dump-dom", "file:///" + tmp.replace("\\", "/")],
                   capture_output=True, timeout=240)
dom = r.stdout.decode("utf-8", "ignore")
m = re.search(r'data-test="([^"]*)"', dom)
if not m:
    print("未取到测试结果，DOM 长度", len(dom))
    for line in re.findall(r'FAIL[^<"]{0,120}', dom)[:10]:
        print("  ", line)
else:
    head, *rest = m.group(1).split(" | ")
    print(head)
    for line in rest:
        print("   " + "  ".join(line.split(" ;; ")))

os.remove(tmp)
