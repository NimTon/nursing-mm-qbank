"""
图形界面：教师讲解 / 题目修正 两分支（共用 scan → assemble 流程）。

运行：mm-qbank-gui  或  python -m mm_qbank.gui_app
"""

from __future__ import annotations

import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

if sys.platform == "win32":
    try:
        import ctypes

        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            try:
                import ctypes

                ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk
except ImportError as e:  # noqa: F841
    tk = None  # type: ignore[misc, assignment]
    filedialog = messagebox = scrolledtext = ttk = None  # type: ignore[misc, assignment]
    _tk_import_error = e
else:
    _tk_import_error = None

from dotenv import load_dotenv
from PIL import Image  # noqa: F401

try:
    import sv_ttk
except Exception:  # noqa: BLE001
    sv_ttk = None  # type: ignore[assignment]

from mm_qbank import __version__
from mm_qbank.config import load_config, llm_text_settings, project_root, vlm_settings
from mm_qbank.logging_utils import configure_logging
from mm_qbank.pipeline.lecture_scan_run import run_correction_scan_pipeline, run_lecture_scan_pipeline
from mm_qbank.pipeline.scan_pages import list_input_images

_LOG_QUEUE: "queue.Queue[str] | None" = None
_IMG_EXTS = frozenset({".png", ".jpg", ".jpeg", ".bmp", ".webp"})


class _TextQueueHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:  # noqa: N802
        if _LOG_QUEUE is not None:
            _LOG_QUEUE.put(self.format(record) + "\n")


def _env_startup_hint() -> str:
    vs = vlm_settings()
    ls = llm_text_settings()
    missing: list[str] = []
    if not vs.get("api_key"):
        missing.append("VLM_API_KEY")
    if not vs.get("base_url"):
        missing.append("VLM_BASE_URL")
    if not ls.get("api_key"):
        missing.append("LLM_API_KEY")
    if not ls.get("base_url"):
        missing.append("LLM_BASE_URL")
    if missing:
        return f"请在 exe 同目录 .env 中配置：{', '.join(missing)}。\n"
    vlm_m = vs.get("mm_model") or "?"
    llm_m = ls.get("text_model") or "?"
    hint = f"已加载 .env：VLM={vlm_m}，LLM={llm_m}。"
    base = f"{vs.get('base_url') or ''}{ls.get('base_url') or ''}".lower()
    if "dashscope" in base and (vlm_m.startswith("gpt-") or llm_m.startswith("gpt-")):
        hint += " 阿里云网关请在 .env 设置 VLM_MODEL=qwen-vl-max、LLM_MODEL=qwen-plus（勿用默认 gpt-4o）。"
    return hint + "\n"


def _reveal_dir(path: Path) -> None:
    p = path.resolve()
    if not p.is_dir():
        p = p.parent
    s = str(p)
    if sys.platform == "win32":
        os.startfile(s)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", s], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(["xdg-open", s], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _list_images(d: Path) -> list[Path]:
    if not d.is_dir():
        return []
    return list_input_images(d)


def _list_direct_images_in_dir(d: Path) -> list[Path]:
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() in _IMG_EXTS)


def _dirs_with_direct_images_dfs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    rt = root.resolve()
    out: list[Path] = []

    def walk(d: Path) -> None:
        if _list_direct_images_in_dir(d):
            out.append(d)
        for sub in sorted((p for p in d.iterdir() if p.is_dir()), key=lambda p: p.name.lower()):
            walk(sub)

    walk(rt)
    return out


def _safe_name(name: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|]+", "_", (name or "").strip())
    s = re.sub(r"\s+", "_", s)
    return s or "input"


def _batch_output_slug(batch_src: Path, root: Path) -> str:
    bs, rt = batch_src.resolve(), root.resolve()
    root_seg = _safe_name(rt.name) or "root"
    if bs == rt:
        return root_seg
    try:
        rel = bs.relative_to(rt)
    except ValueError:
        return f"{root_seg}-{_safe_name(bs.name) or 'batch'}"
    parts = [root_seg] + [_safe_name(p) or "dir" for p in rel.parts]
    return "-".join(parts) or "batch"


