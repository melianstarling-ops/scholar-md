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

// 左到右分词:先认 $$ 再认 $;\$ 转义不当分隔符;<!-- page: N --> 更新当前页。
export function extractMath(md) {
  const out = [];
  let i = 0, page = null;
  while (i < md.length) {
    if (md.startsWith('<!-- page:', i)) {
      const end = md.indexOf('-->', i);
      if (end !== -1) {
        const m = md.slice(i, end).match(/page:\s*(\d+)/);
        if (m) page = parseInt(m[1], 10);
        i = end + 3;
        continue;
      }
    }
    if (md[i] === '$' && md[i - 1] !== '\\') {
      const display = md[i + 1] === '$';
      const delim = display ? '$$' : '$';
      const start = i + delim.length;
      const end = md.indexOf(delim, start);
      if (end === -1) { i++; continue; }
      out.push({ page, mode: display ? 'display' : 'inline', latex: md.slice(start, end).trim() });
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
          message: String(msg).split('\n')[0], latex_head: f.latex.slice(0, 90) });
      }
      return 'warn';       // 不升级为 error,保持默认渲染行为
    };
    try {
      katex.renderToString(f.latex, { throwOnError: true, strict, displayMode: f.mode === 'display' });
    } catch (e) {
      errors.push({
        index, page: f.page, mode: f.mode,
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
