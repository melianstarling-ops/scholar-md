# textbooks 公式视觉修复多后端封装 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `vision_repair.py` 的单 claude 后端改成 `claude|agy|codex|kimi` 四选一可切换,`run_vision_repair` 核心编排逻辑(打包/并发/单图回炉/人工确认门)完全不动。

**Architecture:** 每个后端各自一对 `call_<backend>_vision(crop_path, ...) -> {latex,confidence}` / `call_<backend>_vision_batch(entries, ...) -> {key: {latex,confidence}}`,签名与既有 `call_claude_vision`/`call_claude_vision_batch` 完全一致;`resolve_backend(name)` 按名字取这对函数,`run_vision_repair` 把它们当依赖注入的默认值用(不改变函数已经支持 `batch_fn`/`vision_fn` 覆盖的既有能力)。claude/agy/kimi 走同一套"prompt 里写绝对路径,让模型自己用内置 Read/ReadMediaFile 工具读图"手法(仅 argv 拼法不同:claude prompt 走 stdin,agy/kimi prompt 走 argv);codex 走完全不同的原生 `-i/--image` 附图,prompt 不提路径,批量靠图片附加顺序位置对应。

**Tech Stack:** Python 3(`.venv-textbooks`)、`subprocess`、`pytest`、`monkeypatch`。

## Global Constraints

- 只改 `scripts/pipelines/textbooks/vision_repair.py` 与 `scripts/pipelines/textbooks/tests/test_vision_repair.py`;不碰 `convert.py`/`debug_repair.py`/`corrections.py`(那是另一个模块,交接文档 §3.2,本计划不含)。
- 人工确认门红线不变:`_correction_record` 产出的 `status` 永远是 `"pending"`。
- **别给 agy 加 `--dangerously-skip-permissions`**(读操作默认放行,加了反被 Claude Code auto mode 安全门拦;见 `reference_multi-backend-clis` memory)。
- Windows npm `.cmd` shim 绕行铁律(同 claude 现有 `_resolve_claude_bin` 的踩坑):**禁止**用 `cmd /c` 硬调 `.cmd` 传 prompt——prompt 里含 LaTeX 反斜杠/花括号,`cmd /c` 二次解析会坏;优先找 `node_modules` 下真实入口用 `node <entry>` 直呼,只有找不到入口才退回 `cmd /c`(现有 `_resolve_claude_bin` 已有此兜底,照抄)。
- 测试基线 260 passed 不能破坏:`.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/`。
- 新增/修改的 subprocess 调用一律 `capture_output=True, text=True, encoding="utf-8", errors="replace"`(对齐现有 claude 调用)。

---

## 背景:四后端真实调用方式(本计划编写前已逐一实测确认,不是推测)

| 后端 | 二进制解析 | prompt 传法 | 图片传法 | 输出形态 |
|---|---|---|---|---|
| claude(已有) | `node <npm shim目录>/node_modules/@anthropic-ai/claude-code/cli-wrapper.cjs` | **stdin** | prompt 里写绝对路径,模型用 Read 工具读 | `--output-format json` 信封,`result` 字段是内层 JSON 字符串 |
| agy | 真 `.exe`,`shutil.which("agy")` 直接命中,无需 node 绕行 | **argv**(`agy -p "<prompt>"`) | 同 claude,prompt 里写路径 | 无信封,原始 stdout 可能带前置推理文字,真答案在尾部 |
| kimi | `node <npm shim目录>/node_modules/@moonshot-ai/kimi-code/dist/main.mjs`(**不是** K11 记的 kimi-cli 1.43 独立 exe——那个走 K6 无工具 agent,读不了图) | **argv**(`kimi -p "<prompt>"`) | 同 claude,prompt 里写路径;**实测**(2026-07-05,p48 block6 裁图)kimi 内置 `ReadMediaFile` 工具能读图,转写结果与已知正确答案高度吻合 | 无信封,常见 "•" 前置推理项目符号,真答案在尾部 |
| codex | `node <npm shim目录>/node_modules/@openai/codex/bin/codex.js` | **argv**(`codex exec "<prompt>" -i <path> --sandbox read-only`) | **原生** `-i/--image <FILE>...`,不用讲路径 | **实测**(2026-07-05,同一张 p48 block6 裁图)转写基本正确(`\vec{d}_n` 应为 `\vec{a}_n`,个别符号误读,不是路径/调用层面问题);stdout 常混入 codex 自己读取的项目上下文文字,真答案在尾部,同样需要尾部 JSON 提取 |

---

## Task 1: 通用 npm-shim 绕行解析器 + 三个新后端的二进制解析函数

**Files:**
- Modify: `scripts/pipelines/textbooks/vision_repair.py`(`_resolve_claude_bin` 上方新增 `_resolve_bin`,`_resolve_claude_bin` 改为委托它;新增 `_resolve_agy_bin`/`_resolve_kimi_bin`/`_resolve_codex_bin`)
- Test: `scripts/pipelines/textbooks/tests/test_vision_repair.py`

**Interfaces:**
- Produces: `_resolve_bin(binname: str, entry_parts: tuple[str, ...] | None = None) -> list[str]`、`_resolve_agy_bin() -> list[str]`、`_resolve_kimi_bin() -> list[str]`、`_resolve_codex_bin() -> list[str]`(供 Task 3/4/5 消费)。

