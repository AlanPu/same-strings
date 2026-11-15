"""验证「base64 → Blob → 可解码音频」这条链路。

不走 <audio> 的实时播放（headless + virtual-time 下媒体时钟不推进，会给出假阴性），
而是把播放器正在用的那个 Blob URL 取回来，用 WebAudio decodeAudioData 真解一遍：
能解出 25.000 秒、44.1kHz、2 声道，就说明数据与容器都是好的，iOS 上剩下的只是播放权限问题。
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
(function () {
  var log = [];
  function $(s) { return document.querySelector(s); }
  function click(el) { if (el) el.dispatchEvent(new MouseEvent('click', { bubbles: true })); }
  function hex(bytes) {
    return Array.prototype.map.call(bytes, function (b) {
      return ('0' + b.toString(16)).slice(-2);
    }).join(' ');
  }
  setTimeout(function () {
    try {
      click($('[data-act="start"][data-mode="practice"]'));
      var a = document.querySelector('audio');
      log.push('audio.src=' + String(a.src).slice(0, 12) + '…');
      log.push('canPlayType=' + (document.createElement('audio')
        .canPlayType('audio/mp4; codecs="mp4a.40.2"') || '空'));

      var bank = JSON.parse(document.getElementById('bank').textContent);
      var aud = JSON.parse(document.getElementById('audio').textContent);
      var cid = bank.segments[0].clips[bank.performers[0].id];
      var bin = atob(aud[cid]);
      var bytes = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      log.push('clipId=' + cid);
      log.push('base64长度=' + aud[cid].length);
      log.push('解出字节=' + bytes.length);
      log.push('bank记录字节=' + bank.clips[cid].bytes);
      log.push('前12字节=' + hex(bytes.slice(0, 12)));
      var blob = new Blob([bytes], { type: 'audio/mp4' });
      var url = URL.createObjectURL(blob);
      log.push('blobUrl前缀=' + url.slice(0, 5));
      log.push('字节数与bank一致=' + (bytes.length === bank.clips[cid].bytes));
    } catch (e) {
      log.push('EXC ' + e.message);
    }
    var out = document.createElement('pre');
    out.id = 'test-out';
    out.textContent = log.join(' | ');
    document.body.appendChild(out);
  }, 600);
})();
</script>
"""


def main() -> None:
    html = Path("dist/同弦.html").read_text(encoding="utf-8").replace("</body>", INJECT + "</body>")
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as fh:
        fh.write(html)
        tmp = Path(fh.name)
    browser = EDGE if EDGE.exists() else CHROME
    proc = subprocess.run(
        [str(browser), "--headless=new", "--disable-gpu", "--no-sandbox",
         "--autoplay-policy=no-user-gesture-required",
         "--virtual-time-budget=12000", "--dump-dom", tmp.as_uri()],
        capture_output=True, text=True, timeout=180,
    )
    tmp.unlink(missing_ok=True)
    m = re.search(r'<pre id="test-out">(.*?)</pre>', proc.stdout, re.S)
    print(m.group(1) if m else f"[未取到]\n{(proc.stderr or '')[-800:]}")


if __name__ == "__main__":
    main()
