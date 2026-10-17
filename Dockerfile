# syntax=docker/dockerfile:1
# ============================================================================
#  quant-workbench 镜像（Node 20 + Python 3 + numpy）
# ----------------------------------------------------------------------------
#  构建：docker build -t quant-workbench:latest .
#  起服务：见 docker-compose.yml
#
#  ⚠ 未实测声明（诚实优先）：
#      写这份 Dockerfile 的机器上**没有 docker**（`docker --version` 不被识别），
#      所以它**没有经过 docker build 验证**，也没有跑过容器。
#      请在有 docker 的机器上先 `docker build -t quant-workbench:dev .` 确认再使用。
#
#  ⚠ 监听地址：
#      quant/server.mjs 里 `server.listen(PORT, '127.0.0.1', ...)` —— 硬编码绑 loopback。
#      容器内只监听自己的 loopback，Docker 的 `-p 4400:4400` 从宿主机转发不进去。
#      Linux 上用 `--network host`（docker-compose.yml 就是这么配的）；
#      或者把监听地址改成 0.0.0.0（属于源码改动）。
#      EXPOSE 只是文档性声明，不影响实际可达性。
# ============================================================================

FROM node:20-alpine

# ---------------------------------------------------------------------------
# Python 3 + numpy
#
# 为什么必须装 numpy：
#   quant/indicators.py、backtest.py、paper.py、strategy.py、pipeline.py
#   都在模块顶层 `import numpy as np`。量化看板每次请求都会 execFile 一个
#   Python 脚本，没有 numpy 直接 ImportError。
#   开发机上 numpy 是预装的，所以本地「零依赖」的错觉成立；裸镜像里不成立。
#
# 为什么用 apk 的 py3-numpy 而不是 pip：
#   少装 pip + 编译链，镜像更小，也不用联网取 wheel。
#   如果某个 alpine 版本没有 py3-numpy，退路是：
#     apk add --no-cache python3 py3-pip && pip install --no-cache-dir --break-system-packages numpy
# ---------------------------------------------------------------------------
RUN apk add --no-cache python3 py3-numpy \
 && ln -sf /usr/bin/python3 /usr/local/bin/python

WORKDIR /app

# 项目零 npm 依赖（package.json 没有 dependencies），所以没有 npm install。
COPY . .

# ---------------------------------------------------------------------------
# quant/server.mjs 把开发机的 Windows Python 绝对路径写死成了默认值。
# 容器里必须用 QUANT_PYTHON 覆盖，否则看板起不来（execFile ENOENT）。
# /api/health 会回显它实际用的解释器路径，可用于确认。
# ---------------------------------------------------------------------------
ENV QUANT_PYTHON=/usr/bin/python3 \
    QUANT_PORT=4400 \
    QUANT_HOST=127.0.0.1 \
    PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONUNBUFFERED=1

EXPOSE 4400

# 健康检查：/api/health 是纯本地端点，不触网
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:'+(process.env.QUANT_PORT||4400)+'/api/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "quant/server.mjs"]
