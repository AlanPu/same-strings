#!/bin/sh
# 同弦 · 证书续签检查（在 NAS 上定时跑，root）
#
# 为什么需要它：nginx 只在启动时把证书读进内存，DSM 续签不会通知容器。
# 光靠「每次重容器重建」太粗暴，靠「人记得手动 cp」更不可靠。
#
# 这个脚本比对三个指纹，按需做最小动作：
#   HOST  宿主机 DSM 证书库里的当前证书
#   MOUNT 容器内 /dsm-certs 看到的（只读 bind mount）
#   LIVE  容器内 /etc/nginx/certs（nginx 实际加载的那份副本）
#
#   HOST == MOUNT == LIVE  → 什么都没变，安静退出
#   HOST == MOUNT != LIVE  → 续签了但 nginx 还在用旧的：重新同步 + reload（零中断）
#   HOST != MOUNT          → bind mount 钉死在旧目录/旧 inode 上，容器看不到新文件：
#                            必须 force-recreate 重建挂载（几秒中断），不能只 reload
#
# 最后一种情况是这个方案唯一会「静默失效」的地方：万一 DSM 续签时把整个
# system/default 目录换掉而不是改里面的文件，mount 仍指向旧 inode。
# 不查这一条，脚本会一直用旧证书 reload，直到证书过期才发现。
#
# 装到 DSM：控制面板 → 任务计划 → 新增 → 计划的任务 → 用户定义的脚本
#   用户：root　计划：每周一次（例如周日 04:00）
#   命令：/volume1/docker/same-strings/renew_cert_check.sh
# 任务计划以 root 运行，既能读 DSM 的 root:r-------- 证书，也能调 docker。

set -eu

PROJECT_DIR="${PROJECT_DIR:-/volume1/docker/same-strings}"
CERT_DIR="${DSM_CERT_DIR:-/usr/syno/etc/certificate/system/default}"
CONTAINER="${CONTAINER:-same-strings}"
DOCKER="${DOCKER:-/usr/local/bin/docker}"

log() { echo "[renew-cert] $*"; }

sum() { sha256sum "$1" 2>/dev/null | cut -d' ' -f1; }
in_container_sum() { "$DOCKER" exec "$CONTAINER" sha256sum "$1" 2>/dev/null | cut -d' ' -f1; }

host=$(sum "$CERT_DIR/fullchain.pem")
if [ -z "$host" ]; then
    log "错误：读不到 $CERT_DIR/fullchain.pem，本脚本必须以 root 运行"
    exit 1
fi

if ! "$DOCKER" ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    log "容器 $CONTAINER 没在跑，重建（entrypoint 会自己同步证书）"
    cd "$PROJECT_DIR"
    "$DOCKER" compose up -d --force-recreate --no-build
    exit 0
fi

mount=$(in_container_sum /dsm-certs/fullchain.pem)
live=$(in_container_sum /etc/nginx/certs/fullchain.pem)

if [ "$mount" != "$host" ]; then
    log "bind mount 陈旧（宿主机已换证书，容器挂载仍指向旧 inode），重建容器"
    cd "$PROJECT_DIR"
    "$DOCKER" compose up -d --force-recreate --no-build
    log "重建完成，新证书指纹 ${host}"
    exit 0
fi

if [ "$live" != "$host" ]; then
    log "检测到续签（旧 ${live} → 新 ${host}），同步并 reload"
    "$DOCKER" exec "$CONTAINER" /docker-entrypoint.d/40-sync-dsm-cert.sh
    "$DOCKER" exec "$CONTAINER" nginx -s reload
    log "已 reload，服务未中断"
    exit 0
fi

log "证书未变化（${host}），无需操作"
