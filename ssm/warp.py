"""warp 表的读写与查表（规格 §5.2）。

约定：grid/map 等长，给定 anchor 时间 t 在 grid 上线性插值取 map 中的值。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from . import config


class Warp:
    """一位演奏家的 anchor→目标 时间映射。"""

    def __init__(self, data: dict) -> None:
        """从 warp JSON 构造。

        Args:
            data: work/warp/<id>.json 的内容。
        """
        self.performer_id: str = data["performerId"]
        self.anchor_duration: float = float(data["anchorDuration"])
        self.duration: float = float(data["duration"])
        self.fps: float = float(data["fps"])
        self.mean_cos_sim: float = float(data["meanCosSim"])
        self.grid = np.asarray(data["grid"], dtype=np.float64)
        self.map = np.asarray(data["map"], dtype=np.float64)

    # ---- 查表 ---------------------------------------------------
    def at(self, t_anchor: float) -> float:
        """anchor 时间 → 该演奏家时间（秒，带端点钳制）。"""
        return float(np.interp(t_anchor, self.grid, self.map))

    def inverse_at(self, t_perf: float) -> float:
        """该演奏家时间 → anchor 时间（用于反推乐章边界）。"""
        return float(np.interp(t_perf, self.map, self.grid))

    # ---- 自检 ---------------------------------------------------
    def is_monotonic(self) -> bool:
        """map 是否严格单调不减。"""
        return bool(np.all(np.diff(self.map) >= -1e-9))

    def max_gap(self) -> float:
        """相邻采样点的最大时间跳跃（用于发现插值空洞）。"""
        return float(np.max(np.diff(self.map))) if len(self.map) > 1 else 0.0


def path_of(pid: str) -> Path:
    """warp 文件路径。"""
    return config.DIR_WARP / f"{pid}.json"


def load(pid: str) -> Warp:
    """读取某位演奏家的 warp 表。"""
    return Warp(json.loads(path_of(pid).read_text(encoding="utf-8")))


def load_all(proj) -> dict[str, Warp | None]:
    """读取全部 warp 表，anchor 自身为 None。"""
    out: dict[str, Warp | None] = {}
    for pid in proj.ids:
        if pid == proj.anchor:
            out[pid] = None
        else:
            out[pid] = load(pid)
    return out
