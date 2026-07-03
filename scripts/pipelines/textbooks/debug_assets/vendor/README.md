# vendored 前端库(离线自包含,运行期零外部请求)

| 文件 | 来源 | 用途 |
|---|---|---|
| `katex.mjs` | katex@0.16.11 dist | node headless 扫描器 `scan_katex_errors.mjs` 的 ESM import(自包含,不依赖 node_modules) |
| `katex.min.js` | katex@0.16.11 dist | 浏览器工具:公式渲染(UMD) |
| `katex.inline.css` | katex@0.16.11 `katex.min.css` 加工 | 浏览器工具:KaTeX 样式,20 个 woff2 字体已 base64 内联(丢弃 woff/ttf),358KB |
| `auto-render.min.js` | katex@0.16.11 dist/contrib | 浏览器工具:自动识别 `$…$`/`$$…$$` 并渲染 |
| `markdown-it.min.js` | markdown-it@14.1.0 dist | 浏览器工具:markdown 渲染(与 VS Code 预览同源) |

## 重新生成 katex.inline.css(升级 katex 时)

```bash
npm install katex@<ver>
python - <<'PY'
import re, base64, os
fonts='node_modules/katex/dist/fonts'
css=open('node_modules/katex/dist/katex.min.css',encoding='utf-8').read()
def repl(m):
    b64=base64.b64encode(open(os.path.join(fonts,m.group(1)),'rb').read()).decode()
    return 'src:url(data:font/woff2;base64,%s) format("woff2")'%b64
open('katex.inline.css','w',encoding='utf-8').write(
    re.subn(r'src:url\(fonts/([\w-]+\.woff2)\)[^;}]*', repl, css)[0])
PY
```
