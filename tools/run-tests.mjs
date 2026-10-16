#!/usr/bin/env node
/**
 * ============================================================================
 *  quant-spider 统一测试入口
 * ============================================================================
 *
 *  用法：
 *    node tools/run-tests.mjs                 全部套件（联网套件失败不算失败，见下）
 *    node tools/run-tests.mjs --offline       跳过联网套件（离线可用）
 *    node tools/run-tests.mjs --network-only  只跑联网套件
 *    node tools/run-tests.mjs --strict        联网套件失败也判定为失败
 *    node tools/run-tests.mjs --python-only   只跑 Python 自检
 *    node tools/run-tests.mjs --node-only     只跑 Node 测试
 *    node tools/run-tests.mjs --syntax-only   只做语法检查（所有 .mjs + HTML 内联 module）
 *    node tools/run-tests.mjs --list          只列套件，不执行
 *    node tools/run-tests.mjs --verbose       打印每个套件的完整输出
 *
 *  退出码：0 = 全部必需套件通过；1 = 有失败或运行环境不可用；2 = 参数用法错误
 *
 *  实测基线（2026-10，Windows / Node 24 / Python 3.12 与 3.13 均通过）：
 *    indicators.py --selftest    23 断言   离线
 *    test_indicators.py          55 断言   离线
 *    backtest.py --selftest      20 断言   离线
 *    paper.py --selftest         20 断言   离线
 *    paper_live.py --selftest    62 断言   离线
 *    autotrader.py --selftest    44 断言   离线
 *    datasource.py --selftest     7 断言   ⚠ 联网（真实抓新浪行情）
 *    symbols.py --selftest       25 断言   ⚠ 联网（首次要枚举 8265 只 A 股 + 511 个币对，约 98s）
 *    tests/*.test.mjs            44 通过 + 1 跳过
 *    离线合计 224 项 Python 断言；含联网共 256 项。
 *
 *  为什么联网套件默认不阻塞：
 *    datasource / symbols 会真的请求外部行情源（新浪等），并且 symbols 首次运行要重建
 *    quant/state/symbols-cache.json（该文件被 .gitignore 排除，干净检出时必然不存在）。
 *    在 CI runner 上这属于「外部服务可用性」而不是代码回归，不该阻塞合并。
 *    需要严格判定时显式加 --strict。
 *
 *  设计约束（踩过的坑，别改回去）：
 *    · 零第三方依赖：只用 node 标准库。
 *    · **不创建任何临时文件**——尤其不建 enum.py / struct.py / types.py / json.py
 *      这类会遮蔽 Python 标准库的文件名（本项目已因此挂过两次）。HTML 内联 module
 *      的语法检查走 `node --check --input-type=module -` 从 stdin 读，完全不落盘。
 *    · Python 解释器按 $PYTHON → python → python3 → py -3 顺序逐个探测。Windows 上
 *      python3 常常是应用商店的假存根（实测退出码 9009），必须实测才认。
 *    · 子进程一律传参数数组、不经 shell，所以含空格的安装路径也安全。
 *    · stdio 用 ['ignore','pipe','pipe']：stdin 关掉（子进程不可能卡在等输入）、
 *      stdout/stderr 都捕获（失败时把 stderr 一起打进「子进程输出」尾部，否则
 *      python 缺文件这种错误在汇总里只显示「计数未知」，看不出原因）。
 *      代价：套件运行期间父进程不实时转发子进程输出，只有跑完才打印——所以每个套件
 *      执行前都会先打印它将要跑的命令行。
 * ============================================================================
 */

