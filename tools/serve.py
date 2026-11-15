"""本机预览静态站点（支持 Range，行为与 nginx.conf 一致）。

用途：在把镜像推到 NAS 之前，先在 Mac 上让 iPad 连过来试。
它只做一件事——按 nginx.conf 的语义发 dist 下的文件，
包括 Range/206 和 audio/mp4，所以能提前暴露「iOS 播不动」这类问题。

用法：
    .venv/bin/python tools/serve.py                # dist/site，25001
    .venv/bin/python tools/serve.py 25002 ref      # dist/ref，25002
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


class QuietServer(ThreadingHTTPServer):
    """客户端中途断线时不打堆栈。

    播放器（尤其 iOS 拖动进度条时）会一次性发很多 Range 请求，拿到够用的字节就
    abort 掉剩下的，于是服务端 write 时抛 BrokenPipeError / ConnectionResetError。
    这是正常行为，不是错误 —— 但 ThreadingHTTPServer 默认会把整段 traceback
    打到 stderr，一次加载刷几十屏，把真正的问题淹掉。
    """

    def handle_error(self, request, client_address) -> None:  # noqa: D102
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


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
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 25001
    name = sys.argv[2] if len(sys.argv) > 2 else "site"
    site = DIST / name
    if not (site / "index.html").exists():
        print(f"缺少 {site}/index.html，先跑构建")
        return 2

    srv = QuietServer(
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