- [ ] **Step 1: 写新解析函数的失败测试**

在 `test_vision_repair.py` 现有三个 `test_resolve_claude_bin_*` 测试后追加:

```python
def test_resolve_agy_bin_direct_exe(tmp_path, monkeypatch):
    exe = tmp_path / "agy.exe"
    exe.write_text("", encoding="utf-8")

    def fake_which(name):
        return str(exe) if name == "agy" else None

    monkeypatch.setattr(vision_repair.shutil, "which", fake_which)
    assert vision_repair._resolve_agy_bin() == [str(exe)]


def test_resolve_kimi_bin_prefers_node_entry_when_present(tmp_path, monkeypatch):
    node_modules = tmp_path / "node_modules" / "@moonshot-ai" / "kimi-code" / "dist"
    node_modules.mkdir(parents=True)
    entry = node_modules / "main.mjs"
    entry.write_text("", encoding="utf-8")
    shim = tmp_path / "kimi.cmd"
    shim.write_text("", encoding="utf-8")

    def fake_which(name):
        return str(shim) if "kimi" in name else "C:/node/node.exe"

    monkeypatch.setattr(vision_repair.shutil, "which", fake_which)
    argv = vision_repair._resolve_kimi_bin()
    assert argv == ["C:/node/node.exe", str(entry)]


def test_resolve_codex_bin_prefers_node_entry_when_present(tmp_path, monkeypatch):
    node_modules = tmp_path / "node_modules" / "@openai" / "codex" / "bin"
    node_modules.mkdir(parents=True)
    entry = node_modules / "codex.js"
    entry.write_text("", encoding="utf-8")
    shim = tmp_path / "codex.cmd"
    shim.write_text("", encoding="utf-8")

    def fake_which(name):
        return str(shim) if "codex" in name else "C:/node/node.exe"

    monkeypatch.setattr(vision_repair.shutil, "which", fake_which)
    argv = vision_repair._resolve_codex_bin()
    assert argv == ["C:/node/node.exe", str(entry)]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_vision_repair.py -k "resolve_agy_bin or resolve_kimi_bin or resolve_codex_bin" -v`
Expected: FAIL,`AttributeError: module ... has no attribute '_resolve_agy_bin'`(等三个)。

- [ ] **Step 3: 实现 `_resolve_bin` + 三个新解析函数,`_resolve_claude_bin` 改为委托**

在 `vision_repair.py` 中,把现有 `_resolve_claude_bin`(第 134-152 行)整体替换为:

```python
def _resolve_bin(binname: str, entry_parts: tuple[str, ...] | None = None) -> list[str]:
    """解析可被 subprocess 直接调用的 <binname> 前缀,绕开 Windows npm `.cmd` shim。

    Windows 上 npm 全局 bin 是 `.cmd` shim,Python subprocess(CreateProcess)不认;
    优先找 `node_modules` 下的真实入口用 `node <entry>` 直呼——`cmd /c` 会让 prompt
    里的反斜杠/花括号(LaTeX)被二次解析坏,只有找不到入口才退回 `cmd /c`(同
    Project_MRI_Safety `kb_core.resolve_backend_argv` 的踩坑与解法,K7)。
    """
    shim = shutil.which(binname) or shutil.which(binname + ".cmd")
    node = shutil.which("node")
    if entry_parts and shim and node:
        entry = Path(shim).parent.joinpath(*entry_parts)
        if entry.exists():
            return [node, str(entry)]
    if shim and os.name == "nt":
        if shim.lower().endswith(".exe"):
            return [shim]
        return ["cmd", "/c", shim]
    return [shim or binname]


def _resolve_claude_bin() -> list[str]:
    return _resolve_bin(
        "claude", ("node_modules", "@anthropic-ai", "claude-code", "cli-wrapper.cjs"))


def _resolve_agy_bin() -> list[str]:
    return _resolve_bin("agy")   # 真 .exe,PATH 直挂,无需 node 绕行


def _resolve_kimi_bin() -> list[str]:
    return _resolve_bin(
        "kimi", ("node_modules", "@moonshot-ai", "kimi-code", "dist", "main.mjs"))


def _resolve_codex_bin() -> list[str]:
    return _resolve_bin(
        "codex", ("node_modules", "@openai", "codex", "bin", "codex.js"))
```

- [ ] **Step 4: 跑全部测试确认通过(含既有 3 个 `_resolve_claude_bin` 回归测试)**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_vision_repair.py -k "resolve" -v`
Expected: 6 个测试全部 PASS(既有 3 个 claude 的 + 新增 3 个)。

- [ ] **Step 5: Commit**

```bash
git add scripts/pipelines/textbooks/vision_repair.py scripts/pipelines/textbooks/tests/test_vision_repair.py
git commit -m "refactor(textbooks): 抽出通用 npm-shim 绕行解析器,新增 agy/kimi/codex 二进制解析"
```

---

## Task 2: 尾部 JSON 对象提取(agy/kimi/codex 无信封输出用)

**Files:**
- Modify: `scripts/pipelines/textbooks/vision_repair.py`(`_extract_json_array` 函数后追加)
- Test: `scripts/pipelines/textbooks/tests/test_vision_repair.py`

**Interfaces:**
- Produces: `_extract_json_object(text: str) -> dict`(供 Task 3/4/5 的单图调用消费)。

- [ ] **Step 1: 写失败测试**

追加到 `test_vision_repair.py`:

```python
def test_extract_json_object_parses_clean_object():
    obj = vision_repair._extract_json_object('{"latex": "x", "confidence": "high"}')
    assert obj == {"latex": "x", "confidence": "high"}


