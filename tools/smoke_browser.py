"""用无头浏览器对 dist/同弦.html 做冒烟测试。

不需要 Playwright：把断言脚本注入一份临时副本，用 Edge/Chrome 的 headless + --dump-dom
取回执行结果。覆盖：
  · 首页渲染 / 时长 chips 默认值与切换；
  · 「从头开始」顺序模式：题数 = 段数、按段推进到小结；
  · 「自己选段」：选段页条目数、单段模式、作答后「再来一题」换人重来；
  · 随机练习连答 10 题 → 小结 → 记录写入；
  · 考试模式不泄露答案；
  · 存储降级（禁用 localStorage 仍能答题）。
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

EDGE = Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

INJECT = r"""
<script>
window.__err = [];
window.addEventListener('error', function (e) { window.__err.push(String(e.message)); });
/* 用桩替掉 AudioContext，数「到底响了几声」——无头环境没法真的听，
 * 但提示音是靠 createOscillator 发声的，数振荡器就够了。 */
window.__osc = 0;
(function () {
  function Stub() { this.currentTime = 0; this.state = 'running'; this.destination = {}; }
  Stub.prototype.createOscillator = function () {
    window.__osc += 1;
    return { type: '', frequency: { value: 0 },
             connect: function () { return this; }, start: function () {}, stop: function () {} };
  };
  Stub.prototype.createGain = function () {
    return { gain: { setValueAtTime: function () {}, exponentialRampToValueAtTime: function () {} },
             connect: function () { return this; } };
  };
  Stub.prototype.resume = function () {};
  window.AudioContext = Stub;
  window.webkitAudioContext = Stub;
})();
(function () {
  var log = [];
  function $(s) { return document.querySelector(s); }
  function $$(s) { return Array.prototype.slice.call(document.querySelectorAll(s)); }
  function click(el) { if (el) el.dispatchEvent(new MouseEvent('click', { bubbles: true })); }
  function step(name, value) { log.push(name + '=' + value); }
  function pill(n) { var p = $$('.pill'); return p[n] ? p[n].textContent.trim() : 'none'; }
  function chipOn() { var c = $('.chip.on'); return c ? c.textContent.trim() : 'none'; }
  function chipNamed(t) {
    var hit = null;
    $$('.chip').forEach(function (c) { if (c.textContent.trim().indexOf(t) === 0) hit = c; });
    return hit;
  }

  setTimeout(function () {
    try {
      /* ---------- 首页 ---------- */
      step('首页标题', ($('.h1') || {}).textContent);
      step('时长chips数', $$('.chip').length);
      step('默认时长', chipOn());

      /* 切到最短时长，看是否即时生效 */
      click(chipNamed('10'));
      step('切成10秒', chipOn());

      /* ---------- 从头开始（顺序） ---------- */
      click($('[data-act="start"][data-mode="sequence"]'));
      step('顺序模式标', pill(0));
      step('顺序第1题', pill(1));
      step('顺序选项数', $$('.opt').length);
      step('答题页有时长chips', $$('.chips-s .chip').length);
      click($$('.opt')[0]);
      step('顺序反馈', !!$('.feedback'));
      var total = 0;
      for (var i = 0; i < 12; i++) {
        if ($('.score')) break;
        var o = $$('.opt')[0];
        if (o && !$('.feedback')) click(o);
        var n = $('[data-act="next"]');
        if (n) { click(n); total += 1; } else break;
      }
      step('顺序走完题数', total);
      step('顺序出小结', !!$('.score'));
      step('顺序总分', ($('.score .big') || {}).textContent);

      /* ---------- 自己选段 ---------- */
      click($('[data-act="home"]'));
      click($('[data-act="segments"]'));
      step('选段页条目', $$('.segrow').length);
      step('选段页有时长chips', $$('.chip').length);
      click($$('.segrow')[2]);
      step('单段模式标', pill(0));
      step('单段选项数', $$('.opt').length);
      click($$('.opt')[0]);
      step('单段有再来一题', !!$('[data-act="again"]'));
      step('单段有结束入口', !!$('[data-act="finish"]'));
      click($('[data-act="again"]'));
      step('再来一题后反馈清空', !$('.feedback'));
      step('再来一题选项数', $$('.opt').length);
      click($$('.opt')[0]);
      click($('[data-act="finish"]'));
      step('单段小结', !!$('.score'));

      /* ---------- 随机练习 10 题 ---------- */
      click($('[data-act="home"]'));
      click($('[data-act="start"][data-mode="practice"]'));
      step('练习模式标', pill(0));
      step('练习第1题', pill(1));
      step('有进度条', !!$('input[type=range],progress'));
      step('播放状态元素', !!document.getElementById('status'));
      window.__osc = 0;
      for (var j = 0; j < 10; j++) {
        var oo = $$('.opt')[0];
        if (oo) click(oo);
        var nn = $('[data-act="next"]');
        if (nn) click(nn);
      }
      step('练习模式提示音', window.__osc > 0);
      step('练习出小结', !!$('.score'));
      step('练习总分', ($('.score .big') || {}).textContent);
      var raw = null;
      try { raw = localStorage.getItem('tongxian.v1'); } catch (e) {}
      step('记录已写入', raw ? JSON.parse(raw).total.asked : 'no');
      step('记录里存了时长', raw ? JSON.parse(raw).duration : 'no');

      /* ---------- 考试模式不泄露 ---------- */
      click($('[data-act="home"]'));
      click($('[data-act="start"][data-mode="exam"]'));
      step('考试模式标', pill(0));
      step('考试页无时长chips', $$('.chips-s .chip').length);
      window.__osc = 0;
      click($$('.opt')[0]);
      step('考试模式正确高亮数', $$('.opt.correct').length);
      step('考试模式反馈块', !!$('.feedback'));
      step('考试模式一声不响', window.__osc);

      /* ---------- 记录页 ---------- */
      click($('[data-act="quit"]'));
      click($('[data-act="records"]'));
      step('记录页条目', $$('.item').length);
    } catch (e) {
      log.push('EXC ' + e.message);
    }
    var out = document.createElement('pre');
    out.id = 'test-out';
    out.textContent = log.join(' | ') + ' || ERR=' + window.__err.join(';');
    document.body.appendChild(out);
  }, 700);
})();
</script>
"""


def find_browser() -> Path:
    for b in (EDGE, CHROME):
        if b.exists():
            return b
    raise SystemExit("找不到 Edge/Chrome")


def run_case(src: Path, inject_close_storage: bool) -> str:
    html = src.read_text(encoding="utf-8")
    extra = ""
    if inject_close_storage:
        extra = ("<script>try{Object.defineProperty(window,'localStorage',{get:function(){"
                 "throw new Error('blocked')}})}catch(e){}</script>")
    html = html.replace("</body>", extra + INJECT + "</body>")
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as fh:
        fh.write(html)
        tmp = Path(fh.name)
    browser = find_browser()
    proc = subprocess.run(
        [str(browser), "--headless=new", "--disable-gpu", "--no-sandbox",
         "--virtual-time-budget=9000", "--dump-dom", tmp.as_uri()],
        capture_output=True, text=True, timeout=240,
    )
    tmp.unlink(missing_ok=True)
    dom = proc.stdout
    m = re.search(r'<pre id="test-out">(.*?)</pre>', dom, re.S)
    return m.group(1) if m else f"[未取到结果]\n{(proc.stderr or '')[-600:]}"


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("dist/同弦.html")
    print("== 正常环境 ==")
    print(run_case(target, False))
    print()
    print("== 禁用 localStorage ==")
    print(run_case(target, True))
