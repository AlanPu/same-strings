"""同弦 · 全局配置与 project.json 载入校验。

规格书 §16 的默认参数集中在此，任何阶段都不得在别处硬编码数字。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- 路径
ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "work"
DIR_SRC = WORK / "src"          # S0 原始音轨 + 元数据
DIR_PCM = WORK / "pcm"          # S1 归一化 PCM
DIR_WARP = WORK / "warp"        # S2 warp 表
DIR_CHECK = WORK / "check"      # S2.5 自检音频
DIR_CLIPS = WORK / "clips"      # S4 片段
DIR_DIST = ROOT / "dist"
WEB = ROOT / "web"
LOG_PATH = ROOT / "build.log"
REPORT_PATH = WORK / "build-report.json"
MANIFEST_PATH = WORK / ".manifest.json"

# ---------------------------------------------------------------- 参数（§16）
SR_ANALYSIS = 22050          # 分析用采样率
SR_OUTPUT = 44100            # 输出采样率
HOP = 1024                   # 特征帧移
ALIGN_FPS = 3.5              # DTW 帧率
ALIGN_SMOOTH = 1.0           # 平滑窗口（秒）
WARP_STEP = 0.5              # warp 表精度（秒）
TARGET_LUFS = -16.0          # 目标积分响度
TARGET_TP = -1.5             # 目标真峰值
SILENCE_DBFS = -50.0         # 静音判定阈值
SILENCE_MIN_SEC = 0.8        # 首尾静音最短长度
GAP_MIN_SEC = 0.15           # 分 P 拼接缝最短长度
SPAN_VERSION = 2             # S1 剪辑语义版本：v2 起含「头部对齐」（裁掉掌声/静场）
ALIGN_VERSION = 2            # S2 对齐语义版本：v2 起含「头部局部校正」
SEG_DURATION = 25.0          # 片段时长（秒），project.json 可覆盖
SEG_HOP = 5.0                # 候选窗口步长
SEG_COUNT_MAX = 8            # 段数上限
MAX_CLIPS = 60               # 兼容旧口径的硬上限（实际按字节预算推导，见 max_clip_count）
SEG_MIN_GAP = 60.0           # 相邻选中窗口最小 anchor 间隔（仅 spread 模式）
SEG_MODE = "sequential"      # sequential=从乐曲开头连续切；spread=按乐章配额挑高分窗口
SEG_STRIDE = 0.0             # sequential 模式的刀口间隔；0 表示紧接上一段（= duration）
SEG_COUNT_MIN = 5
OPTION_COUNT = 4
FADE_MS = 20
CLIP_CODEC = "aac"
CLIP_BITRATE = "96k"
MAX_HTML_MB = 25

# 打包形态（project.json 的 pack.mode）
#   inline：单个自包含 HTML，音频 base64 内联，AirDrop 到 iPad 就能用，受 MAX_HTML_MB 约束；
#   static：静态站点，音频外置为 audio/*.m4a，由 Nginx 等 Web 服务器按需发送，
#           不受 HTML 体积约束，段数只受乐曲长度与 maxCount 限制（可覆盖全曲）。
PACK_MODE = "inline"
PACK_MODES = ("inline", "static")
STATIC_HTML_MAX_KB = 300     # static 模式下 index.html 的体积上限（它只剩代码和索引）
STATIC_DIRNAME = "site"      # dist/site/ 下的产物目录名
LUFS_TOLERANCE = 0.5         # 响度极差容忍
MEANCOSSIM_MIN = 0.85        # 对齐质量下限
MEANCOSSIM_FAIL = 0.75       # 低于此值视为对齐失败

# 体积预算：HTML 里音频是 base64（膨胀 4/3），再留一点给 CSS/JS/bank JSON。
# 规划时按 24 MB 算、验收按 25 MB 卡，留 1 MB 余量，避免卡在边界上翻车。
BUDGET_MB = 24
HTML_OVERHEAD_BYTES = 90_000
BASE64_RATIO = 4 / 3
# 实测 25 秒片段平均 305 KB（96kbps 名义值之外还有容器与帧开销），按 12400 B/s 估更稳
BYTES_PER_SEC = 12_400


def clip_bytes(duration: float) -> int:
    """估算单个片段的字节数。"""
    return int(BYTES_PER_SEC * duration) + 1500


def max_clip_count(duration: float) -> int:
    """在体积预算内最多能放多少个片段。

    Args:
        duration: 单个片段时长（秒）。

    Returns:
        片段数上限。
    """
    raw_budget = (BUDGET_MB * 1024 * 1024 - HTML_OVERHEAD_BYTES) / BASE64_RATIO
    return max(1, int(raw_budget // clip_bytes(duration)))


def max_duration_for(n_performers: int, min_segments: int = SEG_COUNT_MIN) -> float:
    """给定演奏家数与最少段数，反推允许的最长片段时长。"""
    raw_budget = (BUDGET_MB * 1024 * 1024 - HTML_OVERHEAD_BYTES) / BASE64_RATIO
    return float((raw_budget / (n_performers * min_segments) - 1500) / BYTES_PER_SEC)

# S3 三信号权重（标定后写入 build.log）
W_MS_RATIO = 0.50            # 立体声中/侧比
W_ACTIVITY = 0.30            # 能量包络活跃度
W_FLATNESS = 0.20            # 谱平坦度（取负）


class BuildError(RuntimeError):
    """任何验收项不达标时抛出，构建脚本据此非零退出。"""


@dataclass
class Performer:
    """一位演奏家的登记信息。"""

    id: str
    display: str
    fullName: str
    bvid: str
    parts: list[int]
    orchestra: str = ""
    conductor: str = ""
    nudge: float = 0.0       # 手工对齐微调（秒），§11 的 ±2s nudge


@dataclass
class Project:
    """project.json 的内存投影。"""

    title: str
    composer: str
    movements: int
    anchor: str
    seg_duration: float
    seg_max_count: int
    seg_min_gap: float
    performers: list[Performer]
    seg_mode: str = SEG_MODE
    seg_stride: float = SEG_STRIDE
    pack_mode: str = PACK_MODE
    manual_segments: list[float] = field(default_factory=list)
    presets: list[int] = field(default_factory=list)   # 运行时可选时长（秒，≤ duration）
    raw: dict[str, Any] = field(default_factory=dict)

    # ---- 派生量 -------------------------------------------------
    @property
    def ids(self) -> list[str]:
        """全部演奏家 id，保持 project.json 中的顺序。"""
        return [p.id for p in self.performers]

    @property
    def n(self) -> int:
        """演奏家数量。"""
        return len(self.performers)

    def by_id(self, pid: str) -> Performer:
        """按 id 取演奏家，不存在则报错。"""
        for p in self.performers:
            if p.id == pid:
                return p
        raise BuildError(f"未知演奏家 id: {pid}")

    @property
    def segment_count(self) -> int:
        """段数。

        - inline：由「字节预算 ÷ 演奏家数」推导，再夹到 [5, maxCount]。
          这样改 segment.duration 不会把 HTML 撑爆：时长变长 → 段数自动变少。
        - static：音频外置，不受 HTML 体积约束，段数直接取 maxCount；
          真正能切几段由 S3 的「切到乐曲末尾为止」决定（anchor 长度不足时自然停下）。
        """
        if self.pack_mode == "static":
            return max(SEG_COUNT_MIN, self.seg_max_count)
        allowed = max_clip_count(self.seg_duration) // self.n
        return max(SEG_COUNT_MIN, min(allowed, self.seg_max_count))

    @property
    def clip_count(self) -> int:
        """片段总数（static 模式下是上限，实际以 S3 切出的段数为准）。"""
        return self.n * self.segment_count

    def check_budget(self) -> str:
        """校验时长与段数在体积预算内。

        Returns:
            一行人类可读的预算说明。

        Raises:
            BuildError: 该时长下连最少段数都放不下（仅 inline 模式会触发）。
        """
        if self.pack_mode == "static":
            est = self.clip_count * clip_bytes(self.seg_duration) / 1024 / 1024
            return (f"静态托管：{self.seg_duration:.0f}s × {self.n} 人 × 最多 "
                    f"{self.segment_count} 段 = 最多 {self.clip_count} 片段，音频约 {est:.0f} MB"
                    f"（不计入 HTML；由 Web 服务器按需发送，可覆盖全曲）")
        allowed = max_clip_count(self.seg_duration)
        limit = max_duration_for(self.n, SEG_COUNT_MIN)
        if allowed // self.n < SEG_COUNT_MIN:
            raise BuildError(
                f"{self.seg_duration:.0f} 秒片段在 {self.n} 位演奏家下只能放 {allowed} 个片段"
                f"（需 {self.n * SEG_COUNT_MIN} 个），请把 segment.duration 降到 "
                f"{limit:.0f} 秒以内，或减少演奏家")
        return (f"预算：{self.seg_duration:.0f}s × {self.n} 人 × {self.segment_count} 段 "
                f"= {self.clip_count} 片段（上限 {allowed}，单文件 ≤ {MAX_HTML_MB} MB；"
                f"本配置最长可用 {limit:.0f}s）")


def load_project(path: Path | None = None) -> Project:
    """读取并校验 project.json。

    Args:
        path: project.json 路径，默认取仓库根目录下的文件。

    Returns:
        Project: 校验通过的项目对象。

    Raises:
        BuildError: 字段缺失、id 非法、anchor 不存在、演奏家少于 3 位等。
    """
    path = path or (ROOT / "project.json")
    if not path.exists():
        raise BuildError(f"找不到 {path}")
    data = json.loads(path.read_text(encoding="utf-8"))

    work = data.get("work") or {}
    for key in ("title", "composer"):
        if not work.get(key):
            raise BuildError(f"project.json 缺少 work.{key}")

    seg = data.get("segment") or {}
    performers_raw = data.get("performers") or []
    if not performers_raw:
        raise BuildError("project.json 未登记任何 performers")
    if len(performers_raw) < 3:
        raise BuildError(f"演奏家只有 {len(performers_raw)} 位，少于 3 位无法出题（规格 §5.1）")

    performers: list[Performer] = []
    seen: set[str] = set()
    for idx, raw in enumerate(performers_raw):
        pid = str(raw.get("id", ""))
        if not re.fullmatch(r"[a-z0-9_-]+", pid):
            raise BuildError(f"第 {idx + 1} 位演奏家 id 非法（需小写 ASCII、无空格）: {pid!r}")
        if pid in seen:
            raise BuildError(f"演奏家 id 重复: {pid}")
        seen.add(pid)
        parts = raw.get("parts") or [1]
        if not all(isinstance(x, int) and x >= 1 for x in parts):
            raise BuildError(f"{pid}: parts 必须是正整数列表")
        performers.append(
            Performer(
                id=pid,
                display=str(raw.get("display") or pid),
                fullName=str(raw.get("fullName") or ""),
                bvid=str(raw.get("bvid") or ""),
                parts=list(parts),
                orchestra=str(raw.get("orchestra") or ""),
                conductor=str(raw.get("conductor") or ""),
                nudge=float(raw.get("nudge") or 0.0),
            )
        )
        if not performers[-1].bvid:
            raise BuildError(f"{pid}: 缺少 bvid")

    anchor = str(data.get("anchor") or "")
    if anchor not in seen:
        raise BuildError(f"anchor={anchor!r} 不在 performers 中")

    mode = str(seg.get("mode") or SEG_MODE).strip().lower()
    if mode not in ("sequential", "spread"):
        raise BuildError(f"segment.mode 只能是 sequential 或 spread，实际 {mode!r}")
    stride = float(seg.get("stride", SEG_STRIDE) or 0.0)
    if stride < 0:
        raise BuildError(f"segment.stride 不能为负（实际 {stride}）")
    if 0 < stride < float(seg.get("duration", SEG_DURATION)):
        raise BuildError(f"segment.stride={stride:g}s 小于片段时长 "
                         f"{float(seg.get('duration', SEG_DURATION)):g}s，会让相邻段重叠")

    pack = data.get("pack") or {}
    pack_mode = str(pack.get("mode") or PACK_MODE).strip().lower()
    if pack_mode not in PACK_MODES:
        raise BuildError(f"pack.mode 只能是 {' 或 '.join(PACK_MODES)}，实际 {pack_mode!r}")

    return Project(
        title=str(work["title"]),
        composer=str(work["composer"]),
        movements=int(work.get("movements") or 1),
        anchor=anchor,
        seg_duration=float(seg.get("duration", SEG_DURATION)),
        seg_max_count=int(seg.get("maxCount", SEG_COUNT_MAX)),
        seg_min_gap=float(seg.get("minGap", SEG_MIN_GAP)),
        seg_mode=mode,
        seg_stride=stride,
        pack_mode=pack_mode,
        performers=performers,
        manual_segments=[float(t) for t in (seg.get("manual") or [])],
        presets=sorted({int(x) for x in (seg.get("presets") or [10, 15, 20, 25, 30])
                        if 5 <= int(x) <= int(seg.get("duration", SEG_DURATION))}
                       | {int(seg.get("duration", SEG_DURATION))}),
        raw=data,
    )


def ensure_dirs() -> None:
    """创建所有中间产物目录。"""
    for d in (WORK, DIR_SRC, DIR_PCM, DIR_WARP, DIR_CHECK, DIR_CLIPS, DIR_DIST):
        d.mkdir(parents=True, exist_ok=True)