def test_extract_json_object_strips_markdown_fence():
    text = "```json\n" + json.dumps({"latex": "x"}) + "\n```"
    assert vision_repair._extract_json_object(text) == {"latex": "x"}


def test_extract_json_object_finds_object_amid_leading_reasoning_noise():
    text = ("• The user wants me to read the image and transcribe it.\n"
            "Thinking about the formula structure {not real json}...\n"
            + json.dumps({"latex": "x", "confidence": "high"}))
    assert vision_repair._extract_json_object(text) == {"latex": "x", "confidence": "high"}


def test_extract_json_object_raises_on_no_object():
    with pytest.raises(ValueError):
        vision_repair._extract_json_object("no object here")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_vision_repair.py -k "extract_json_object" -v`
Expected: FAIL,`AttributeError: ... has no attribute '_extract_json_object'`。

- [ ] **Step 3: 实现**

在 `_extract_json_array` 函数(第 52-73 行)后追加:

```python
def _extract_json_object(text: str) -> dict:
    """从模型输出里抓最后一个配平的 JSON 对象(同 `_extract_json_array` 手法,反着找
    `{...}`):agy/kimi/codex 无 `--output-format json` 信封,原始 stdout 常见前置推理
    文字(kimi 甚至带 "•" 项目符号),真答案在末尾。"""
    s = _strip_fence(text)
    end = s.rfind("}")
    while end != -1:
        depth = 0
        for i in range(end, -1, -1):
            if s[i] == "}":
                depth += 1
            elif s[i] == "{":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(s[i:end + 1])
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        pass
                    break
        end = s.rfind("}", 0, end)
    raise ValueError("未找到可解析的 JSON 对象")
```

注意:`test_extract_json_object_finds_object_amid_leading_reasoning_noise` 里刻意混入一个 `{not real json}`(前面的假花括号),验证"从尾部往前找、`json.loads` 校验失败就继续退到更靠前的 `}`"这条已有于 `_extract_json_array` 的健壮性在对象版本上同样成立。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_vision_repair.py -k "extract_json_object" -v`
Expected: 4 个测试 PASS。

- [ ] **Step 5: Commit**

```bash
git add scripts/pipelines/textbooks/vision_repair.py scripts/pipelines/textbooks/tests/test_vision_repair.py
git commit -m "feat(textbooks): 加尾部 JSON 对象提取,供无信封后端(agy/kimi/codex)复用"
```

---

## Task 3: agy 后端

**Files:**
- Modify: `scripts/pipelines/textbooks/vision_repair.py`
- Test: `scripts/pipelines/textbooks/tests/test_vision_repair.py`

**Interfaces:**
- Consumes: `_resolve_agy_bin()`(Task 1)、`_extract_json_object()`(Task 2)、`build_vision_prompt()`/`build_batch_vision_prompt()`(既有)。
- Produces: `call_agy_vision(crop_path, timeout=120, agy_argv=None) -> {"latex","confidence"}`、`call_agy_vision_batch(entries, timeout=300, agy_argv=None) -> {key: {"latex","confidence"}}`。

- [ ] **Step 1: 写失败测试**

```python
def test_call_agy_vision_invokes_subprocess_with_prompt_in_argv(monkeypatch):
    captured = {}

    class FakeResult:
        stdout = json.dumps({"latex": "x", "confidence": "high"})

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return FakeResult()

    monkeypatch.setattr(vision_repair.subprocess, "run", fake_run)
    out = vision_repair.call_agy_vision("C:/crops/eq_1.58.png", agy_argv=["agy"])

    assert captured["argv"][0] == "agy"
    assert "-p" in captured["argv"]
    prompt = captured["argv"][captured["argv"].index("-p") + 1]
    assert "C:/crops/eq_1.58.png" in prompt
    assert "--dangerously-skip-permissions" not in captured["argv"]
    assert "input" not in captured["kwargs"]          # prompt 走 argv,不走 stdin
    assert out == {"latex": "x", "confidence": "high"}


def test_call_agy_vision_batch_invokes_subprocess_once_for_all_entries(monkeypatch):
    captured = {}

    class FakeResult:
        stdout = json.dumps([
            {"key": "49_3", "latex": "x3", "confidence": "high"},
            {"key": "49_6", "latex": "x6", "confidence": "high"},
        ])

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return FakeResult()

    monkeypatch.setattr(vision_repair.subprocess, "run", fake_run)
    entries = [{"key": "49_3", "crop_path": "C:/a/eq3.png"},
               {"key": "49_6", "crop_path": "C:/a/eq6.png"}]
    out = vision_repair.call_agy_vision_batch(entries, agy_argv=["agy"])

    prompt = captured["argv"][captured["argv"].index("-p") + 1]
    assert "C:/a/eq3.png" in prompt and "C:/a/eq6.png" in prompt
    assert out["49_3"]["latex"] == "x3"
    assert out["49_6"]["latex"] == "x6"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_vision_repair.py -k "call_agy_vision" -v`
Expected: FAIL,`AttributeError`。

