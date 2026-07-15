// LaTeX -> 规范化 MathML,供 Task 3b 的 latex_equiv 交叉验证使用。
// 用 vendored katex.mjs 把每条 latex 渲染成 MathML,再规范化到只保留结构:
//   - 删 <annotation>(原始 latex 注解,不参与结构比较)
//   - 剥掉所有属性(displaystyle/stretchy/maxsize 等只影响外观,不影响数学结构)
//   - 展开 <mstyle>(纯样式包裹,例如 \dfrac 相对 \frac 只多一层
//     <mstyle displaystyle="true"><mfrac>...) 与只包一个子节点的冗余 <mrow>
//   - 折叠空白
// MathML 保留顺序,规范化不做任何重排——a+b 与 b+a 规范化后仍不同。
//
// 用法: echo '["x^2","\\frac{1}{2}"]' | node latex_to_mathml.mjs
// stdin: JSON 字符串数组(latex 列表);stdout: JSON 字符串数组(规范化 MathML,同序同长)。
// 渲染异常 / 找不到 <math> 结构的条目输出空串(上游 KaTeX 门已挡掉不可渲染的
// latex,故两条都空串被判等价是安全的已知情况,不是本模块的职责)。
import katex from './vendor/katex.mjs';
import fs from 'node:fs';

const TRANSPARENT_TAGS = new Set(['mstyle']); // 纯样式包裹,无论子节点数一律展开

// ---- 极简 XML 解析(只服务于 KaTeX 产出的规范 MathML,不是通用 XML 解析器)----

function tokenize(xml) {
  const tokens = [];
  let i = 0;
  while (i < xml.length) {
    if (xml[i] === '<') {
      const end = xml.indexOf('>', i);
      if (end === -1) break; // 不应发生;防御性截断
      const raw = xml.slice(i, end + 1);
      if (raw.startsWith('</')) {
        tokens.push({ kind: 'close', name: raw.slice(2, -1).trim() });
      } else if (raw.endsWith('/>')) {
        tokens.push({ kind: 'self', name: raw.slice(1, -2).trim().split(/\s/)[0] });
      } else {
        tokens.push({ kind: 'open', name: raw.slice(1, -1).trim().split(/\s/)[0] });
      }
      i = end + 1;
    } else {
      const next = xml.indexOf('<', i);
      const stop = next === -1 ? xml.length : next;
      const text = xml.slice(i, stop);
      if (text.trim()) tokens.push({ kind: 'text', value: text });
      i = stop;
    }
  }
  return tokens;
}

function parse(xml) {
  const tokens = tokenize(xml);
  const root = { tag: null, children: [] };
  const stack = [root];
  for (const tok of tokens) {
    const top = stack[stack.length - 1];
    if (tok.kind === 'open') {
      const node = { tag: tok.name, children: [] };
      top.children.push(node);
      stack.push(node);
    } else if (tok.kind === 'self') {
      top.children.push({ tag: tok.name, children: [] });
    } else if (tok.kind === 'close') {
      // 容错:若闭合标签与栈顶不匹配(不应发生于 KaTeX 输出),就地忽略而不崩溃。
      if (stack.length > 1 && stack[stack.length - 1].tag === tok.name) {
        stack.pop();
      }
    } else if (tok.kind === 'text') {
      top.children.push({ text: tok.value.replace(/\s+/g, ' ').trim() });
    }
  }
  return root;
}

// 展开纯样式包裹(mstyle)与只有一个元素子节点的冗余 mrow;递归到叶子。
function simplify(node) {
  if (node.text !== undefined) return node.text ? [node] : [];

  const children = node.children.flatMap(simplify);
  const isRedundantMrow = node.tag === 'mrow' && children.length === 1
    && children[0].text === undefined;

  if (TRANSPARENT_TAGS.has(node.tag) || isRedundantMrow) {
    return children;
  }
  return [{ tag: node.tag, children }];
}

function serialize(node) {
  if (node.text !== undefined) return node.text;
  const inner = node.children.map(serialize).join('');
  return `<${node.tag}>${inner}</${node.tag}>`;
}

function normalizeMathml(html) {
  const m = html.match(/<math[\s\S]*<\/math>/);
  if (!m) return '';
  let xml = m[0];
  xml = xml.replace(/<annotation[^>]*>[\s\S]*?<\/annotation>/g, '');
  xml = xml.replace(/\s+\w+="[^"]*"/g, '');
  const root = { tag: null, children: parse(xml).children.flatMap(simplify) };
  return root.children.map(serialize).join('');
}

function renderOne(latex) {
  try {
    const html = katex.renderToString(latex, { output: 'mathml', throwOnError: false });
    return normalizeMathml(html);
  } catch {
    return '';
  }
}

function main() {
  const raw = fs.readFileSync(0, 'utf-8');
  let latexList;
  try {
    latexList = JSON.parse(raw);
  } catch {
    process.stderr.write('latex_to_mathml: stdin 不是合法 JSON\n');
    process.exit(2);
  }
  if (!Array.isArray(latexList)) {
    process.stderr.write('latex_to_mathml: stdin 须是 JSON 字符串数组\n');
    process.exit(2);
  }
  const out = latexList.map((l) => renderOne(String(l)));
  process.stdout.write(JSON.stringify(out));
}

main();
