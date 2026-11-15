# 同弦 · 运行镜像
#
# 只负责托管 dist/site 这一份静态站点。构建（下载音轨、对齐、切片）仍在本机跑：
# 那一步要 2.2 GB PCM、几十分钟 CPU 和网络，塞进 NAS 只会把镜像撑成几 GB。
#
# 用法：
#   python build.py                    # 本机先产出 dist/site/
#   docker compose up -d --build       # 再在 NAS 上起容器

FROM nginx:1.27-alpine

# 换掉默认站点（它自带一个欢迎页，和我们的 index.html 抢 / 路径）
RUN rm -f /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/conf.d/same-strings.conf

# 若 dist/site 不存在，这一步会直接失败并报错 —— 比在浏览器里看到空白页好排查
COPY dist/site /usr/share/nginx/html

EXPOSE 443 8443

# 容器内只能走 127.0.0.1，和证书上的域名对不上，所以必须跳过证书校验，
# 否则健康检查永远失败、容器被标记成 unhealthy（服务其实是好的）
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -q --no-check-certificate -O /dev/null https://127.0.0.1/healthz || exit 1
