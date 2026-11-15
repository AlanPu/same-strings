"""同弦 · 构建管线。

模块划分：
  config    参数与 project.json 校验
  logbook   build.log / build-report.json
  audio     ffmpeg / ffprobe 封装
  warp      warp 表读写与查表
  s0..s5    六个构建阶段
  verify    §10 验收总表
"""
