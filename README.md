# scholar-md

> 面向科研文章、专利等技术文档的个性化 PDF → Markdown 转换工具。

通用开源工具(Marker、docling 等)效果不错但太泛用;`scholar-md` 针对学术与技术
文档按**文档类型分管线**做专门优化,输出结构化、便于纳入笔记系统或喂给 LLM 阅读的
Markdown。

## 管线

| 管线 | 适用 | 方法 |
|------|------|------|
| `patents` | 美国授权专利(born-digital,双栏 + 中央行号) | 确定性几何解析,零 ML、零幻觉、纯 CPU |
| `general` | born-digital 通用文档(网页导出的指南/说明页等) | Marker:文本层取字 + ML 版面识别,带图,Tier0 自检 |

**选型**:有文本层的专利走 `patents`;其他 born-digital 文档走 `general`;扫描版用
`general --force-ocr` 降级。

## 安装

Windows + Python 3.12 虚拟环境。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# patents 管线(轻量,仅 PyMuPDF)
pip install -r requirements.txt

# general 管线(Marker,含 torch;GPU 加速,首次运行自动下载模型约 2–4GB)
pip install --extra-index-url https://download.pytorch.org/whl/cu128 -r scripts/pipelines/general/requirements.txt
```

## 用法

### general — 通用文档,自适应一键

```powershell
# --src 接受 文件 或 目录、单个 或 多个;--out 省略则产物落到 PDF 同目录
.\.venv\Scripts\python.exe scripts\pipelines\general\batch.py --src "<PDF 或目录>"
```

产物:每份 `<name>.md` + `<name>.assets/`(图片,Typora/VS Code 双兼容)+ Tier0 自检报告。

### patents — 美国专利

```powershell
# 把专利 PDF 放进源目录(或用环境变量 SCHOLARMD_PATENTS_SRC 指向任意目录)
.\.venv\Scripts\python.exe scripts\pipelines\patents\batch_patents.py --list
.\.venv\Scripts\python.exe scripts\pipelines\patents\batch_patents.py
```

各管线详细说明见其目录内 README(`scripts/pipelines/<type>/README.md`)。

## 工具与数据分离

本仓库**只含工具**,不含任何受版权保护的内容。你的输入 PDF 与转换产物是私有数据,
自行存放(本地/网盘),不进版本控制。详见 [DISCLAIMER.md](DISCLAIMER.md)——本工具
仅供处理你合法持有的文档。

## 目录

```
scholar-md/
├── scripts/
│   └── pipelines/
│       ├── patents/     专利几何解析(自包含)
│       └── general/     通用 born-digital,Marker(自包含,含 requirements.txt)
├── requirements.txt     patents 依赖
├── LICENSE              MIT
└── DISCLAIMER.md        版权免责声明
```

## License

[MIT](LICENSE) © 2026 melianstarling-ops
