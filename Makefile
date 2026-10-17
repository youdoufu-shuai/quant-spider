# ============================================================================
#  quant-workbench 工程化入口
# ----------------------------------------------------------------------------
#  常用：
#    make test          跑全部自检（Python 9 套，联网套件失败不阻塞）
#    make test-offline  只跑离线套件（无外网 / CI 用）
#    make quant         启动量化工作台  http://127.0.0.1:4400/
#    make lint          语法检查（所有 .mjs + HTML 内联 module 脚本）
#    make docker        构建镜像
#
#  ⚠ 诚实声明：开发这套基建的机器上**没有 make**（`make --version` 不可用），
#    所以这份 Makefile 没有用 make 真跑过。每个配方里的命令都逐条单独实测过；
#    缩进用脚本校验过全部是 TAB。
#    Windows 上想用 make：装 WSL / Git Bash / `scoop install make` 任一即可。
#
#  ⚠ 配方行必须 TAB 缩进，绝不能是空格（GNU make 会直接报 missing separator）。
#
#  想指定解释器：make test PYTHON=/path/to/python
#  （不指定就交给 tools/run-tests.mjs 自己按 $PYTHON → python → python3 → py -3 探测）
# ============================================================================

NODE  ?= node
IMAGE ?= quant-workbench:latest

# 只在调用方显式给了 PYTHON 时才导出，避免把自动探测的兜底链掐掉
ifdef PYTHON
export PYTHON
endif

.DEFAULT_GOAL := help

.PHONY: help test test-offline test-strict python-only lint quant docker compose-up compose-down env

help:
	@echo "quant-workbench 可用目标："
	@echo ""
	@echo "  make test           全部自检（Python 9 套；联网套件失败不阻塞）"
	@echo "  make test-offline   只跑离线套件（无外网环境 / CI 用）"
	@echo "  make test-strict    全部自检，联网套件失败也算失败"
	@echo "  make python-only    只跑 Python 自检"
	@echo "  make lint           语法检查（所有 .mjs + HTML 内联 module 脚本）"
	@echo "  make quant          启动量化工作台  http://127.0.0.1:4400/"
	@echo "  make docker         构建镜像 $(IMAGE)"
	@echo "  make compose-up     用 docker compose 起服务（见 docker-compose.yml 的网络说明）"
	@echo "  make compose-down   停掉 docker compose 服务"
	@echo "  make env            打印 node / python 解析结果"
	@echo ""
	@echo "  端口可用环境变量覆盖：QUANT_PORT（默认 4400）"

test:
	$(NODE) tools/run-tests.mjs

test-offline:
	$(NODE) tools/run-tests.mjs --offline

test-strict:
	$(NODE) tools/run-tests.mjs --strict

python-only:
	$(NODE) tools/run-tests.mjs --python-only

lint:
	$(NODE) tools/run-tests.mjs --syntax-only

quant:
	$(NODE) quant/server.mjs

docker:
	docker build -t $(IMAGE) .

compose-up:
	docker compose up -d --build

compose-down:
	docker compose down

env:
	@echo "NODE   = $(NODE)"
	@echo "IMAGE  = $(IMAGE)"
	@$(NODE) -e "console.log('NODE   =', process.version, process.execPath)"
	@-python -c "import sys, numpy; print('PYTHON =', sys.version.split()[0], sys.executable); print('numpy  =', numpy.__version__)" 2>/dev/null || echo "PYTHON = 未在 PATH 上找到"