def _folder_batch_display_name(batch_src: Path, root: Path) -> str:
    return _batch_output_slug(batch_src, root)


def _install_gui_mascot_strip(parent: tk.Misc, font: tuple[Any, ...]) -> None:
    fr = ttk.Frame(parent)
    fr.pack(fill=tk.X, anchor=tk.E, pady=(2, 0))
    frames = (
        "      ∧∧\n"
        "     (·ω·)っ ❚ 敏敏加油 ❚\n"
        "     ( U U)  挥挥~",
        "      ∧∧\n"
        "    ⊂(·ω· ) ❚ 敏敏加油 ❚っ\n"
        "     ( U U)  挥挥~",
        "      ∧∧\n"
        "     (·ω·)⊃ ❚ 敏敏加油 ❚\n"
        "     ( U U)  加油!",
        "      ∧∧\n"
        "    ⊂(·ω· ) ❚ 敏敏加油 ❚っ\n"
        "     ( U U)  挥挥~",
    )
    lbl = tk.Label(fr, text=frames[0], font=font, justify=tk.LEFT, fg="#5a5a5a")
    try:
        bg = parent.tk.call("ttk::style", "lookup", "TFrame", "-background")
        if bg:
            lbl.configure(bg=bg)
    except Exception:  # noqa: BLE001
        pass
    lbl.pack(anchor=tk.E, padx=0, pady=0)
    idx = 0
    after_id: list[Any] = [None]

    def tick() -> None:
        nonlocal idx
        try:
            if not parent.winfo_exists():
                return
        except Exception:  # noqa: BLE001
            return
        idx = (idx + 1) % len(frames)
        lbl.config(text=frames[idx])
        after_id[0] = parent.winfo_toplevel().after(480, tick)

    def on_destroy(_: tk.Event[Any]) -> None:
        aid = after_id[0]
        if aid is not None:
            try:
                parent.winfo_toplevel().after_cancel(aid)
            except Exception:  # noqa: BLE001
                pass
            after_id[0] = None

    fr.bind("<Destroy>", on_destroy)
    tick()


