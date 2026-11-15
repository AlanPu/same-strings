"""服务端形态的协议层冒烟：证明 dist/site 能被按段取用。

为什么单独做这件事：iOS Safari 的 <audio> 不是「一次 GET 拿全文件」，
它会先发 Range 探测、再按需续取。服务器若只回 200 全量，表现是「点了播放没反应」，
而且这个失败在 file:// 下复现不出来 —— 必须真的走一遍 HTTP。

本脚本用标准库起一个支持 Range 的静态服务器（模拟 nginx.conf 里的行为），
逐条验证：
  1. index.html 以 text/html、200 返回；
  2. 音频响应带 Accept-Ranges: bytes、Content-Type: audio/mp4；
  3. 带 Range 的请求返回 206，Content-Range 与字节数都对得上；
  4. 首段字节是 ftyp（确实是 m4a，不是空的或 HTML 错误页）；
  5. bank.json 里的每个 clipId 在 audio/ 下都有对应文件。

用法：
    .venv/bin/python tools/smoke_site.py [dist/site]
"""
from __future__ import annotations

import http.server
import json
import os
import re
import sys
import threading
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SITE = ROOT / "dist" / "site"

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".m4a": "audio/mp4",
    # 参照页的视频。少了这条会落到 octet-stream，iOS 对 <video> 的 MIME 比 <audio>
    # 更挑，实测会出现「一直转圈不出画面」。
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
}


class RangeHandler(http.server.SimpleHTTPRequestHandler):
    """照 nginx 的行为发静态文件：Accept-Ranges + 206 分段。"""

    def __init__(self, *args, directory: str | None = None, **kwargs):
        super().__init__(*args, directory=directory, **kwargs)

    def log_message(self, fmt, *args):   # noqa: A003
        pass                             # 测试输出要干净

    def _ctype(self, path: str) -> str:
        return MIME.get(Path(path).suffix.lower(), "application/octet-stream")

    def do_HEAD(self):                   # noqa: N802
        self._serve(body=False)

    def do_GET(self):                    # noqa: N802
        self._serve(body=True)

    def _serve(self, body: bool) -> None:
        path = self.translate_path(self.path.split("?")[0])
        if os.path.isdir(path):
            path = os.path.join(path, "index.html")
        if not os.path.isfile(path):
            self.send_error(404)
            return

        size = os.path.getsize(path)
        ctype = self._ctype(path)
        rng = self.headers.get("Range")

        if not rng:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            if body:
                with open(path, "rb") as fh:
                    while True:
                        chunk = fh.read(64 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            return

        m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
        if not m:
            self.send_error(416)
            return
        a, b = m.group(1), m.group(2)
        start = int(a) if a else 0
        end = int(b) if b else size - 1
        end = min(end, size - 1)
        if start > end or start >= size:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return

        self.send_response(206)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        if body:
            with open(path, "rb") as fh:
                fh.seek(start)
                self.wfile.write(fh.read(end - start + 1))


def fetch(url: str, rng: str | None = None) -> tuple[int, dict, bytes]:
    """发一次请求，返回 (状态码, 响应头, 正文)。"""
    req = urllib.request.Request(url)
    if rng:
        req.add_header("Range", rng)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


def main(argv: list[str]) -> int:
    site = Path(argv[1]) if len(argv) > 1 else DEFAULT_SITE
    if not (site / "index.html").exists():
        print(f"缺少 {site}/index.html，先跑 build.py")
        return 2

    srv = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), lambda *a, **k: RangeHandler(*a, directory=str(site), **k))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    print(f"服务根目录 {site}（{base}）")

    rows: list[tuple[str, str, bool]] = []

    st, hd, data = fetch(f"{base}/index.html")
    rows.append(("index.html 200", f"{st}，{len(data) / 1024:.1f} KB，"
                 f"{hd.get('Content-Type', '?')}", st == 200 and "text/html" in hd.get("Content-Type", "")))

    bank = json.loads((site / "bank.json").read_text(encoding="utf-8"))
    clips = sorted(bank["clips"])
    missing = [c for c in clips if not (site / "audio" / f"{c}.m4a").exists()]
    rows.append(("片段文件齐全", f"{len(clips)} 个 clipId，缺 {len(missing)}",
                 not missing and len(clips) > 0))

    # 抽三个位置各测一次 Range（开头 / 中段 / 末尾）
    for label, cid in (("首", clips[0]), ("中", clips[len(clips) // 2]), ("末", clips[-1])):
        url = f"{base}/audio/{cid}.m4a"
        size = (site / "audio" / f"{cid}.m4a").stat().st_size

        st, hd, data = fetch(url)
        ok_full = (st == 200 and len(data) == size
                   and hd.get("Accept-Ranges") == "bytes"
                   and hd.get("Content-Type", "").startswith("audio/mp4"))
        rows.append((f"{label}段 全量", f"{st}，{len(data):,} 字节，"
                     f"Accept-Ranges={hd.get('Accept-Ranges', '缺')}", ok_full))

        st, hd, data = fetch(url, "bytes=0-99")
        head_ok = (st == 206 and len(data) == 100
                   and hd.get("Content-Range", "").startswith("bytes 0-99/")
                   and data[4:8] == b"ftyp")
        rows.append((f"{label}段 Range 0-99", f"{st}，{len(data)} 字节，"
                     f"{hd.get('Content-Range', '缺')}，头 {data[4:8]!r}", head_ok))

        st, hd, data = fetch(url, f"bytes={size - 64}-")
        tail_ok = st == 206 and len(data) == 64
        rows.append((f"{label}段 Range 尾 64", f"{st}，{len(data)} 字节", tail_ok))

    width = max(len(r[0]) for r in rows)
    bad = 0
    for name, detail, ok in rows:
        if not ok:
            bad += 1
        print(f"  [{'OK  ' if ok else 'FAIL'}] {name.ljust(width)}  {detail}")

    srv.shutdown()
    print(f"\n通过 {len(rows) - bad}/{len(rows)}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
