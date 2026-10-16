# syntax=docker/dockerfile:1
# ============================================================================
#  quant-spider 一体化镜像（Node 20 + Python 3 + numpy）
# ----------------------------------------------------------------------------
#  构建：docker build -t quant-spider:latest .
#  起服务：见 docker-compose.yml（**不要**直接用 docker run -p，原因见下）
#
#  ⚠⚠ 未实测声明（诚实优先）：
#      写这份 Dockerfile 的沙箱里**没有 docker**（`docker --version` 不被识别），
#      所以它**没有经过 docker build 验证**，也没有跑过容器。
#      请在有 docker 的机器上先 `docker build -t quant-spider:dev .` 确认再使用；
#      下面的每一处依赖都是按 alpine 官方仓库的包名写的，但没人替你编译过。
#
#  ⚠ 端口发布限制（实测出来的事实，不是猜的）：
#      server.mjs          server.listen(port, '127.0.0.1', ...)          （第 86 行）
#      quant/server.mjs    server.listen(PORT, '127.0.0.1', ...)          （写这份文件时在第 452 行）
#      两个服务都**硬编码绑定 127.0.0.1**，容器里就只监听容器自己的 loopback。
#      因此 `docker run -p 4317:4317` 转发不进去（端口发布了也没人接）。
#      注意：这两个文件正被别人改动，行号会漂 —— 用上面 `server.listen(..., '127.0.0.1', ...)`
#      这段内容去搜，别信行号。
#      两个可选方案：
#        a) Linux 上用 host 网络：docker run --network host ...（docker-compose.yml 用的就是这个）
#        b) 改 server.mjs / quant/server.mjs 的监听地址为 0.0.0.0
#           —— 这属于源码改动，不在本次基建任务的写入范围内，故未改。
#      EXPOSE 只是文档性声明，不会影响实际可达性。
# ============================================================================

FROM node:20-alpine

# ---------------------------------------------------------------------------
# Python 3 + numpy
#
# 为什么这里必须装 numpy（而不是像任务描述里说的「零第三方依赖，不需要 pip install」）：
#   quant/indicators.py:40、backtest.py:49、paper.py:45、strategy.py:49、
#   pipeline.py:30 都在模块顶层 `import numpy as np`。量化看板（quant/server.mjs）
#   每次请求都会 execFile 一个 Python 脚本，没有 numpy 直接 ImportError。
#   开发机上 numpy 是预装的，所以本地「零依赖」的错觉成立；裸镜像里不成立。
#
# 为什么用 apk 的 py3-numpy 而不是 pip：
#   少装一个 pip/编译链，镜像更小，也不用联网取 wheel。
#   如果某个 alpine 版本没有 py3-numpy，退路是：
#     apk add --no-cache python3 py3-pip && pip install --no-cache-dir --break-system-packages numpy
# ---------------------------------------------------------------------------
RUN apk add --no-cache python3 py3-numpy \
 && ln -sf /usr/bin/python3 /usr/local/bin/python

WORKDIR /app

# 项目零 npm 依赖（package.json 没有 dependencies 字段），所以没有 npm install 这一步，
# 也就不需要先 COPY package.json 做层缓存。
COPY . .

# ---------------------------------------------------------------------------
# quant/server.mjs 把开发机的 Windows Python 绝对路径写死成了默认值：
#     const PYTHON = process.env.QUANT_PYTHON
#       || String.raw`C:\Users\24942\.dsh\...\python.exe`;      （写这份文件时在第 32-33 行）
# 容器里必须用 QUANT_PYTHON 覆盖，否则量化看板起不来（会报 execFile ENOENT）。
# 这是实测的（本机 /api/health 会回显它实际用的解释器路径）。
# ---------------------------------------------------------------------------
ENV QUANT_PYTHON=/usr/bin/python3 \
    PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONUNBUFFERED=1

# 4317 = 蜘蛛 UI（server.mjs）
# 4400 = 量化看板（quant/server.mjs）
# 见文件头的「端口发布限制」：EXPOSE 只是声明，实际可达性取决于监听地址。
EXPOSE 4317 4400

# 健康检查走容器内的 loopback，所以不受上面的发布限制影响。
# 用 exec 形式（JSON 数组）而不是 shell 形式：绕开 /bin/sh 的引号解析，少一个失败面。
# ⚠ 未实测（本机无 docker）；但这段 JS 已经在本机用等价脚本验证过：
#    服务在跑 → exit 0；端口关着 → exit 1。
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD ["node", "-e", "fetch('http://127.0.0.1:4317/').then(r => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))"]

# 默认起蜘蛛 UI；量化看板用 `docker run ... node quant/server.mjs`
# 或 docker-compose.yml 里的两个 service。
CMD ["node", "server.mjs"]
