// Headless KaTeX 硬报错扫描器(无人值守产 render_errors.json)。
// 用 vendored katex.mjs 对 md 里每个 $…$ / $$…$$ 跑 renderToString({throwOnError:true}),
// 捕获抛错者。与浏览器工具共用同版本 KaTeX,判红一致。
// 页归属:靠 md 里的 <!-- page: N --> 注释(python 端逐页 reconstruct 时插入);无注释则 page=null。
//
// 用法: node scan_katex_errors.mjs --md <path> [--out <path>] [--label <name>]
import katex from './vendor/katex.mjs';
import fs from 'node:fs';
import { pathToFileURL } from 'node:url';

function arg(name, def = null) {
  const i = process.argv.indexOf(name);
  return i !== -1 && i + 1 < process.argv.length ? process.argv[i + 1] : def;
}

function isEscaped(md, pos) {
  let n = 0;
  for (let i = pos - 1; i >= 0 && md[i] === '\\'; i--) n++;
  return n % 2 === 1;
}

function isMarkdownBoundary(md, pos) {
  if (md.startsWith('<!--', pos)) return true;
  if (md.startsWith('\n\n', pos)) return true;
  if (md.startsWith('\r\n\r\n', pos)) return true;   // CRLF 段落分隔(md 为 \r\n 换行)
  if (md.startsWith('![', pos)) return true;
  if (md.startsWith('\n![', pos)) return true;
  return false;
}

function findDisplayEnd(md, start) {
  for (let i = start; i < md.length - 1; i++) {
    if (md.startsWith('$$', i) && !isEscaped(md, i)) return i;
  }
  return -1;
}

function findInlineEnd(md, start) {
  for (let i = start; i < md.length; i++) {
    if (isMarkdownBoundary(md, i)) return -1;
    if (md[i] !== '$' || isEscaped(md, i)) continue;
    if (md[i - 1] === '$' || md[i + 1] === '$') return -1;
    return i;
  }
  return -1;
}

// 左到右分词:先认 $$ 再认 $;\$ 转义不当分隔符;<!-- page: N --> 更新当前页。
// 跳过 ``` / ~~~ 围栏代码块:代码里的 $(如 BASIC 字符串变量 A$=INKEY$)不是数学,
// Typora 也不当公式渲染;不跳会把整段代码误判成 $…$ 报红(假阳性)。
export function extractMath(md) {
  const out = [];
  let i = 0, page = null, block_ids = [];
  let inFence = false, fenceChar = '';
  const atLineStart = (pos) => pos === 0 || md[pos - 1] === '\n';
  while (i < md.length) {
    if (atLineStart(i)) {
      const m = md.slice(i).match(/^[ \t]*(`{3,}|~{3,})/);
      if (m) {
        const ch = m[1][0];
        if (!inFence) { inFence = true; fenceChar = ch; }
        else if (ch === fenceChar) { inFence = false; fenceChar = ''; }
        const nl = md.indexOf('\n', i);
        i = nl === -1 ? md.length : nl + 1;
        continue;
      }
    }
    if (inFence) { i++; continue; }
    if (md.startsWith('<!-- page:', i)) {
      const end = md.indexOf('-->', i);
      if (end !== -1) {
        const comment = md.slice(i, end);
        const m = comment.match(/page:\s*(\d+)/);
        if (m) page = parseInt(m[1], 10);
        const b = comment.match(/block_ids:\s*([0-9,\s]+)/);
        block_ids = b ? b[1].split(',').map((x) => parseInt(x.trim(), 10)).filter((x) => !Number.isNaN(x)) : [];
        i = end + 3;
        continue;
      }
    }
    if (md[i] === '$' && !isEscaped(md, i)) {
      const display = md[i + 1] === '$';
      const delim = display ? '$$' : '$';
      const start = i + delim.length;
      const end = display ? findDisplayEnd(md, start) : findInlineEnd(md, start);
      if (end === -1) { i += display ? 2 : 1; continue; }
      const latex = md.slice(start, end).trim();
      const tag = latex.match(/\\tag\{([^}]*)\}/);
      out.push({
        page,
        block_ids,
        formula_number: tag ? tag[1] : null,
        mode: display ? 'display' : 'inline',
        latex,
      });
      i = end + delim.length;
      continue;
    }
    i++;
  }
  return out;
}

export function scan(md) {
  const formulas = extractMath(md);
  const errors = [];      // 硬报错(抛错 → 红)
  const warnings = [];    // strict warning(不抛错但 LaTeX 不兼容/字形缺失 → 潜在渲染异常)
  formulas.forEach((f, index) => {
    const seen = new Set();
    const strict = (code, msg) => {
      const key = code + '|' + msg;
      if (!seen.has(key)) {
        seen.add(key);
        warnings.push({ index, page: f.page, mode: f.mode, code,
          block_ids: f.block_ids, formula_number: f.formula_number,
          message: String(msg).split('\n')[0], latex_head: f.latex.slice(0, 90) });
      }
      return 'warn';       // 不升级为 error,保持默认渲染行为
    };
    try {
      katex.renderToString(f.latex, { throwOnError: true, strict, displayMode: f.mode === 'display' });
    } catch (e) {
      errors.push({
        index, page: f.page, mode: f.mode,
        block_ids: f.block_ids, formula_number: f.formula_number,
        error: String(e.message).split('\n')[0],
        latex_head: f.latex.slice(0, 90),
      });
    }
  });
  return { total: formulas.length, errors, warnings };
}

// 直接执行(非 import)时跑 CLI
if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  const mdPath = arg('--md');
  if (!mdPath) { console.error('缺 --md <path>'); process.exit(2); }
  const label = arg('--label', mdPath);
  const md = fs.readFileSync(mdPath, 'utf-8');
  const { total, errors, warnings } = scan(md);
  const out = arg('--out');
  if (out) {
    fs.writeFileSync(out, JSON.stringify(
      { label, total, error_count: errors.length, warning_count: warnings.length, errors, warnings },
      null, 2), 'utf-8');
  }
  console.log(`[${label}] 公式 ${total} | 硬报错 ${errors.length} | 警告 ${warnings.length}`);
  for (const e of errors) {
    console.log(`  [红] p${e.page ?? '?'} ${e.mode} | ${e.error}`);
    console.log(`       ${e.latex_head}`);
  }
  const wpages = [...new Set(warnings.map((w) => w.page))];
  for (const w of warnings) {
    console.log(`  [警告] p${w.page ?? '?'} ${w.mode} | ${w.message}`);
  }
  if (warnings.length) console.log(`  警告涉及页: ${wpages.join(', ')}`);
}