def main() -> None:
    global _LOG_QUEUE  # noqa: PLW0603

    if tk is None:
        print("当前解释器未带 tkinter。", file=sys.stderr)  # noqa: T201
        raise SystemExit(1) from _tk_import_error

    load_dotenv(project_root() / ".env")
    _LOG_QUEUE = queue.Queue()
    configure_logging(verbose=False, quiet=False)
    th = _TextQueueHandler()
    th.setLevel(logging.INFO)
    th.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
    logging.getLogger().addHandler(th)
    for name in ("httpx", "httpcore", "openai", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)

    root = tk.Tk()
    root.title(f"nursing-mm-qbank v{__version__} · 教师讲解 · 题目修正")
    root.geometry("1200x780")
    root.minsize(720, 520)
    if sv_ttk is not None:
        try:
            sv_ttk.set_theme("light")
        except Exception:  # noqa: BLE001
            pass

    _font_main: tuple[Any, ...] | None = None
    _font_mono: tuple[Any, ...] | None = None
    try:
        import tkinter.font as tkfont

        base_family = "Segoe UI" if sys.platform == "win32" else "TkDefaultFont"
        main_size = 10
        mono_family = "Consolas" if os.name == "nt" else "monospace"

        tkfont.nametofont("TkDefaultFont").configure(family=base_family, size=main_size)
        tkfont.nametofont("TkTextFont").configure(family=base_family, size=main_size)
        tkfont.nametofont("TkHeadingFont").configure(family=base_family, size=main_size, weight="bold")
        tkfont.nametofont("TkMenuFont").configure(family=base_family, size=main_size)
        tkfont.nametofont("TkTooltipFont").configure(family=base_family, size=main_size)
        tkfont.nametofont("TkFixedFont").configure(family=mono_family, size=main_size)

        _font_main = (base_family, main_size)
        _font_mono = (mono_family, main_size)

        style = ttk.Style()
        style.configure(".", font=_font_main)
        style.configure("TLabelframe.Label", font=_font_main)
        for sty in (
            "TLabel",
            "TButton",
            "TCheckbutton",
            "TRadiobutton",
            "TEntry",
            "TCombobox",
            "TSpinbox",
            "TNotebook.Tab",
            "Treeview",
            "Treeview.Heading",
        ):
            style.configure(sty, font=_font_main)

        root.option_add("*TCombobox*Listbox.font", _font_main)
        root.option_add("*Listbox.font", _font_main)
    except Exception:  # noqa: BLE001
        style = ttk.Style()

    content = ttk.Frame(root)
    content.pack(fill=tk.BOTH, expand=True)

    work_dir: list[Path | None] = [None]
    last_out: list[Path | None] = [None]
    run_cancel = threading.Event()
    user_paused: list[bool] = [False]
    is_running: list[bool] = [False]
    gui_llm_persist_cb: list[Any] = []
    folder_batch_var = tk.BooleanVar(value=False)
    pipeline_mode_var = tk.StringVar(value="lecture")

    fr_top = ttk.LabelFrame(content, text="图片输入", padding=8)
    fr_top.pack(fill=tk.X, padx=8, pady=4)
    fr_top_bar = ttk.Frame(fr_top)
    fr_top_bar.pack(fill=tk.X)
    fr_top_right = ttk.Frame(fr_top_bar)
    fr_top_right.pack(side=tk.RIGHT, padx=(4, 0))
    fr_ver = ttk.Frame(fr_top_right)
    fr_ver.pack(anchor=tk.E)
    ttk.Label(
        fr_ver,
        text=f"v{__version__}",
        foreground="#666",
    ).pack(side=tk.LEFT, padx=(0, 6))
    ttk.Label(
        fr_ver,
        text="教师讲解 · 题目修正",
        foreground="#666",
    ).pack(side=tk.LEFT)
    if _font_mono is not None:
        _install_gui_mascot_strip(fr_top_right, _font_mono)
    btn_dir = ttk.Button(fr_top_bar, text="选择图片文件夹…")
    btn_dir.pack(side=tk.LEFT)
    lbl_in = ttk.Label(fr_top, text="未选择", foreground="#666")
    lbl_in.pack(anchor=tk.W, pady=(4, 0))
    lbl_thumb_note = ttk.Label(fr_top, text="选择含图文件夹后点击「开始」。", foreground="#666", wraplength=780)
    lbl_thumb_note.pack(anchor=tk.W, pady=(2, 0))

    chk_folder_batch = ttk.Checkbutton(
        fr_top,
        text="按文件夹分层分批（每个「当前层有直属图片」的文件夹单独一轮）",
        variable=folder_batch_var,
    )
    chk_folder_batch.pack(anchor=tk.W, pady=(4, 0))

    fr_mode = ttk.LabelFrame(fr_top, text="处理分支", padding=4)
    fr_mode.pack(fill=tk.X, pady=(6, 0))
    ttk.Radiobutton(
        fr_mode,
        text="教师讲解：逐图 VLM → 按页码拼接 → LLM 拆题 → 生成讲课 Word",
        variable=pipeline_mode_var,
        value="lecture",
    ).pack(anchor=tk.W)
    ttk.Radiobutton(
        fr_mode,
        text="题目修正：相同 scan+拆题流程 → 教材向修正（无卷面解析时 LLM 生成解析）→ 导出 xlsx",
        variable=pipeline_mode_var,
        value="correction",
    ).pack(anchor=tk.W, pady=(2, 0))

    def _refresh_input_label() -> None:
        wd = work_dir[0]
        if not wd or not wd.is_dir():
            return
        if folder_batch_var.get():
            batches = _dirs_with_direct_images_dfs(wd)
            nic = sum(len(_list_direct_images_in_dir(b)) for b in batches)
            lbl_in.config(text=f"文件夹: {wd}  （分批 {len(batches)} 层，{nic} 张直属图）", foreground="black")
        else:
            n = len(_list_images(wd))
            lbl_in.config(text=f"文件夹: {wd}  （共 {n} 张）", foreground="black")

    folder_batch_var.trace_add("write", lambda *_: _refresh_input_label())

    def on_pick_dir() -> None:
        p = filedialog.askdirectory(title="选择含图片的文件夹", mustexist=True)
        if not p:
            return
        work_dir[0] = Path(p)
        _refresh_input_label()
        if not folder_batch_var.get() and not _list_images(Path(p)):
            messagebox.showwarning("提示", "该目录下没有支持的图片。")

    btn_dir["command"] = on_pick_dir

    fr_out = ttk.LabelFrame(content, text="输出位置", padding=6)
    fr_out.pack(fill=tk.X, padx=8, pady=4)
    lbl_path = ttk.Label(
        fr_out,
        text="开始后在 data/out/ 下生成时间戳目录",
        wraplength=800,
    )
    lbl_path.pack(anchor=tk.W)

    fr_pb = ttk.LabelFrame(content, text="进度", padding=6)
    fr_pb.pack(fill=tk.X, padx=8, pady=2)
    pb = ttk.Progressbar(fr_pb, mode="determinate", maximum=1000, value=0)
    pb.pack(fill=tk.X, pady=(0, 4))
    st_lbl = ttk.Label(fr_pb, text="空闲")
    st_lbl.pack(anchor=tk.W)
    btn_ref = ttk.Button(fr_pb, text="打开输出目录", state=tk.DISABLED)
    btn_ref.pack(anchor=tk.W, pady=4)

    fr_ops = ttk.LabelFrame(content, text="转写控制", padding=6)
    fr_ops.pack(fill=tk.X, padx=8, pady=4)
    fr_ops_btns = ttk.Frame(fr_ops)
    fr_ops_btns.pack(fill=tk.X)
    btn_s = ttk.Button(fr_ops_btns, text="开始", width=10)
    btn_s.pack(side=tk.LEFT, padx=(0, 6))
    btn_pause = ttk.Button(fr_ops_btns, text="暂停", width=10, state=tk.DISABLED)
    btn_pause.pack(side=tk.LEFT, padx=6)
    btn_end = ttk.Button(fr_ops_btns, text="结束", width=10, state=tk.DISABLED)
    btn_end.pack(side=tk.LEFT, padx=6)

    fr_llm = ttk.Frame(fr_ops)
    fr_llm.pack(fill=tk.X, pady=(6, 0))
    ttk.Label(fr_llm, text="LLM 参数：").pack(side=tk.LEFT)
    try:
        _cfg0 = load_config(None)
    except Exception:
        _cfg0 = {}
    _rcfg0 = dict((_cfg0.get("refine") or {}) if isinstance(_cfg0, dict) else {})
    _lcc0 = dict((_cfg0.get("lecture_content") or {}) if isinstance(_cfg0, dict) else {})
    _web0 = bool(_rcfg0.get("web_search", False)) or bool(_lcc0.get("web_search", False))
    web_search_var = tk.BooleanVar(value=_web0)
    ttk.Checkbutton(fr_llm, text="联网检索", variable=web_search_var).pack(side=tk.LEFT, padx=(6, 0))
    _gui_yaml = (project_root() / "configs" / "default.yaml").resolve()

    def _persist_web_search() -> None:
        try:
            cfg = load_config(None)
        except Exception:
            cfg = deepcopy(_cfg0) if isinstance(_cfg0, dict) else {}
        if not isinstance(cfg, dict):
            cfg = {}
        ws = bool(web_search_var.get())
        rcfg = dict(cfg.get("refine") or {})
        lccfg = dict(cfg.get("lecture_content") or {})
        rcfg["web_search"] = ws
        lccfg["web_search"] = ws
        cfg["refine"] = rcfg
        cfg["lecture_content"] = lccfg
        text = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)
        tmp = _gui_yaml.with_suffix(_gui_yaml.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(_gui_yaml)

    web_search_var.trace_add("write", lambda *_: _persist_web_search())
    gui_llm_persist_cb.append(_persist_web_search)

    fr_log = ttk.LabelFrame(content, text="日志", padding=4)
    fr_log.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
    fr_log_bar = ttk.Frame(fr_log)
    fr_log_bar.pack(fill=tk.X, pady=(0, 4))
    ttk.Label(fr_log_bar, text="主题：").pack(side=tk.LEFT)
    builtin_themes = list(style.theme_names() or [])
    theme_var = tk.StringVar(value="sv-light" if sv_ttk is not None else (builtin_themes[0] if builtin_themes else ""))
    theme_values: list[str] = []
    if sv_ttk is not None:
        theme_values.extend(["sv-light", "sv-dark"])
    theme_values.extend(builtin_themes)
    cb_theme = ttk.Combobox(
        fr_log_bar,
        textvariable=theme_var,
        values=theme_values,
        width=14,
        state="readonly",
    )
    cb_theme.pack(side=tk.LEFT, padx=(0, 12))

    def _apply_theme(_: Any | None = None) -> None:
        v = (theme_var.get() or "").strip()
        if not v:
            return
        if v.startswith("sv-"):
            if sv_ttk is None:
                return
            try:
                sv_ttk.set_theme("dark" if v == "sv-dark" else "light")
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).warning("GUI theme 切换失败: %s", v, exc_info=True)
        else:
            try:
                style.theme_use(v)
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).warning("ttk theme 切换失败: %s", v, exc_info=True)

    cb_theme.bind("<<ComboboxSelected>>", _apply_theme)
    _apply_theme()

    ttk.Label(fr_log_bar, text="日志级别：").pack(side=tk.LEFT)
    log_level_var = tk.StringVar(value="info")
    cb_level = ttk.Combobox(
        fr_log_bar,
        textvariable=log_level_var,
        values=["info", "debug", "warning"],
        width=10,
        state="readonly",
    )
    cb_level.pack(side=tk.LEFT)

    def _apply_log_level(_: Any | None = None) -> None:
        v = (log_level_var.get() or "info").strip().lower()
        level = logging.INFO
        if v == "debug":
            level = logging.DEBUG
        elif v in ("warn", "warning"):
            level = logging.WARNING
        logging.getLogger().setLevel(level)
        th.setLevel(level)
        logging.getLogger("mm_qbank").setLevel(level)

    cb_level.bind("<<ComboboxSelected>>", _apply_log_level)
    _apply_log_level()

    log_t = scrolledtext.ScrolledText(
        fr_log,
        height=14,
        state=tk.DISABLED,
        font=_font_mono or _font_main or ("Consolas", 10),
    )

    def _append_t(msg: str) -> None:
        log_t.config(state=tk.NORMAL)
        log_t.insert(tk.END, msg if msg.endswith("\n") else msg + "\n")
        log_t.see(tk.END)
        log_t.config(state=tk.DISABLED)

    log_t.pack(fill=tk.BOTH, expand=True)
    _append_t(_env_startup_hint())

    def poll_q() -> None:
        if _LOG_QUEUE is None:
            return
        buf: list[str] = []
        for _ in range(200):
            try:
                buf.append(_LOG_QUEUE.get_nowait())
            except queue.Empty:
                break
        if buf:
            _append_t("".join(buf))
        root.after(100, poll_q)

    root.after(150, poll_q)

    def _set_input_state(state: str) -> None:
        btn_dir["state"] = state
        chk_folder_batch["state"] = state

    def _reset_ui(*, keep_out: bool = True) -> None:
        work_dir[0] = None
        lbl_in.config(text="未选择", foreground="#666")
        chk_folder_batch["state"] = tk.NORMAL
        pb["value"] = 0
        st_lbl.config(text="空闲", foreground="black")
        root.config(cursor="")
        if not (keep_out and last_out[0] and Path(str(last_out[0])).is_dir()):
            last_out[0] = None
            btn_ref["state"] = tk.DISABLED

    def _set_run_btns(working: bool, paused: bool) -> None:
        is_running[0] = working
        btn_s["state"] = tk.DISABLED if working else tk.NORMAL
        btn_pause["state"] = tk.NORMAL if working else tk.DISABLED
        btn_pause["text"] = "继续" if paused else "暂停"
        btn_end["state"] = tk.NORMAL if working else tk.DISABLED

    batch_prog = {"idx": 0, "n": 1}

    def on_run() -> None:
        if is_running[0]:
            return
        wd = work_dir[0]
        if not wd or not wd.is_dir():
            messagebox.showwarning("提示", "请先选择图片文件夹。")
            return
        mode = pipeline_mode_var.get()
        folder_batches: list[Path] | None = None
        if folder_batch_var.get():
            folder_batches = _dirs_with_direct_images_dfs(wd)
            if not folder_batches:
                messagebox.showwarning("提示", "分批模式：没有含直属图片的文件夹层。")
                return
            n = sum(len(_list_direct_images_in_dir(b)) for b in folder_batches)
        else:
            n = len(_list_images(wd))
            if n < 1:
                messagebox.showwarning("提示", "文件夹内没有可处理的图片。")
                return
        run_cancel.clear()
        user_paused[0] = False
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        src_name = _safe_name(wd.name) if wd.name else "input"
        prefix = "lecture_gui" if mode == "lecture" else "correction_gui"
        out = project_root() / "data" / "out" / f"{prefix}_{ts}_{src_name}"
        out.mkdir(parents=True, exist_ok=True)
        last_out[0] = out
        lbl_path.config(text=f"本次输出: {out.resolve()}", foreground="black")
        mode_label = "教师讲解" if mode == "lecture" else "题目修正"
        st_lbl.config(text=f"合计 {n} 张 · 0%（{mode_label}）")
        _set_input_state(tk.DISABLED)
        _set_run_btns(True, False)
        btn_ref["state"] = tk.DISABLED
        root.config(cursor="wait")
        pb["value"] = 0

        def scale_prog(v: int, bi: int, nbt: int) -> int:
            if nbt <= 1:
                return max(0, min(1000, v))
            span = 1000.0 / nbt
            return min(1000, int(bi * span + v / 1000.0 * span))

        def paused_fn() -> bool:
            return user_paused[0]

        def on_vlm(done: int, total: int) -> None:
            v = int(400 * done / max(1, total))
            bi, nbt = batch_prog["idx"], batch_prog["n"]

            def u() -> None:
                pb["value"] = scale_prog(v, bi, nbt)
                st_lbl.config(text=f"VLM {done}/{total} · {100 * done // max(1, total)}%")

            root.after(0, u)

        def on_final(done: int, total: int) -> None:
            v = 400 + int(600 * done / max(1, total))
            bi, nbt = batch_prog["idx"], batch_prog["n"]
            label = "讲课" if mode == "lecture" else "修正"

            def u() -> None:
                pb["value"] = scale_prog(v, bi, nbt)
                st_lbl.config(text=f"{label} {done}/{total}")

            root.after(0, u)

        def work() -> None:
            err: str | None = None
            sm_acc: dict[str, Any] = {}
            try:
                if gui_llm_persist_cb:
                    gui_llm_persist_cb[0]()
                assert wd is not None
                if folder_batches:
                    loops = [(b, out / f"{i + 1:03d}_{_batch_output_slug(b, wd)}") for i, b in enumerate(folder_batches)]
                else:
                    loops = [(wd, out)]
                batch_prog["n"] = len(loops)
                summaries: list[dict[str, Any]] = []
                for bi, (batch_src, batch_out) in enumerate(loops):
                    batch_prog["idx"] = bi
                    while user_paused[0] and not run_cancel.is_set():
                        time.sleep(0.15)
                    if run_cancel.is_set():
                        sm_acc = {"cancelled": True, "mode": mode, "out_dir": str(out)}
                        break
                    batch_out.mkdir(parents=True, exist_ok=True)
                    pipeline_in = batch_src
                    staging: Path | None = None
                    if folder_batches:
                        staging = Path(tempfile.mkdtemp(prefix="mm_qbank_batch_"))
                        for img in _list_direct_images_in_dir(batch_src):
                            shutil.copy2(img, staging / img.name)
                        pipeline_in = staging
                    try:
                        if mode == "lecture":
                            sm_b = run_lecture_scan_pipeline(
                                input_dir=pipeline_in,
                                out_dir=batch_out,
                                config_path=None,
                                cancel_event=run_cancel,
                                paused=paused_fn,
                                on_vlm_page_done=on_vlm,
                                on_assemble_done=lambda: root.after(0, lambda: st_lbl.config(text="LLM 拆题与讲课内容…")),
                                on_lecture_progress=on_final,
                            )
                        else:
                            sm_b = run_correction_scan_pipeline(
                                input_dir=pipeline_in,
                                out_dir=batch_out,
                                config_path=None,
                                cancel_event=run_cancel,
                                paused=paused_fn,
                                on_vlm_page_done=on_vlm,
                                on_assemble_done=lambda: root.after(0, lambda: st_lbl.config(text="LLM 拆题与教材修正…")),
                                on_refine_progress=on_final,
                            )
                    finally:
                        if staging:
                            shutil.rmtree(staging, ignore_errors=True)
                    if sm_b.get("cancelled"):
                        sm_acc = {**sm_b, "mode": mode, "out_dir": str(out)}
                        break
                    sm_acc = {**sm_b, "mode": mode, "out_dir": str(out)}
                    summaries.append(
                        {
                            "name": _folder_batch_display_name(batch_src, wd) if folder_batches else batch_src.name,
                            "out_sub": str(batch_out),
                            "docx": sm_b.get("out_docx", ""),
                            "xlsx": sm_b.get("out_xlsx") or (sm_b.get("refine") or {}).get("out_xlsx", ""),
                            "n_questions": sm_b.get("n_questions", 0),
                        }
                    )
                if len(loops) > 1 and err is None and not sm_acc.get("cancelled"):
                    sm_acc["folder_batch_summaries"] = summaries
                    sm_acc["folder_batch_count"] = len(loops)
            except Exception as ex:  # noqa: BLE001
                err = str(ex)
                sm_acc = {}
            root.after(0, lambda e=err, s=sm_acc: on_done(e, s))

        def on_done(e: str | None, s: dict[str, Any]) -> None:
            root.config(cursor="")
            run_cancel.clear()
            _set_input_state(tk.NORMAL)
            _set_run_btns(False, False)
            if e:
                pb["value"] = 0
                st_lbl.config(text="失败", foreground="red")
                _append_t(f"\n[错误] {e}\n")
                messagebox.showerror("失败", e)
                _reset_ui(keep_out=True)
                return
            if s.get("cancelled"):
                st_lbl.config(text="已结束", foreground="#a60")
                messagebox.showinfo("已结束", f"输出: {s.get('out_dir', out)}")
                _reset_ui(keep_out=True)
                return
            pb["value"] = 1000
            od = str(s.get("out_dir") or out)
            last_out[0] = Path(od)
            btn_ref["state"] = tk.NORMAL
            mode = s.get("mode", "lecture")
            if mode == "lecture":
                st_lbl.config(text="完成（教师讲解）", foreground="green")
                docx = s.get("out_docx") or ""
                msg = f"拆题 {s.get('n_questions', 0)} 道\n讲义 Word:\n{docx or '（未生成）'}\n\n{od}"
            else:
                st_lbl.config(text="完成（题目修正）", foreground="green")
                xlsx = s.get("out_xlsx") or (s.get("refine") or {}).get("out_xlsx", "")
                msg = f"拆题 {s.get('n_questions', 0)} 道\n修正 xlsx:\n{xlsx or '（未生成）'}\n\n{od}"
            _append_t(f"\n==== 完成 ====\n{msg}\n")
            messagebox.showinfo("完成", msg)
            try:
                _reveal_dir(Path(od))
            except Exception as ex:  # noqa: BLE001
                _append_t(f"打开目录失败: {ex}\n")
            _reset_ui(keep_out=True)

        threading.Thread(target=work, daemon=True).start()

    btn_s["command"] = on_run
    btn_ref["command"] = lambda: _reveal_dir(last_out[0]) if last_out[0] else None

    def on_toggle_pause() -> None:
        if not is_running[0]:
            return
        user_paused[0] = not user_paused[0]
        btn_pause["text"] = "继续" if user_paused[0] else "暂停"

    def on_end() -> None:
        if is_running[0]:
            run_cancel.set()
            user_paused[0] = False
            st_lbl.config(text="正在结束…", foreground="#a60")

    btn_pause["command"] = on_toggle_pause
    btn_end["command"] = on_end

    root.mainloop()
    _LOG_QUEUE = None


if __name__ == "__main__":
    main()
