# general 管线

> **通用兜底管线**:born-digital(自带文本层)PDF → Typora 兼容 Markdown。
> 适用于不属于专门领域(patents / standards / books …)的一般文档:网页导出的
> 设计指南、说明页、在线文档存档等。

## 定位

工作区按"最适合的转换方式"分管线。本管线的判定依据是 **PDF 是否 born-digital
(有文本层)**:

- **有文本层** → 走本管线。Marker 从文本层取字(零 OCR 误差)+ ML 版面识别
  (标题/列表/图片/表格/锚点)。**不分块**(区别于 `books` 的大型扫描教材流水线)。
- 扫描版(无文本层) → 应走 `books` 管线或本管线 `--force-ocr` 降级。

## 一键用法

```bash
# 源与产物同目录(最常见):丢 PDF 进 References,一键转换
python batch.py --src "D:/.../References"

# 源/产物分离;只跑前 N 份调试;扫描版降级
python batch.py --src "<src>" --out "<out>" --limit 1
python batch.py --src "<src>" --force-ocr
```

流程:`扫 *.pdf → ① Marker 转换 → ② Typora 重排 → ③ Tier0 自检 + 报告`

## 产物结构(Typora / VS Code 双兼容)

```
References/
├── <name>.md                    md 在根
├── <name>.assets/               图片单独文件夹(Typora 默认约定)
│   ├── _page_*.jpeg
│   └── <name>_meta.json         Marker 转换元数据
└── _selfcheck/                  Tier0 报告(<时间戳>_tier0.md)
```

md 内图片引用形如 `![](<name>.assets/_page_0_Picture_6.jpeg)`,路径空格 URL 编码为
`%20`,两端渲染器均可解析。

## 质检两层(复用 patents 管线理念)

- **Tier0**(`selfcheck.py`,零成本、确定性,**默认跑**):断图链、图引用/实体
  一致性、空文件、`picture omitted` / base64 残留、乱码。存在 error 即退出码 1。
- **Tier1**(可选,LLM 语义审查):抽查转换保真度、标题层级合理性。尚未实装;
  规划移植 `patents/ai_review.py` 架构,替换为 docs 检查规则。

## 脚本

| 文件 | 职责 | 可独立运行 |
|------|------|:--:|
| `batch.py` | 一键入口:转换 → 重排 → 自检 | ✓ |
| `convert.py` | Marker 封装(born-digital, 固定高质量参数) | ✓ |
| `typora_layout.py` | Marker 输出 → Typora 结构重排 | ✓ |
| `selfcheck.py` | Tier0 形态自检 + 报告 | ✓ |

## 依赖

复用工作区 `.venv`,核心为 `marker-pdf==1.10.2` + `torch==2.11.0+cu128`
(GPU 加速;无 GPU 自动回退 CPU)。完整锁定见 `requirements.txt`。

安装(torch 走 cu128 专用 index):

```bash
pip install --extra-index-url https://download.pytorch.org/whl/cu128 -r requirements.txt
```

## 已知限制

- 极少数 PDF 内嵌的 CJK 兼容字形(如"以中**⽂**查看"的"⽂")会原样保留,占比极低。
- 表格质量依赖 Marker 的表格识别;复杂表格建议人工抽检。
- Tier0 只验"形态完整",不评判语义保真度(那是 Tier1 的职责)。