- [ ] **Step 3: 实现**

在 `call_claude_vision_batch` 函数(既有,约第 170-178 行)后追加:

```python
def call_agy_vision(crop_path: str, timeout: int = 120,
                    agy_argv: list[str] | None = None) -> dict:
    """无头调 `agy -p` 读一张裁图。agy 语法是 prompt 走 argv(不同于 claude 的
    stdin),输出无信封,直接从 stdout 尾部抓 JSON 对象。**别加**
    `--dangerously-skip-permissions`——读操作本就默认放行,加了反被 Claude Code
    auto mode 安全门拦(见 reference_multi-backend-clis memory)。"""
    prompt = build_vision_prompt(crop_path)
    argv = (agy_argv or _resolve_agy_bin()) + ["-p", prompt]
    proc = subprocess.run(argv, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)
    obj = _extract_json_object(proc.stdout or "")
    return {"latex": obj.get("latex", ""), "confidence": obj.get("confidence", "")}


def call_agy_vision_batch(entries: list[dict], timeout: int = 300,
                          agy_argv: list[str] | None = None) -> dict:
    """一次调用读多张裁图,复用 claude 同款批量 prompt(纯文本,后端无关)。"""
    prompt = build_batch_vision_prompt(entries)
    argv = (agy_argv or _resolve_agy_bin()) + ["-p", prompt]
    proc = subprocess.run(argv, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)
    arr = _extract_json_array(proc.stdout or "")
    return {str(item.get("key")): {"latex": item.get("latex", ""),
                                   "confidence": item.get("confidence", "")}
            for item in arr if isinstance(item, dict) and item.get("key") is not None}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_vision_repair.py -k "call_agy_vision" -v`
Expected: 2 个测试 PASS。

- [ ] **Step 5: Commit**

```bash
git add scripts/pipelines/textbooks/vision_repair.py scripts/pipelines/textbooks/tests/test_vision_repair.py
git commit -m "feat(textbooks): 接入 agy 视觉后端"
```

---

## Task 4: kimi 后端

**Files:**
- Modify: `scripts/pipelines/textbooks/vision_repair.py`
- Test: `scripts/pipelines/textbooks/tests/test_vision_repair.py`

**Interfaces:**
- Consumes: `_resolve_kimi_bin()`(Task 1)、`_extract_json_object()`(Task 2)、`build_vision_prompt()`/`build_batch_vision_prompt()`(既有)。
- Produces: `call_kimi_vision(crop_path, timeout=120, kimi_argv=None) -> {"latex","confidence"}`、`call_kimi_vision_batch(entries, timeout=300, kimi_argv=None) -> {key: {"latex","confidence"}}`。

- [ ] **Step 1: 写失败测试**(与 Task 3 agy 版结构相同,替换函数名/后端标识)

```python
def test_call_kimi_vision_invokes_subprocess_with_prompt_in_argv(monkeypatch):
    captured = {}

    class FakeResult:
        stdout = "• thinking about the image...\n" + json.dumps(
            {"latex": "x", "confidence": "high"})

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return FakeResult()

    monkeypatch.setattr(vision_repair.subprocess, "run", fake_run)
    out = vision_repair.call_kimi_vision("C:/crops/eq_1.58.png", kimi_argv=["kimi"])

    assert captured["argv"][0] == "kimi"
    assert "-p" in captured["argv"]
    prompt = captured["argv"][captured["argv"].index("-p") + 1]
    assert "C:/crops/eq_1.58.png" in prompt
    assert "input" not in captured["kwargs"]
    assert out == {"latex": "x", "confidence": "high"}


def test_call_kimi_vision_batch_invokes_subprocess_once_for_all_entries(monkeypatch):
    captured = {}

    class FakeResult:
        stdout = json.dumps([
            {"key": "49_3", "latex": "x3", "confidence": "high"},
            {"key": "49_6", "latex": "x6", "confidence": "high"},
        ])

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return FakeResult()

    monkeypatch.setattr(vision_repair.subprocess, "run", fake_run)
    entries = [{"key": "49_3", "crop_path": "C:/a/eq3.png"},
               {"key": "49_6", "crop_path": "C:/a/eq6.png"}]
    out = vision_repair.call_kimi_vision_batch(entries, kimi_argv=["kimi"])

    prompt = captured["argv"][captured["argv"].index("-p") + 1]
    assert "C:/a/eq3.png" in prompt and "C:/a/eq6.png" in prompt
    assert out["49_3"]["latex"] == "x3"
    assert out["49_6"]["latex"] == "x6"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_vision_repair.py -k "call_kimi_vision" -v`
Expected: FAIL,`AttributeError`。

- [ ] **Step 3: 实现**(结构与 agy 完全一致,只换二进制解析函数)