import { execFileSync } from 'node:child_process';
import { readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const NODE = process.execPath;                 // 跑本脚本的 node 就是被测的 node
const RULE = '='.repeat(74);
const MAX_BUFFER = 64 * 1024 * 1024;
const PY_TIMEOUT_MS = 120000;                  // 离线套件实测 < 1s，留足余量
const NET_TIMEOUT_MS = 600000;                 // symbols 首次枚举实测约 98s
const NODE_TIMEOUT_MS = 300000;

const USAGE = `quant-spider 统一测试入口

  node tools/run-tests.mjs                 全部套件（联网套件失败不判定为失败）
  node tools/run-tests.mjs --offline       跳过联网套件
  node tools/run-tests.mjs --network-only  只跑联网套件
  node tools/run-tests.mjs --strict        联网套件失败也判定为失败
  node tools/run-tests.mjs --python-only   只跑 Python 自检
  node tools/run-tests.mjs --node-only     只跑 Node 测试
  node tools/run-tests.mjs --syntax-only   只做语法检查
  node tools/run-tests.mjs --list          只列套件，不执行
  node tools/run-tests.mjs --verbose       打印每个套件的完整输出

退出码：0 = 通过；1 = 有失败或环境不可用；2 = 参数用法错误`;

// ── 套件定义 ────────────────────────────────────────────────────────────────
// 与 .github/workflows/ci.yml 的步骤一一对应；改这里记得同步改 YAML。
const PY_SUITES = [
  { id: 'indicators',      label: 'indicators.py --selftest',   script: 'quant/indicators.py',      args: ['--selftest'], network: false },
  { id: 'test_indicators', label: 'test_indicators.py（回归）',  script: 'quant/test_indicators.py', args: [],             network: false },
  { id: 'backtest',        label: 'backtest.py --selftest',     script: 'quant/backtest.py',        args: ['--selftest'], network: false },
  { id: 'paper',           label: 'paper.py --selftest',        script: 'quant/paper.py',           args: ['--selftest'], network: false },
  { id: 'paper_live',      label: 'paper_live.py --selftest',   script: 'quant/paper_live.py',      args: ['--selftest'], network: false },
  { id: 'autotrader',      label: 'autotrader.py --selftest',   script: 'quant/autotrader.py',      args: ['--selftest'], network: false },
  { id: 'optimizer',       label: 'optimizer.py --selftest',    script: 'quant/optimizer.py',       args: ['--selftest'], network: false },
  { id: 'ai_settings',     label: 'ai_settings.py --selftest',  script: 'quant/ai_settings.py',     args: ['--selftest'], network: false },
  { id: 'datasource',      label: 'datasource.py --selftest',   script: 'quant/datasource.py',      args: ['--selftest'], network: true  },
  { id: 'symbols',         label: 'symbols.py --selftest',      script: 'quant/symbols.py',         args: ['--selftest'], network: true  },
];

const KNOWN_FLAGS = new Set([
  '--offline', '--network-only', '--python-only', '--node-only',
  '--syntax-only', '--strict', '--list', '--verbose',
]);

// 语法检查时跳过的目录（版本库元数据、Chrome profile、本机数据与被忽略的产物）
const SKIP_DIRS = new Set(['node_modules', 'exports', 'reference', 'research', 'output', 'scripts', 'dist', 'build', 'coverage', 'state']);

const INLINE_SCRIPT_RE = /<script\b([^>]*)>([\s\S]*?)<\/script\s*>/gi;
const MODULE_ATTR_RE = /\btype\s*=\s*["']?module["']?/i;

// ── 主流程 ──────────────────────────────────────────────────────────────────
function main(argv) {
  const bad = argv.filter((a) => !KNOWN_FLAGS.has(a));
  if (bad.length > 0) {
    console.error(`未知参数：${bad.join(' ')}`);
    console.error(`可用参数：${[...KNOWN_FLAGS].join(' ')}`);
    process.exitCode = 2;
    return;
  }
  const flag = (f) => argv.includes(f);
  const opt = {
    offline: flag('--offline'),
    networkOnly: flag('--network-only'),
    pythonOnly: flag('--python-only'),
    nodeOnly: flag('--node-only'),
    syntaxOnly: flag('--syntax-only'),
    strict: flag('--strict'),
    list: flag('--list'),
    verbose: flag('--verbose'),
  };

  if (opt.offline && opt.networkOnly) return usageError('--offline 与 --network-only 不能同时使用');
  if (opt.pythonOnly && opt.nodeOnly) return usageError('--python-only 与 --node-only 不能同时使用');
  if (opt.syntaxOnly && (opt.offline || opt.networkOnly || opt.pythonOnly || opt.nodeOnly || opt.strict)) {
    return usageError('--syntax-only 不能与其它筛选参数同时使用');
  }

  // 套件选择规则（简单可预测）：
  //   --syntax-only → 只做语法检查
  //   --network-only → 只跑联网 Python 套件（不含 Node，因为 Node 测试本来就不联网）
  //   其余情况 → Python 套件（默认全部，--offline 时排除联网）+ Node 测试（除非 --python-only）
  const wantSyntax = opt.syntaxOnly;
  const wantNode = !opt.syntaxOnly && !opt.networkOnly && !opt.pythonOnly;
  const wantPython = !opt.syntaxOnly && !opt.nodeOnly;

  const pySuites = wantPython
    ? PY_SUITES.filter((s) => (opt.networkOnly ? s.network : true)).filter((s) => (opt.offline ? !s.network : true))
    : [];

  if (opt.list) {
    printPlan(pySuites, wantNode, wantSyntax);
    return;
  }

  // 只有真的要跑 Python 时才去找解释器——--node-only 在没有 Python 的机器上也得能用
  let py = null;
  if (pySuites.length > 0) {
    py = resolvePython();
    if (!py) {
      console.error('找不到可用的 Python 解释器（需要 Python 3.10+ 且带 numpy）。');
      console.error('已尝试：$PYTHON → python → python3 → py -3');
      console.error('请安装 Python 或用 PYTHON=/path/to/python 显式指定。');
      process.exitCode = 1;
      return;
    }
  }

  const results = [];
  const startedAt = Date.now();

  console.log(RULE);
  console.log('  quant-spider 统一测试入口');
  console.log(`  仓库   ${ROOT}`);
  console.log(`  Node   ${process.version}  (${NODE})`);
  if (py) console.log(`  Python ${py.version}  (${[py.cmd, ...py.pre].join(' ')})`);
  console.log(RULE);
  console.log('');

  // ── Python 自检 ───────────────────────────────────────────────────────────
  if (pySuites.length > 0) {
    console.log('[ Python 自检 ]');
    for (const s of pySuites) {
      const cmdline = `${[py.cmd, ...py.pre].join(' ')} ${s.script}${s.args.length ? ' ' + s.args.join(' ') : ''}`;
      console.log(`  → ${s.label}${s.network ? '   ⚠ 需要联网' : ''}`);
      console.log(`    $ ${cmdline}`);
      const r = exec([py.cmd, ...py.pre, s.script, ...s.args], {
        timeout: s.network ? NET_TIMEOUT_MS : PY_TIMEOUT_MS,
      });
      const counts = parseCounts(r.stdout) || parseCounts(r.output) || { pass: 0, fail: 0 };
      const ok = r.ok && counts.fail === 0;
      results.push({ kind: 'python', ...s, cmdline, ...r, counts, ok });
      reportLine(ok, s, counts, r, opt.verbose);
    }
    console.log('');
  }

  // ── Node 测试 ─────────────────────────────────────────────────────────────
  if (wantNode) {
    const files = nodeTestFiles();
    console.log('[ Node 测试 ]');
    if (files.length === 0) {
      console.log('  ⚠ tests/*.test.mjs 一个都没有，跳过');
    } else {
      const rel = files.map((f) => path.relative(ROOT, f));
      const label = `tests/*.test.mjs（${rel.length} 个文件）`;
      console.log(`  → ${label}`);
      console.log(`    $ node --test ${rel.join(' ')}`);
      const r = exec([NODE, '--test', '--test-reporter=tap', ...rel], { timeout: NODE_TIMEOUT_MS });
      const counts = parseCounts(r.stdout) || { pass: 0, fail: 0 };
      const ok = r.ok && counts.fail === 0;
      results.push({ kind: 'node', id: 'node-tests', label, network: false, ...r, counts, ok });
      reportLine(ok, { label, network: false }, counts, r, opt.verbose);
    }
    console.log('');
  }

  // ── 语法检查 ──────────────────────────────────────────────────────────────
  if (wantSyntax) {
    console.log('[ 语法检查 ]');
    const syntax = runSyntaxChecks(opt.verbose);
    for (const item of syntax.checks) {
      if (item.ok) {
        if (opt.verbose) console.log(`    [OK]   ${item.label}`);
      } else {
        console.log(`    [FAIL] ${item.label}`);
        for (const line of String(item.detail).split('\n').slice(0, 12)) console.log(`           ${line}`);
      }
    }
    const badCount = syntax.checks.filter((c) => !c.ok).length;
    console.log(`  → 检查 ${syntax.checks.length} 个目标（${syntax.mjsCount} 个 .mjs + ${syntax.inlineCount} 段内联 module）：${badCount === 0 ? '全部通过' : `${badCount} 个失败`}`);
    results.push({
      kind: 'syntax', id: 'syntax', label: '语法检查', network: false, ok: badCount === 0,
      status: badCount === 0 ? 0 : 1, stdout: '', output: '', cmdline: 'node --check',
      counts: { pass: syntax.checks.length - badCount, fail: badCount },
    });
    console.log('');
  }

  summarize(results, opt, startedAt);
}

// ── 汇总 ────────────────────────────────────────────────────────────────────
function summarize(results, opt, startedAt) {
  const elapsed = ((Date.now() - startedAt) / 1000).toFixed(1);
  const hardFail = results.filter((r) => !r.ok && !(r.network && !opt.strict));
  const softFail = results.filter((r) => !r.ok && r.network && !opt.strict);
  const pyResults = results.filter((r) => r.kind === 'python');
  const nodeResults = results.filter((r) => r.kind === 'node');
  const syntaxResult = results.find((r) => r.kind === 'syntax');
  const pyAssertions = pyResults.filter((r) => r.ok).reduce((n, r) => n + r.counts.pass, 0);

  console.log(RULE);
  if (hardFail.length === 0) {
    // 有非阻塞失败时，第一个数字必须是「真正通过的套件数」，断言数也只统计通过的那些，
    // 否则会出现「8 套自检全部通过」但其中一套其实报红的误导性汇总。
    const passedPy = pyResults.filter((r) => r.ok);
    const parts = [];
    if (pyResults.length > 0) {
      const suiteCount = softFail.length > 0 ? `${passedPy.length} 套 Python 自检` : `${pyResults.length} 套 Python 自检`;
      parts.push(`${suiteCount} / ${pyAssertions} 断言`);
    }
    if (nodeResults.length > 0) {
      const n = nodeResults[0].counts;
      parts.push(`1 套 Node 测试 / ${n.pass + n.fail + (n.skipped || 0)} 用例（${n.pass} 通过${n.skipped ? ` + ${n.skipped} 跳过` : ''}）`);
    }
    if (syntaxResult) parts.push(`${syntaxResult.counts.pass} 个语法检查目标`);
    console.log(`  ${softFail.length > 0 ? '必需套件全部通过' : '全部通过'}：${parts.join('  ·  ')}`);
    if (softFail.length > 0) {
      const detail = softFail.map((r) => `${r.label}（${r.counts.pass} 通过 / ${r.counts.fail} 失败）`).join('、');
      console.log(`  ⚠ ${softFail.length} 套联网套件失败（非阻塞，未计入上面的断言数）：${detail}`);
      console.log('    联网套件依赖外部行情源，失败属于外部服务可用性；加 --strict 可让它判定为失败。');
    }
    console.log(`  耗时 ${elapsed}s`);
    console.log(RULE);
    process.exitCode = 0;
    return;
  }

  console.log(`  失败：${hardFail.length} 套件未通过（耗时 ${elapsed}s）`);
  for (const r of hardFail) {
    console.log(`    - ${r.label}  (exit=${r.status}${r.timedOut ? ', 超时' : ''}${r.counts && r.counts.fail ? `, ${r.counts.fail} 项失败` : ''})`);
  }
  if (softFail.length > 0) {
    console.log(`  ⚠ 另有 ${softFail.length} 套联网套件失败（非阻塞）：${softFail.map((r) => r.label).join('、')}`);
  }
  console.log(RULE);
  process.exitCode = 1;
}

function usageError(message) {
  console.error(message);
  console.error('');
  console.error(USAGE);
  process.exitCode = 2;
}

function printPlan(pySuites, wantNode, wantSyntax) {
  console.log(RULE);
  console.log('  套件清单');
  console.log(RULE);
  for (const s of pySuites) {
    // 不用 padEnd 对齐：中文是双宽字符，按码点补空格只会更乱
    console.log(`  [Python]${s.network ? ' ⚠联网' : '      '} ${s.label}  →  ${s.script} ${s.args.join(' ')}`.trimEnd());
  }
  if (wantNode) {
    const rel = nodeTestFiles().map((f) => path.relative(ROOT, f));
    console.log(`  [Node]          node --test ${rel.length} 个文件：${rel.join(' ')}`);
  }
  if (wantSyntax) console.log('  [Syntax]        所有 .mjs + HTML 内联 <script type="module">');
  console.log(RULE);
}

function reportLine(ok, suite, counts, r, verbose) {
  const mark = ok ? '[PASS]' : (suite.network ? '[WARN]' : '[FAIL]');
  const count = counts.pass || counts.fail ? `${counts.pass} 通过 / ${counts.fail} 失败` : '计数未知';
  const secs = r.ms === undefined ? '' : `  ${(r.ms / 1000).toFixed(2)}s`;
  console.log(`    ${mark} ${count}${secs}${r.ok ? '' : `  (exit=${r.status}${r.timedOut ? ' 超时' : ''})`}`);
  if (!ok || verbose) {
    const lines = String(r.output || r.stdout || '').split('\n');
    const tail = lines.slice(-(verbose ? lines.length : 30)).filter((l) => l.trim() !== '');
    if (tail.length > 0) {
      console.log('    ┌─ 子进程输出（尾部）');
      for (const l of tail) console.log(`    │ ${l}`);
      console.log('    └─');
    }
  }
}

// ── 断言计数解析 ────────────────────────────────────────────────────────────
// 兼容项目内既有的三种格式 + node:test 的 TAP 汇总：
//   "  自检: 23 通过 / 0 失败"            indicators/backtest/paper/paper_live/autotrader/symbols
//   "  自检: 7 成功 / 0 失败"             datasource 用的是「成功」
//   "  全部通过：55 项断言"                test_indicators.py 全通过时
//   "  50 通过 / 3 失败"                  test_indicators.py 有失败时
//   "# pass 44 / # fail 0 / # skipped 1"  node --test --test-reporter=tap
function parseCounts(text) {
  const t = String(text || '');
  let m = t.match(/(\d+)\s*(?:通过|成功)\s*\/\s*(\d+)\s*失败/);
  if (m) return { pass: Number(m[1]), fail: Number(m[2]) };
  m = t.match(/全部通过\s*[：:]\s*(\d+)\s*项断言/);
  if (m) return { pass: Number(m[1]), fail: 0 };
  m = t.match(/^#\s*pass\s+(\d+)\s*$/m);
  if (m) {
    const f = t.match(/^#\s*fail\s+(\d+)\s*$/m);
    const s = t.match(/^#\s*skipped\s+(\d+)\s*$/m);
    return { pass: Number(m[1]), fail: f ? Number(f[1]) : 0, skipped: s ? Number(s[1]) : 0 };
  }
  return null;
}

// ── 子进程执行 ──────────────────────────────────────────────────────────────
function exec(cmdArgs, { timeout, input } = {}) {
  const [cmd, ...args] = cmdArgs;
  const started = Date.now();
  const base = {
    cwd: ROOT,
    encoding: 'utf8',
    maxBuffer: MAX_BUFFER,
    timeout: timeout || PY_TIMEOUT_MS,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
  };
  try {
    const opts = input === undefined
      ? { ...base, stdio: ['ignore', 'pipe', 'pipe'] }
      : { ...base, stdio: ['pipe', 'pipe', 'pipe'], input };
    const stdout = execFileSync(cmd, args, opts);
    return { ok: true, status: 0, stdout: String(stdout), output: String(stdout), ms: Date.now() - started, timedOut: false };
  } catch (e) {
    const stdout = e.stdout ? String(e.stdout) : '';
    const stderr = e.stderr ? String(e.stderr) : '';
    return {
      ok: false,
      status: typeof e.status === 'number' ? e.status : -1,
      stdout,
      output: `${stdout}\n${stderr}`.trim(),
      error: e.message,
      timedOut: e.code === 'ETIMEDOUT' || e.signal === 'SIGTERM',
      ms: Date.now() - started,
    };
  }
}

// ── Python 解释器探测 ───────────────────────────────────────────────────────
function resolvePython() {
  const explicit = process.env.PYTHON;
  const candidates = explicit
    ? [{ cmd: explicit, pre: [] }]
    : [
        { cmd: 'python', pre: [] },
        { cmd: 'python3', pre: [] },
        { cmd: 'py', pre: ['-3'] },
      ];
  for (const c of candidates) {
    const probe = exec([c.cmd, ...c.pre, '-c', 'import sys; print(sys.version.split()[0])'], { timeout: 20000 });
    const version = probe.stdout.trim();
    if (probe.ok && /^\d+\.\d+/.test(version)) return { ...c, version };
    if (explicit) console.error(`$PYTHON 指向的解释器不可用：${explicit}（${probe.error || `exit=${probe.status}`}）`);
  }
  return null;
}

// ── 语法检查 ────────────────────────────────────────────────────────────────
function runSyntaxChecks(verbose) {
  const files = listFiles(ROOT);
  const mjs = files.filter((f) => f.endsWith('.mjs')).sort();
  const html = files.filter((f) => f.endsWith('.html')).sort();
  const checks = [];

  for (const file of mjs) {
    const label = path.relative(ROOT, file);
    const r = exec([NODE, '--check', label], { timeout: 60000 });
    checks.push({ ok: r.ok, label, detail: r.output || r.error || '' });
  }

  let inlineCount = 0;
  for (const file of html) {
    const rel = path.relative(ROOT, file);
    let html;
    try {
      html = readFileSync(file, 'utf8');
    } catch (e) {
      checks.push({ ok: false, label: `${rel}（读取失败）`, detail: e.message });
      continue;
    }
    INLINE_SCRIPT_RE.lastIndex = 0;
    let m;
    let n = 0;
    while ((m = INLINE_SCRIPT_RE.exec(html)) !== null) {
      if (!MODULE_ATTR_RE.test(m[1])) continue;
      const code = m[2];
      if (!code.trim()) continue;               // <script type="module" src="..."> 没有内联体
      n += 1;
      inlineCount += 1;
      const line = html.slice(0, m.index).split('\n').length;
      // 从 stdin 读，不落盘（避免创建任何临时文件）
      const r = exec([NODE, '--check', '--input-type=module', '-'], { timeout: 60000, input: code });
      checks.push({ ok: r.ok, label: `${rel}:${line} 内联 module`, detail: r.output || r.error || '' });
    }
    if (verbose && n === 0) console.log(`    [SKIP] ${rel} 没有内联 module 脚本`);
  }

  return { checks, mjsCount: mjs.length, inlineCount };
}

// ── 目录遍历 ────────────────────────────────────────────────────────────────
function listFiles(dir, out = []) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.name.startsWith('.')) continue;                 // .git / .shot-profile / .vc 等
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (SKIP_DIRS.has(entry.name)) continue;
      listFiles(full, out);
    } else if (entry.isFile()) {
      out.push(full);
    }
  }
  return out;
}

function nodeTestFiles() {
  const prefix = path.join(ROOT, 'tests') + path.sep;
  try {
    return listFiles(ROOT).filter((f) => f.startsWith(prefix) && f.endsWith('.test.mjs')).sort();
  } catch {
    return [];
  }
}

// ── 入口 ────────────────────────────────────────────────────────────────────
const ARGV = process.argv.slice(2);
if (ARGV.includes('--help') || ARGV.includes('-h')) {
  console.log(USAGE);
  process.exitCode = 0;
} else {
  main(ARGV);
}
