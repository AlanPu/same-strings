"""解析 b23.tv 短链 → BV 号 / 时长 / 分P / chapters。

单独跑：python3 tools/resolve_meta.py <url> [<url> ...]
输出 JSON 到 stdout，供 project.json 手工核对。
"""
import json
import re
import subprocess
import sys

YTDLP = "yt-dlp"


def run_json(url: str) -> dict:
    """调 yt-dlp -J 拿完整元数据（单个 P 或整个合集）。"""
    out = subprocess.run(
        [YTDLP, "-J", "--skip-download", "--no-warnings", url],
        capture_output=True, text=True, timeout=180,
    )
    if out.returncode != 0:
        raise RuntimeError(f"yt-dlp 失败: {url}\n{out.stderr[-800:]}")
    return json.loads(out.stdout)


def bvid_of(info: dict) -> str:
    """从 id 里还原 BV 号（多 P 时 id 形如 BV1xx..._p3）。"""
    m = re.match(r"(BV[0-9A-Za-z]+)", info.get("id", ""))
    if m:
        return m.group(1)
    return info.get("id", "")


def probe(url: str) -> dict:
    """返回 {bvid, entries:[{index,pid,duration,title}], chapters, total}。"""
    info = run_json(url)
    bvid = bvid_of(info)
    entries = info.get("entries")
    if entries:
        rows = []
        for i, e in enumerate(entries, 1):
            rows.append({
                "index": i,
                "id": e.get("id"),
                "duration": e.get("duration"),
                "title": (e.get("title") or "").split(" p")[0][:60],
            })
        total = sum(r["duration"] or 0 for r in rows)
        # 合集里每个 P 自己也可能带 chapters，但分 P 本身已是乐章边界，忽略
        return {"bvid": bvid, "entries": rows, "chapters": None, "total": total}
    return {
        "bvid": bvid,
        "entries": [{"index": 1, "id": info.get("id"), "duration": info.get("duration"),
                     "title": (info.get("title") or "")[:60]}],
        "chapters": info.get("chapters"),
        "total": info.get("duration"),
    }


if __name__ == "__main__":
    result = []
    for u in sys.argv[1:]:
        try:
            result.append({"url": u, **probe(u)})
        except Exception as exc:  # noqa: BLE001
            result.append({"url": u, "error": str(exc)[:300]})
    print(json.dumps(result, ensure_ascii=False, indent=2))