```python
def call_kimi_vision(crop_path: str, timeout: int = 120,
                     kimi_argv: list[str] | None = None) -> dict:
    """无头调 `kimi -p` 读一张裁图。kimi 内置 ReadMediaFile 工具能读本地图片
    (实测 2026-07-05,p48 block6 裁图转写结果与已知正确答案高度吻合);走
    node_modules/main.mjs 的默认 agentic 模式,**不是** K11 记的 kimi-cli 1.43
    独立 exe(那条走 K6 无工具 chat agent,读不了图)。prompt 走 argv,输出
    无信封,常见 "•" 前置推理项目符号,从尾部抓 JSON 对象。"""
    prompt = build_vision_prompt(crop_path)
    argv = (kimi_argv or _resolve_kimi_bin()) + ["-p", prompt]
    proc = subprocess.run(argv, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)
    obj = _extract_json_object(proc.stdout or "")
    return {"latex": obj.get("latex", ""), "confidence": obj.get("confidence", "")}


def call_kimi_vision_batch(entries: list[dict], timeout: int = 300,
                           kimi_argv: list[str] | None = None) -> dict:
    prompt = build_batch_vision_prompt(entries)
    argv = (kimi_argv or _resolve_kimi_bin()) + ["-p", prompt]
    proc = subprocess.run(argv, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)
    arr = _extract_json_array(proc.stdout or "")
    return {str(item.get("key")): {"latex": item.get("latex", ""),
                                   "confidence": item.get("confidence", "")}
            for item in arr if isinstance(item, dict) and item.get("key") is not None}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_vision_repair.py -k "call_kimi_vision" -v`
Expected: 2 个测试 PASS。

- [ ] **Step 5: Commit**

```bash
git add scripts/pipelines/textbooks/vision_repair.py scripts/pipelines/textbooks/tests/test_vision_repair.py
git commit -m "feat(textbooks): 接入 kimi 视觉后端(纠正'Kimi 读不了图'的旧结论)"
```

---

## Task 5: codex 后端(原生 `-i` 附图,批量靠顺序对应)

**Files:**
- Modify: `scripts/pipelines/textbooks/vision_repair.py`
- Test: `scripts/pipelines/textbooks/tests/test_vision_repair.py`

**Interfaces:**
- Consumes: `_resolve_codex_bin()`(Task 1)、`_extract_json_object()`/`_extract_json_array()`(既有+Task 2)。
- Produces: `build_codex_vision_prompt() -> str`、`build_codex_batch_vision_prompt(count: int) -> str`、`call_codex_vision(crop_path, timeout=120, codex_argv=None) -> {"latex","confidence"}`、`call_codex_vision_batch(entries, timeout=300, codex_argv=None) -> {key: {"latex","confidence"}}`。

- [ ] **Step 1: 写失败测试**

```python
def test_build_codex_vision_prompt_has_no_path_reading_instruction():
    prompt = vision_repair.build_codex_vision_prompt()
    assert "Read the image file" not in prompt   # 图片走 -i 原生附加,不用讲路径
    assert r"\mathcal" in prompt


def test_build_codex_batch_vision_prompt_mentions_order_not_keys():
    prompt = vision_repair.build_codex_batch_vision_prompt(3)
    assert "3" in prompt
    assert "order" in prompt.lower()


def test_call_codex_vision_attaches_image_via_dash_i_flag(monkeypatch):
    captured = {}

    class FakeResult:
        stdout = json.dumps({"latex": "x", "confidence": "high"})

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return FakeResult()

    monkeypatch.setattr(vision_repair.subprocess, "run", fake_run)
    out = vision_repair.call_codex_vision("C:/crops/eq_1.58.png", codex_argv=["codex"])

    assert captured["argv"][0] == "codex"
    assert "exec" in captured["argv"]
    i = captured["argv"].index("-i")
    assert captured["argv"][i + 1] == "C:/crops/eq_1.58.png"
    assert "--sandbox" in captured["argv"] and "read-only" in captured["argv"]
    assert out == {"latex": "x", "confidence": "high"}


def test_call_codex_vision_batch_attaches_all_images_in_order_maps_back_by_position(monkeypatch):
    captured = {}

    class FakeResult:
        stdout = json.dumps([                     # 无 key,靠顺序对应
            {"latex": "x3", "confidence": "high"},
            {"latex": "x6", "confidence": "medium"},
        ])

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return FakeResult()

    monkeypatch.setattr(vision_repair.subprocess, "run", fake_run)
    entries = [{"key": "49_3", "crop_path": "C:/a/eq3.png"},
               {"key": "49_6", "crop_path": "C:/a/eq6.png"}]
    out = vision_repair.call_codex_vision_batch(entries, codex_argv=["codex"])

    i_indices = [idx for idx, tok in enumerate(captured["argv"]) if tok == "-i"]
    assert [captured["argv"][idx + 1] for idx in i_indices] == \
        ["C:/a/eq3.png", "C:/a/eq6.png"]
    assert out["49_3"]["latex"] == "x3"
    assert out["49_6"]["latex"] == "x6"
```

(响应数组短于 `entries` 时 `zip` 天然截断、缺的 key 由 `run_vision_repair` 既有的单图回炉机制兜底——这条行为已被 `test_run_vision_repair_falls_back_to_single_call_for_key_missing_from_batch` 覆盖过,不需要在 codex 这层重复写测试。)

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_vision_repair.py -k "codex" -v`
Expected: FAIL,`AttributeError`。

- [ ] **Step 3: 实现**

```python
def build_codex_vision_prompt() -> str:
    """codex exec 原生 `-i` 附图,prompt 不用像 claude/agy/kimi 那样讲"去读这个路径"。"""
    return (
        "The attached image shows a single mathematical formula, cropped from an OCR "
        "engine's output page. The engine may have mis-structured it (e.g. treating an "
        "integral contour/surface label like c' or s' as a fraction denominator and "
        "dropping the real denominator, or dropping sub/superscripts on a big operator "
        "like \\oint/\\int/\\sum/\\lim). Transcribe the formula exactly as shown in the "
        "image into correct LaTeX. For script-style capital letters (e.g. a calligraphic "
        "E for electric field), use \\mathcal{} — this book's convention — not "
        "\\mathscr{} or \\mathfrak{}.\n\n"
        "Respond with ONLY a single JSON object, no markdown fences, no explanation, "
        "in this exact shape:\n"
        '{"latex": "<LaTeX source, without $$ wrappers>", '
        '"confidence": "high|medium|low"}'
    )


