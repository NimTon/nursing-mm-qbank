# nursing-mm-qbank

**护理类题库**工具链（**当前推荐能力**）：对 **已导出的结果 xlsx** 用文本模型批注 **「讲师提醒」**（要点/易错/辨析；**流式 CSV** 断点续跑、**另存新表**；命令行 `xlsx-lecture-tips` 或 `mm-qbank-gui` 选结果表）。VLM 转写、教材向修正、拆题等见下文主流程。

## 安装

```bash
pip install -U pip
pip install -e .
# 需 OpenAI 兼容网关；在 .env 中配置 VLM_* 与 LLM_*（见 .env.example）
```

## 配置

- 项目根目录 **`.env`**：网关与模型名——整页读图 **`VLM_BASE_URL` + `VLM_API_KEY` + `VLM_MODEL`**；拆题/修正/讲课 **`LLM_BASE_URL` + `LLM_API_KEY` + `LLM_MODEL`**（可与 VLM 不同服务）。见 `.env.example`。
- **`configs/default.yaml`**：预处理 `preprocess`、并发、温度、**`web_search`** 等；**模型名已统一放在 `.env`**，不再从 yaml 读取。

## 主流程

1. **`vlm-text`** — 多模态读图，输出每页 `.{page_id}.json`（`题号/类型/内容`）+ `.txt` + `pages.jsonl`  
2. **`vlm-refine`** — 按**题号**合并「问题」和「解析」，经文本 LLM 填写修正后字段，生成 **`refined_merged.xlsx` + `refined_merged.jsonl`**  
3.（可选）**`llm-compose`** — 读每页 `text_file` 的整段字，再拆为题目数组（JSONL 每行一页）

### 1. 整页转写（VLM）

```bash
mm-qbank vlm-text --in data/inbound/某目录
mm-qbank vlm-text --in data/inbound/某目录 -v
```

默认输出 **`data/out/vlm_text/pages/`**（`structured_file` 在 `pages.jsonl` 中）。

### 2. 合并 + 教材向修正 + 导出 xlsx

```bash
mm-qbank vlm-refine --manifest data/out/vlm_text/pages/pages.jsonl
# 可指定: --out-xlsx  --out-jsonl
```

### 3.（可选）拆题

```bash
mm-qbank llm-compose --manifest data/out/vlm_text/pages/pages.jsonl --out data/out/llm/pages_items.jsonl
```

## 开发

```bash
pip install -e ".[dev]"
python -m pytest test/ -q
```

## 图形界面

```bash
mm-qbank-gui
# 或: python -m mm_qbank.gui_app
```

选择**图片文件夹**或**多选图片文件**（多选会复制到临时目录，结束后删除），在窗口**「转写控制」**区域使用 **开始 / 暂停·继续 / 结束** 控制 VLM 整页转写（位于「输出位置」与「进度」之间）；**暂停**与**结束**在相邻两页之间才会生效，当前页正在与模型通信时需等当次完成。输出到 **`data/out/vlm_gui_<时间戳>/`**，窗口内为**日志**与**进度条**；**完成**或**已结束**后会自动打开输出文件夹，也可用「打开输出目录」再打开一次。

## 打包为 Windows 可执行文件（exe）

项目提供 PyInstaller 的整项目打包配置：

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

- 产物：
  - `dist/mm-qbank-gui/` — onedir，可直接拷贝整个文件夹运行
  - `dist/mm-qbank-gui-setup.exe` — **自解压 exe**（默认生成，需本机安装 [7-Zip](https://www.7-zip.org/)：`winget install 7zip.7zip`）；用户双击解压后运行 `mm-qbank-gui\mm-qbank-gui.exe`
- 运行时会从 **exe 同目录**读取 `configs/` 与 `.env`（如需改 key/模型，放一个 `.env` 在 exe 旁边即可）。

## 其他

- 可维护脚本 **`scripts/gen_test_page_images.py`**，生成用于联调的多题+解析风格测试图（输出到 `data/inbound/...`）。
