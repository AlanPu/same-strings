"""本机预览静态站点（支持 Range，行为与 nginx.conf 一致）。

用途：在把镜像推到 NAS 之前，先在 Mac 上让 iPad 连过来试。
它只做一件事——按 nginx.conf 的语义发 dist 下的文件，
包括 Range/206 和 audio/mp4，所以能提前暴露「iOS 播不动」这类问题。

用法：
    .venv/bin/python tools/serve.py                # dist/site，8080
    .venv/bin/python tools/serve.py 8081 ref       # dist/ref，8081
"""
from __future__ import annotations

import socket
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from smoke_site import RangeHandler  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"


def lan_ip() -> str:
    """取本机在局域网里的地址（不会真的发包）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.1.1", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    name = sys.argv[2] if len(sys.argv) > 2 else "site"
    site = DIST / name
    if not (site / "index.html").exists():
        print(f"缺少 {site}/index.html，先跑构建")
        return 2

    srv = ThreadingHTTPServer(
        ("0.0.0.0", port), lambda *a, **k: RangeHandler(*a, directory=str(site), **k))
    ip = lan_ip()
    print(f"站点目录 {site}")
    print(f"  本机：  http://127.0.0.1:{port}/")
    print(f"  局域网：http://{ip}:{port}/   ← iPad 用这个")
    print("Ctrl-C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