def build_codex_batch_vision_prompt(count: int) -> str:
    """codex 一次可附多张图但不带 key 标签,靠"第几张附图"这个顺序位置对应
    (与 claude/agy/kimi 的 key 匹配不同,回应也不含 key)。"""
    return (
        f"You are given {count} cropped formula images, attached in order (the 1st "
        "attached image is item 1, the 2nd is item 2, and so on). For EACH one, "
        "transcribe the formula into correct LaTeX. The engine may have mis-structured "
        "it (e.g. treating an integral contour/surface label like c' or s' as a "
        "fraction denominator and dropping the real denominator, or dropping "
        "sub/superscripts on a big operator like \\oint/\\int/\\sum/\\lim). For "
        "script-style capital letters (e.g. a calligraphic E for electric field), use "
        "\\mathcal{} — this book's convention — not \\mathscr{} or \\mathfrak{}.\n\n"
        f"Respond with ONLY a single JSON array of exactly {count} objects, in the same "
        "order as the attached images, no markdown fences, no explanation, no \"key\" "
        "field needed, in this exact shape:\n"
        '[{"latex": "<LaTeX source, without $$ wrappers>", '
        '"confidence": "high|medium|low"}, ...]'
    )


def call_codex_vision(crop_path: str, timeout: int = 120,
                      codex_argv: list[str] | None = None) -> dict:
    """codex exec 原生支持 `-i/--image` 附图,图片走 argv 不走 prompt 文字。
    `--sandbox read-only` 防误写(纯读图任务不需要写权限)。"""
    prompt = build_codex_vision_prompt()
    argv = (codex_argv or _resolve_codex_bin()) + \
        ["exec", prompt, "-i", crop_path, "--sandbox", "read-only"]
    proc = subprocess.run(argv, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)
    obj = _extract_json_object(proc.stdout or "")
    return {"latex": obj.get("latex", ""), "confidence": obj.get("confidence", "")}


def call_codex_vision_batch(entries: list[dict], timeout: int = 300,
                            codex_argv: list[str] | None = None) -> dict:
    """多张图按 entries 顺序整批附给一次 codex exec 调用;响应数组不带 key,
    按位置 zip 回 entries[i]['key']。响应数组比 entries 短(模型漏答某张)时
    zip 天然截断,缺的 key 由 run_vision_repair 既有的单图回炉机制兜底,不
    在这层重复处理。"""
    prompt = build_codex_batch_vision_prompt(len(entries))
    image_args = []
    for e in entries:
        image_args += ["-i", e["crop_path"]]
    argv = (codex_argv or _resolve_codex_bin()) + \
        ["exec", prompt] + image_args + ["--sandbox", "read-only"]
    proc = subprocess.run(argv, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)
    arr = _extract_json_array(proc.stdout or "")
    out = {}
    for entry, item in zip(entries, arr):
        if isinstance(item, dict):
            out[entry["key"]] = {"latex": item.get("latex", ""),
                                 "confidence": item.get("confidence", "")}
    return out
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_vision_repair.py -k "codex" -v`
Expected: 4 个测试 PASS。

- [ ] **Step 5: Commit**

```bash
git add scripts/pipelines/textbooks/vision_repair.py scripts/pipelines/textbooks/tests/test_vision_repair.py
git commit -m "feat(textbooks): 接入 codex 视觉后端(原生 -i 附图,批量靠顺序对应)"
```

---

## Task 6: `resolve_backend` 分发层 + `run_vision_repair`/CLI 接入 `--backend`

**Files:**
- Modify: `scripts/pipelines/textbooks/vision_repair.py`(`_correction_record`、`run_vision_repair`、`main`)
- Test: `scripts/pipelines/textbooks/tests/test_vision_repair.py`

**Interfaces:**
- Consumes: Task 3/4/5 产出的 6 个 `call_<backend>_vision(_batch)` 函数。
- Produces: `resolve_backend(name: str) -> tuple[callable, callable]`;`run_vision_repair(doc_dir, backend="claude", batch_fn=None, vision_fn=None, ...)`(签名变化:`batch_fn`/`vision_fn` 默认值从"直接是 claude 函数"改成"`None` 时按 `backend` 解析");`_correction_record(item, result, today, backend="claude")`(`source` 字段从硬编码 `"claude-vision"` 改成 `f"{backend}-vision"`)。

- [ ] **Step 1: 写失败测试**

```python
def test_resolve_backend_returns_matching_vision_and_batch_fn():
    vision_fn, batch_fn = vision_repair.resolve_backend("agy")
    assert vision_fn is vision_repair.call_agy_vision
    assert batch_fn is vision_repair.call_agy_vision_batch


