"""build.log 与 build-report.json 的统一写入。

build.log 给人看：每个阶段的时间戳、参数、实测值。
build-report.json 给 verify.py 看：结构化的验收指标。
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config


class Log:
    """一次构建会话的日志写入器。"""

    def __init__(self, path: Path | None = None, report_path: Path | None = None,
                 append: bool = False) -> None:
        """初始化日志。

        Args:
            path: build.log 路径，默认取 config.LOG_PATH。
            report_path: 机读报告路径，默认取 config.REPORT_PATH。
            append: True 则接着已有日志写（单跑验收时用），False 则开新的一轮构建。
        """
        self.path = path or config.LOG_PATH
        self.report_path = report_path or config.REPORT_PATH
        self.report: dict[str, Any] = {"builtAt": None, "stages": {}, "metrics": {}, "warnings": []}
        self._t0 = time.time()
        if append and self.path.exists():
            self.line("")
            self.line("")
            self.line("-------------------------- 追加：只跑验收 --------------------------")
        else:
            self.path.write_text("", encoding="utf-8")
        self.line(f"同弦构建开始 · {datetime.now().astimezone().isoformat(timespec='seconds')}")

    # ------------------------------------------------------------ 基础写入
    def line(self, text: str = "") -> None:
        """追加一行原始文本。"""
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(text)

    def stage(self, name: str) -> None:
        """写入阶段标题。"""
        self.report["stages"].setdefault(name, {"start": datetime.now().astimezone().isoformat(timespec="seconds")})
        self.line("")
        self.line(f"===== {name} =====")

    def kv(self, **pairs: Any) -> None:
        """写入若干 key=value 行。"""
        for key, val in pairs.items():
            self.line(f"  {key} = {val}")

    def warn(self, text: str) -> None:
        """记录警告（不中断构建，但进报告）。"""
        self.report["warnings"].append(text)
        self.line(f"  ! WARN {text}")

    def fail(self, text: str) -> None:
        """记录失败并抛错，由 build.py 转成非零退出码。"""
        self.line(f"  x FAIL {text}")
        raise config.BuildError(text)

    def table(self, headers: list[str], rows: list[list[Any]], indent: str = "  ") -> None:
        """打印等宽对齐的表格。"""
        cells = [[str(c) for c in r] for r in rows]
        widths = [max(len(headers[i]), *(len(r[i]) for r in cells)) if cells else len(headers[i])
                  for i in range(len(headers))]
        self.line(indent + " | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
        self.line(indent + "-+-".join("-" * w for w in widths))
        for r in cells:
            self.line(indent + " | ".join(r[i].ljust(widths[i]) for i in range(len(headers))))

    def metric(self, name: str, value: Any, judge: str, ok: bool) -> None:
        """记录一条验收指标。

        Args:
            name: 指标名（对应 §10 总表）。
            value: 实测值。
            judge: 判据描述。
            ok: 是否通过。
        """
        self.report["metrics"][name] = {"value": value, "judge": judge, "ok": bool(ok)}
        mark = "OK  " if ok else "FAIL"
        self.line(f"  [{mark}] {name}: {value}    (判据 {judge})")

    # ------------------------------------------------------------ 收尾
    def finish(self, ok: bool) -> None:
        """写入耗时并落盘机读报告。"""
        elapsed = time.time() - self._t0
        self.report["builtAt"] = datetime.now().astimezone().isoformat(timespec="seconds")
        self.report["elapsedSec"] = round(elapsed, 1)
        self.report["ok"] = ok
        self.line("")
        self.line(f"构建{'成功' if ok else '失败'} · 耗时 {elapsed / 60:.1f} 分钟")
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text(
            json.dumps(self.report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
