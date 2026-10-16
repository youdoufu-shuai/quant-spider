<p align="center"><img src="docs/brand/logo.svg" width="480" alt="Quant Spider"></p>

# Quant Spider · 量化蜘蛛

<p align="center">
  <a href="README.md"><img src="https://img.shields.io/badge/%E8%AF%AD%E8%A8%80-%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-f34a87?style=flat-square" alt="简体中文"></a>
  <a href="README.en.md"><img src="https://img.shields.io/badge/Language-English-3b82f6?style=flat-square" alt="English"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-f0ca57?style=flat-square" alt="License MIT"></a>
  <a href="#quick-start"><img src="https://img.shields.io/badge/Node.js-20%2B-339933?style=flat-square&amp;logo=nodedotjs&amp;logoColor=white" alt="Node.js 20+"></a>
  <a href="#quick-start"><img src="https://img.shields.io/badge/Python-3.12%2B-3776ab?style=flat-square&amp;logo=python&amp;logoColor=white" alt="Python 3.12+"></a>
</p>

> I took [Kirito14IT/prompt-spider-ui](https://github.com/Kirito14IT/prompt-spider-ui) — an animated prompt
> visualization — and turned it into something I actually use: **a quantitative trading dashboard that
> pulls real market data, backtests strategies, and runs a paper-trading account.** The spider web stayed,
> repurposed as process visualization.

**Scope: this is a personal tool, not a product.** Long-only. No live order routing. No auth on the local
services. See [Known limitations](#known-limitations).

---

## What it does

**Real market data, no API key needed.**

| Market | Source | Notes |
|---|---|---|
| A-shares | Sina Finance | 8000+ tickers, searchable |
| Crypto | Binance | |
| US stocks | Sina Finance | |

**27 indicators**, each checked against hand-computed values (Wilder smoothing α=1/n, Bollinger with
population stddev `ddof=0`, MACD histogram doubled per the Chinese convention, and so on).

**5 strategies**: EMA cross, RSI reversion, Bollinger breakout, MACD trend, Donchian breakout.

**Backtester** that refuses to lie about itself: it never uses future data, and it warns instead of
silently computing nonsense when the input prices are broken.

**Paper trading** with realistic constraints: T+1 settlement, price-limit checks, lot-size rounding,
commission and slippage, stop-loss / take-profit / trailing-stop / drawdown circuit breaker.

**AI decision mode** — plug in DeepSeek (the default), OpenAI, a local Ollama model, or any
OpenAI-compatible endpoint. Configured from the UI; **the API key is stored locally only, is never echoed
back to the browser, and is never committed.**

---

## Quick start

Requires **Python 3.12+** and **Node.js 20+**. The only Python dependency is **numpy**.

**Memory footprint is small**: ~110 MB resident for both services, plus a short-lived Python process per
request (peaking at 36–47 MB). A 512 MB VPS is enough. Measured numbers in
[部署内存占用.md](docs/部署内存占用.md) (Chinese).

```bash
pip install numpy

# Terminal 1 — spider UI (the upstream animation viewer)
node server.mjs            # http://127.0.0.1:4317

# Terminal 2 — quant dashboard
node quant/server.mjs      # http://127.0.0.1:4400
```

Then open **http://127.0.0.1:4400/**.

On Windows you can also double-click `启动.cmd` (starts both) or `量化看板.cmd` (quant only).

### Pages

| Path | What it is |
|---|---|
| `/` | Quant dashboard — quote, 27 indicators, 5 strategies, backtest |
| `/pro.html` | Pro chart — crosshair, zoom/pan, parameter optimizer |
| `/decision.html` | **Decision web** — the spider carrying real information (below) |
| `/paper.html` | Paper trading — manual orders, AI auto mode, account persistence |

---

## Connect your agent with MCP

`mcp/` contains a local stdio MCP server that turns the spider UI into something an agent can drive.
The agent creates a session, gets a preview URL, reports real progress as it works, and the browser
shows the mechanical spider, curved flight paths, and three-way routing — a live view of what the agent
is actually doing, rather than a canned animation.

```bash
npm --prefix mcp ci          # installs @modelcontextprotocol/server + zod
node mcp/server.mjs          # or let your MCP client launch it
```

Register it as a **local stdio** MCP server:

```jsonc
{
  "mcpServers": {
    "prompt-spider": {
      "command": "node",
      "args": ["/absolute/path/to/mcp/server.mjs"]
    }
  }
}
```

The tools cover session creation, reporting and updating steps, completing a session, configuring
layout and theme, and exporting either a replay or a standalone editable project. Requires Node.js 20+.

Full tool list, event contract, layouts, theming, and export semantics: [docs/mcp.md](docs/mcp.md)
(Chinese).

---

## The decision web

![Decision web](docs/screenshots/decision-web.png)

The upstream animation drew a *K-line time stream*, and the spider itself just drifted at a hardcoded
speed (`body.vx = 26`, never modified by data). The data layer was real; the spider was decoration.
The problem was the metaphor: **candlesticks aren't a web — factors are.**

A quant pipeline *is* a web:

```
6 factors ──weighted sum──> score ──threshold──> action ──risk gates──> position
    ↑                        ↑                       ↑
 strands on the web     spider's position    whether it gets through
```

So every visual element now encodes real data:

| Element | Encodes |
|---|---|
| Strand **thickness** | factor **weight** (regime-dependent: trending 1.8 / ranging 0.5) |
| Strand **color** | direction (red = bullish, green = bearish) |
| **Bead on strand** | **contribution v×w** — position and brightness both encode it |
| Strand **sag** | absolute strength; strong signals pull taut, weak ones droop |
| Spider **radius** | **score −1~+1** mapped from hub to rim |
| Spider **heading** | current action |
| Spider **brightness** | conviction = \|score\| ÷ entry threshold |
| **Tension arc** | distance to threshold — the arc closing means act |
| **Risk gates** | six locks the spider must get past |

**The most valuable frame is risk interception:**

![Risk interception](docs/screenshots/decision-risk.png)

Three contradictory things visible at once — the factors say *buy* (overbought/oversold +1.00, channel
position +0.97), the signal says *sell* (total −0.44), and the actual order is *sell* because of a
**stop-loss at −12%**, not because of the signal. On screen: the spider is pushed toward SELL, the
stop-loss gate is outlined in red and marked triggered, and the link to the decision box becomes a red
dashed line.

Since quant is **deterministic** — same candles plus same parameters always yield the same factors and
score — "replay the computation" replays the 9 real stages of an actual calculation, not a random
animation.

Details (Chinese): [决策蛛网说明.md](docs/决策蛛网说明.md)

---

## Verification

```bash
# Full standalone suite — offline, no network needed, no pytest
python quant/tests/run.py                    # 1023 assertions across 256 cases

# Unified entry (Python self-tests + Node tests)
node tools/run-tests.mjs --offline

# Per-module self-tests
python quant/decision.py --selftest          # 56
python quant/paper_live.py --selftest        # 62
python quant/autotrader.py --selftest        # 44
```

> **A test-harness bug worth knowing about.** `quant/tests/run.py` used to insert its search paths in
> reverse order, so `import test_indicators` resolved to a stale same-named copy in `quant/` instead of
> the real suite. Result: **213 assertions never ran, while the summary reported "all passed."**
> Fixed by loading test files by explicit path, plus two guards that refuse to start when a same-named
> file exists or when a test file registers too few cases. Details in the `run.py` docstring.

---

## Project structure

```
server.mjs              upstream spider UI server (port 4317)
app/                    upstream animation (Canvas 2D, chart engine)
quant/
  datasource.py         multi-source market data with automatic fallback
  indicators.py         27 indicators
  strategy.py           5 strategies
  backtest.py           backtester
  paper.py              paper-trading engine (replay)
  paper_live.py         paper-trading sessions (persistent)
  autotrader.py         three-layer auto-trading logic
  optimizer.py          parameter grid search
  ai_settings.py        AI provider configuration
  decision.py           decision snapshot (feeds the decision web)
  server.mjs            quant server + API (port 4400)
  web/                  dashboard pages + chart engine + decision web
  tests/                standalone test suite
mcp/                    MCP server (let an agent drive the spider)
tools/                  unified test entry, memory measurement, screenshots
docs/                   documentation and screenshots
```

**Documentation is currently Chinese only.** Good starting points:
[量化模块说明.md](docs/量化模块说明.md) (quant module), [本地部署说明.md](docs/本地部署说明.md) (deployment),
[决策蛛网说明.md](docs/决策蛛网说明.md) (decision web).

---

## Known limitations

Real, and not hidden:

- **Long-only.** No shorting, no leverage, no derivatives.
- **No live order routing.** Paper trading only; nothing here places a real order.
- **The optimizer has no walk-forward validation.** Grid-search results are in-sample and will
  overstate performance.
- **No authentication on either service.** Both bind `127.0.0.1` by default — do not expose them.
- **Docker/Makefile were written but never built or run** (the tooling wasn't available on my machine).
  Treat them as unverified.
- **AI decision quality is unverified** — I have no working API key, so the panel is tested end to end,
  but the model's actual trading calls have never been evaluated.
- **The decision web shows one instant**, not a time series, and its position assumption is set manually
  rather than read from a live paper account.

---

## Sources and license

Forked from [Kirito14IT/prompt-spider-ui](https://github.com/Kirito14IT/prompt-spider-ui) (MIT).
The upstream copyright notice is preserved verbatim in [LICENSE](LICENSE) as the license requires;
the quant module is my own work under the same license.

Market data belongs to its sources (Sina Finance, Binance). **This is not investment advice.**