def test_resolve_backend_raises_on_unknown_name():
    with pytest.raises(ValueError):
        vision_repair.resolve_backend("not-a-backend")


def test_correction_record_source_reflects_backend():
    item = {"page": 49, "block_id": 3, "kinds": ["bare_op"], "engine_latex": "$$ a $$"}
    rec = vision_repair._correction_record(
        item, {"latex": "x", "confidence": "high"}, "2026-07-05", backend="agy")
    assert rec["source"] == "agy-vision"


def test_run_vision_repair_defaults_to_backend_named_functions_when_no_fn_given(
        tmp_path, monkeypatch):
    doc_dir = str(tmp_path / "book")
    _write_worklist(doc_dir, "book", [_item(1, 1, "a.png")])
    calls = []

    def fake_agy_batch(entries, timeout=300):
        calls.append(entries)
        return {"1_1": {"latex": "x", "confidence": "high"}}

    monkeypatch.setattr(vision_repair, "call_agy_vision_batch", fake_agy_batch)
    result = vision_repair.run_vision_repair(doc_dir, backend="agy", batch_size=10)

    assert len(calls) == 1
    with open(result["corrections_path"], encoding="utf-8") as f:
        data = json.load(f)
    assert data["corrections"][0]["source"] == "agy-vision"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_vision_repair.py -k "resolve_backend or correction_record_source or defaults_to_backend" -v`
Expected: FAIL(`resolve_backend` 不存在;`_correction_record` 不接受 `backend` 关键字;`run_vision_repair` 不接受 `backend`)。

- [ ] **Step 3a: `_correction_record` 加 `backend` 参数**

把既有函数(约第 185-197 行)：

```python
def _correction_record(item: dict, result: dict, today: str) -> dict:
    return {
        "page": item["page"],
        "block_id": item["block_id"],
        "kind": "+".join(item.get("kinds", [])),
        "engine_latex": item["engine_latex"],
        "corrected_latex": f"$$ {result['latex']} $$",
        "source": "claude-vision",
        "confidence": result.get("confidence", ""),
        "content_fingerprint": content_fingerprint(item["engine_latex"]),
        "status": "pending",   # 人工确认门(红线):新产出的修正一律待审,不自动生效
        "ts": today,
    }
```

改为:

```python
def _correction_record(item: dict, result: dict, today: str, backend: str = "claude") -> dict:
    return {
        "page": item["page"],
        "block_id": item["block_id"],
        "kind": "+".join(item.get("kinds", [])),
        "engine_latex": item["engine_latex"],
        "corrected_latex": f"$$ {result['latex']} $$",
        "source": f"{backend}-vision",
        "confidence": result.get("confidence", ""),
        "content_fingerprint": content_fingerprint(item["engine_latex"]),
        "status": "pending",   # 人工确认门(红线):新产出的修正一律待审,不自动生效
        "ts": today,
    }
```

- [ ] **Step 3b: 加 `resolve_backend`,`run_vision_repair` 接入 `backend`**

在 `call_codex_vision_batch`(Task 5 产出)后追加分发表:

```python
_BACKENDS = {
    "claude": (call_claude_vision, call_claude_vision_batch),
    "agy": (call_agy_vision, call_agy_vision_batch),
    "kimi": (call_kimi_vision, call_kimi_vision_batch),
    "codex": (call_codex_vision, call_codex_vision_batch),
}


def resolve_backend(name: str) -> tuple:
    """按名字取 (vision_fn, batch_fn) 对;未知名字直接抛错,不做静默降级
    (调用方——CLI 或上游 convert.py --repair——自己决定兜底)。"""
    if name not in _BACKENDS:
        raise ValueError(f"未知视觉后端 {name!r},可选:{sorted(_BACKENDS)}")
    return _BACKENDS[name]
```

把 `run_vision_repair`(既有,约第 200-254 行)签名与函数体开头改为:

```python
def run_vision_repair(doc_dir: str, backend: str = "claude", batch_fn=None,
                      vision_fn=None, batch_size: int = 5,
                      parallel: int = 3, timeout: int = 300) -> dict:
    """...(docstring 原文不变,补一句:`batch_fn`/`vision_fn` 缺省时按 `backend`
    经 `resolve_backend` 取默认实现;显式传入仍可覆盖,供测试注入假后端。)"""
    default_vision_fn, default_batch_fn = resolve_backend(backend)
    batch_fn = batch_fn or default_batch_fn
    vision_fn = vision_fn or default_vision_fn
    stem = os.path.basename(os.path.normpath(doc_dir))
    ...(其余函数体不变,只把末尾 corrections.append(_correction_record(item, result, today))
        改成 corrections.append(_correction_record(item, result, today, backend)))
```

- [ ] **Step 3c: CLI `main()` 加 `--backend`**

把 `main()`(既有,约第 257-269 行)的 argparse 部分改为:

```python
def main() -> None:
    ap = argparse.ArgumentParser(description="疑似公式:无头 CLI 读裁图 → corrections.json")
    ap.add_argument("--doc", required=True, help="doc 目录(同 debug_repair --doc)")
    ap.add_argument("--backend", default="claude", choices=sorted(_BACKENDS),
                    help="视觉修复后端(默认 claude)")
    ap.add_argument("--batch-size", type=int, default=5, help="每次调用打包几张裁图(默认5)")
    ap.add_argument("--parallel", type=int, default=3, help="批间并发数(默认3,对齐 SOP)")
    ap.add_argument("--timeout", type=int, default=300, help="单批调用超时秒(默认300)")
    args = ap.parse_args()
    result = run_vision_repair(args.doc, backend=args.backend, batch_size=args.batch_size,
                               parallel=args.parallel, timeout=args.timeout)
    print(f"[vision_repair] {result['count']} 条修正 → {result['corrections_path']}")
    if result["failed"]:
        print(f"[vision_repair] {len(result['failed'])} 项失败:", result["failed"])
```

- [ ] **Step 4: 跑全部测试确认通过(全量回归,含既有 260 基线)**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/ -v`
Expected: 全部 PASS,数量为 260 + 本计划新增的测试数(Task1: +3,Task2: +4,Task3: +2,Task4: +2,Task5: +4,Task6: +4,共 +19 → 279)。

- [ ] **Step 5: Commit**

```bash
git add scripts/pipelines/textbooks/vision_repair.py scripts/pipelines/textbooks/tests/test_vision_repair.py
git commit -m "feat(textbooks): vision_repair 接入 --backend 分发(claude|agy|codex|kimi)"
```

---

## Task 7: 四后端真实小测(非自动化测试,人工验证环节)

**目的:** Task 1-6 全靠 monkeypatch 假 subprocess 验证了"argv 拼得对不对",没验证"真跑起来后端到底读不读得懂图"。这一步用同一张已知正确答案的裁图(p48 block6)分别真跑 claude(基线,已验证过)/agy(已验证过)/kimi/codex 四个后端各一次,人工核对,**不写入生产 `_corrections.json`**(避免真实文档状态被测试污染)。

**Files:** 无代码改动,只跑验证脚本(建议写到 scratchpad,不提交仓库)。

- [ ] **Step 1: 用真实裁图跑四个后端,打印结果对比**

```bash
.venv-textbooks/Scripts/python.exe -c "
import sys
sys.path.insert(0, '.')
from scripts.pipelines.textbooks import vision_repair as vr

crop = r'C:\Users\hulia\OneDrive\Project_scholar-md\03_Output\textbooks\_realrun_100page_test\_work_root\Paul_p1-100_scan\Paul_p1-100_scan_repair\crops\page_0048_block_6.png'
known_good = r'l=-\frac{\mu\int_c \vec{\mathcal{H}}_t\cdot\vec{a}_n dl}{I(z,t)}'
print('已知正确答案:', known_good)
for name, fn in [('agy', vr.call_agy_vision), ('kimi', vr.call_kimi_vision),
                 ('codex', vr.call_codex_vision)]:
    try:
        out = fn(crop, timeout=90)
        print(f'{name}: {out}')
    except Exception as e:
        print(f'{name}: ERROR {type(e).__name__}: {e}')
"
```

- [ ] **Step 2: 人工核对**

对每个后端输出的 `latex`,和 `known_good` 逐字对比(允许等价的空白差异,如 `\int_c` vs `\int_{c}`)。**若某后端离谱错误或直接报错**,不阻塞本计划已完成的代码(argv 拼装/解析层已用 Task 1-6 的单元测试验证过),只需在下面的交接文档里如实记录该后端当前视觉质量水平/暂不建议生产使用,留给使用者自己选后端时参考——不因质量问题回退代码。

- [ ] **Step 3: 把小测结果写进交接文档**

在 `docs/handoff/` 新开一份交接(或追加到当前这份的延续记录),记录四后端在同一裁图上的真实输出对比,供后续选默认后端/写 §3.2 `convert.py --repair` 时参考默认值。

---

## Self-Review 记录(写计划时已过一遍)

- **Spec 覆盖**:交接 §3.3"把 `vision_repair` 的单 claude 后端做成多后端可切换(`--backend claude|agy|codex|kimi`)"——Task 1-6 覆盖四后端 + CLI 开关;"agy 后端骨架已有实证调用方式,codex/kimi 接口需先各跑一次小测确认"——已在写计划前完成实测(见"背景"表格),Task 7 补真实调用的端到端小测。
- **占位扫描**:Task 5 草稿曾出现一条占位测试(`test_call_codex_vision_batch_drops_entries_beyond_short_response`),已在该 Task 内标注**不要保留**,Step 3 实现说明里用文字解释了那条行为(zip 截断 + 上游回炉兜底),不需要专门测试重复覆盖。
- **类型一致性**:所有 6 个 `call_<backend>_vision(_batch)` 函数签名与既有 `call_claude_vision(_batch)` 完全对齐(`vision_fn(crop_path, timeout=..., <name>_argv=None) -> {"latex","confidence"}`;`batch_fn(entries, timeout=..., <name>_argv=None) -> {key: {"latex","confidence"}}`),`run_vision_repair` 的 `batch_fn`/`vision_fn` 参数类型不变,新增的只是 `backend: str` 及其解析路径。

---

## 执行方式(执行时选,不影响本计划文档)

**Plan complete and saved to `docs/superpowers/plans/2026-07-05-textbooks-vision-repair-multibackend.md`. 两种执行方式:**

1. **Subagent-Driven(推荐)** —— 逐任务派独立 subagent,任务间人工/主 agent review,快速迭代。
2. **Inline Execution** —— 本会话内按 executing-plans 批量跑,检查点式复核。
