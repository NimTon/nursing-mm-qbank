"""
图形界面：
- VLM 整页转写（带预处理：EXIF 纠正 + 自动 0/90/180/270 旋转 + deskew）
- VLM 结果流式落盘（pages.jsonl 逐页写入）
- 按题号缓冲：同题「问题+解析」凑齐就触发教材向修正（凑满一批就请求），并流式追加写 CSV 便于断点续跑
- 最终导出 xlsx/jsonl；也可直接选择 llm_compose_merged.jsonl 仅修正

运行：
  mm-qbank-gui
或：
  python -m mm_qbank.gui_app
"""

from __future__ import annotations

import logging
import os
import queue
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
import yaml
import re

if sys.platform == "win32":
    # 1440p/高分屏下避免 Tk 被系统拉伸导致字体发糊：尽量启用 Per-Monitor DPI Aware
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
from mm_qbank.config import load_config, project_root
from mm_qbank.logging_utils import configure_logging
from mm_qbank.pipeline.llm_compose_run import run_llm_compose_manifest
from mm_qbank.pipeline.refine_vlm_run import (
    run_refine_from_compose_jsonl,
    run_refine_vlm_merged,
    run_vlm_text_and_refine_streaming,
)
from mm_qbank.pipeline.vlm_text_run import run_vlm_text_only

_LOG_QUEUE: "queue.Queue[str] | None" = None


class _TextQueueHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:  # noqa: N802
        if _LOG_QUEUE is not None:
            _LOG_QUEUE.put(self.format(record) + "\n")


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
    exts = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
    if not d.is_dir():
        return []
    return sorted(
        p
        for p in d.rglob("*")
        if p.is_file() and p.suffix.lower() in exts
    )


def _safe_name(name: str) -> str:
    s = (name or "").strip()
    s = re.sub(r"[\\/:*?\"<>|]+", "_", s)
    s = re.sub(r"\s+", "_", s)
    return s or "input"


def _install_gui_mascot_strip(parent: tk.Misc, font: tuple[Any, ...]) -> None:
    """在 parent 内放一条循环颜文字（应援棒挥动）；假定 parent 已按右上排版。"""
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
    after_id: list[str | int | None] = [None]

    def tick() -> None:
        nonlocal idx
        try:
            if not parent.winfo_exists():
                return
        except Exception:  # noqa: BLE001
            return
        idx = (idx + 1) % len(frames)
        lbl.config(text=frames[idx])
        w = parent.winfo_toplevel()
        after_id[0] = w.after(480, tick)

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
        print("当前解释器未带 tkinter，请使用官方 Python（含 Tcl/Tk）。", file=sys.stderr)  # noqa: T201
        raise SystemExit(1) from _tk_import_error

    load_dotenv(project_root() / ".env")

    _LOG_QUEUE = queue.Queue()
    # 必须先配置根 logger（会清空已有 handlers），再挂 GUI 的队列 handler；
    # 若先 addHandler 再 configure_logging，后者会 clear 掉 _TextQueueHandler，导致界面收不到与终端同等的日志。
    # GUI 默认 INFO，可在界面内切换 INFO/DEBUG/WARNING
    configure_logging(verbose=False, quiet=False)
    th = _TextQueueHandler()
    th.setLevel(logging.INFO)
    th.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    logging.getLogger().addHandler(th)
    for name in ("httpx", "httpcore", "openai", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in ("mm_qbank",):
        logging.getLogger(name).setLevel(logging.INFO)

    root = tk.Tk()
    root.title(f"nursing-mm-qbank v{__version__} · 流式转写与教材向修正")
    root.geometry("1200x800")
    root.minsize(720, 520)
    # 现代 ttk 主题（优先启用；缺依赖时自动降级）
    if sv_ttk is not None:
        try:
            sv_ttk.set_theme("light")
            logging.getLogger(__name__).info("GUI theme: sv-ttk light")
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).warning("GUI theme: sv-ttk 启用失败，将使用默认 ttk 主题", exc_info=True)
    else:
        logging.getLogger(__name__).info("GUI theme: default ttk (sv-ttk not installed)")

    # 统一字体层级：主 > 次 > 注释（避免“字体倒挂”）
    try:
        import tkinter.font as tkfont

        base_family = "Segoe UI" if sys.platform == "win32" else "TkDefaultFont"
        main_size = 10
        # 统一：容器内所有字号都使用 main_size
        sub_size = main_size
        small_size = main_size
        mono_family = "Consolas" if os.name == "nt" else "monospace"

        tkfont.nametofont("TkDefaultFont").configure(family=base_family, size=main_size)
        tkfont.nametofont("TkTextFont").configure(family=base_family, size=main_size)
        tkfont.nametofont("TkHeadingFont").configure(family=base_family, size=main_size, weight="bold")
        tkfont.nametofont("TkMenuFont").configure(family=base_family, size=main_size)
        tkfont.nametofont("TkTooltipFont").configure(family=base_family, size=main_size)
        tkfont.nametofont("TkFixedFont").configure(family=mono_family, size=main_size)

        _font_main = (base_family, main_size)
        _font_sub = _font_main
        _font_small = _font_main
        _font_mono = (mono_family, main_size)

        style = ttk.Style()
        style.configure(".", font=_font_main)
        style.configure("TLabelframe.Label", font=_font_main)
        # 某些主题（尤其是 sv-ttk）会对部分 ttk 样式/子控件使用单独字体；
        # 这里显式覆盖常见控件，避免出现“仍有小字体”的情况。
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

        # Combobox 弹出下拉列表是一个 Listbox，不一定吃 ttk Style，需用 option_add 强制字体。
        root.option_add("*TCombobox*Listbox.font", _font_main)
        root.option_add("*Listbox.font", _font_main)
    except Exception:  # noqa: BLE001
        _font_main = None
        _font_sub = None
        _font_small = None
        _font_mono = None

    # --- 主界面容器（不使用整体滚动条）
    content = ttk.Frame(root)
    content.pack(fill=tk.BOTH, expand=True)

    work_dir: list[Path | None] = [None]
    is_temp: list[bool] = [False]
    last_out: list[Path | None] = [None]
    compose_jsonl_in: list[Path | None] = [None]
    # 运行/暂停/取消：与 vlm_text_run 内「页与页之间」协作用；单次模型请求中无法暂停或结束直到该页返回
    run_cancel = threading.Event()
    user_paused: list[bool] = [False]
    is_running: list[bool] = [False]

    # --- 顶部：输入（不显示缩略图/列表）
    fr_top = ttk.LabelFrame(content, text="图片输入", padding=8)
    fr_top.pack(fill=tk.X, padx=8, pady=4)
    fr_top_bar = ttk.Frame(fr_top)
    fr_top_bar.pack(fill=tk.X)
    fr_top_right = ttk.Frame(fr_top_bar)
    fr_top_right.pack(side=tk.RIGHT, padx=(4, 0))
    ttk.Label(fr_top_right, text=f"v{__version__}", foreground="#666", font=_font_small).pack(anchor=tk.E)
    _mono_mascot: tuple[Any, ...] = _font_mono or (
        ("Consolas", 9) if os.name == "nt" else ("monospace", 9)
    )
    _install_gui_mascot_strip(fr_top_right, _mono_mascot)
    btn_dir = ttk.Button(fr_top_bar, text="选择图片文件夹…")
    btn_dir.pack(side=tk.LEFT, padx=(0, 8))
    btn_files = ttk.Button(fr_top_bar, text="选择图片（可多选）…")
    btn_files.pack(side=tk.LEFT)
    btn_compose = ttk.Button(fr_top_bar, text="选择 compose jsonl（仅修正）…")
    btn_compose.pack(side=tk.LEFT, padx=(8, 0))
    lbl_in = ttk.Label(fr_top, text="未选择", foreground="#666")
    lbl_in.pack(anchor=tk.W, pady=(4, 0))
    lbl_thumb_note = ttk.Label(
        fr_top,
        text=(
            "选择含图的文件夹或图片后，将开始处理（本界面不展示图片预览）。"
            "拍照建议：尽量正向、文字横平竖直，避免倒置与大角度倾斜；"
            "同一套题尽量保持题号清晰连续，避免多个题目出现重复题号。"
        ),
        foreground="#666",
        font=_font_small,
        wraplength=780,
    )
    lbl_thumb_note.pack(anchor=tk.W, pady=(2, 0))

    def _clear_input_preview() -> None:
        compose_jsonl_in[0] = None
        lbl_thumb_note.config(
            text=(
                "选择含图的文件夹或图片后，将开始处理（本界面不展示图片预览）。"
                "拍照建议：尽量正向、文字横平竖直，避免倒置与大角度倾斜；"
                "同一套题尽量保持题号清晰连续，避免多个题目出现重复题号。"
            ),
            foreground="#666",
        )

    def on_pick_dir() -> None:
        p = filedialog.askdirectory(title="选择含图片的文件夹", mustexist=True)
        if not p:
            return
        compose_jsonl_in[0] = None
        d = Path(p)
        img_paths = _list_images(d)
        work_dir[0] = d
        is_temp[0] = False
        nic = len(img_paths)
        lbl_in.config(text=f"文件夹: {d}  （共 {nic} 张图）", foreground="black")
        if not img_paths:
            lbl_thumb_note.config(text="该路径下无支持的图片格式。", foreground="#a60")
        else:
            lbl_thumb_note.config(text=f"已选择 {nic} 张图片，点击「开始」处理。", foreground="#444")
        if not img_paths:
            messagebox.showwarning("提示", "该目录下没有支持的图片（.png / .jpg / .jpeg / .bmp / .webp）。")

    def on_pick_files() -> None:
        files = filedialog.askopenfilenames(
            title="选择一个或多个图片",
            filetypes=[
                ("图片", "*.png;*.jpg;*.jpeg;*.bmp;*.webp"),
                ("全部", "*.*"),
            ],
        )
        if not files:
            return
        compose_jsonl_in[0] = None
        paths = [Path(f) for f in files if Path(f).is_file()]
        if not paths:
            return
        tmp = Path(tempfile.mkdtemp(prefix="mm_qbank_gui_"))
        for src in paths:
            shutil.copy2(src, tmp / src.name)
        work_dir[0] = tmp
        is_temp[0] = True
        img_paths = _list_images(tmp)
        lbl_in.config(
            text=(
                f"已选 {len(paths)} 个文件  （临时: {tmp}，结束后自动删除，"
                f"共 {len(img_paths)} 张有效图）"
            ),
            foreground="black",
        )
        if not img_paths:
            lbl_thumb_note.config(text="该路径下无支持的图片格式。", foreground="#a60")
        else:
            lbl_thumb_note.config(text=f"已选择 {len(img_paths)} 张图片，点击「开始」处理。", foreground="#444")
        if not img_paths:
            messagebox.showwarning("提示", "未复制到有效图片，请重选。")

    def on_pick_compose_jsonl() -> None:
        p = filedialog.askopenfilename(
            title="选择 llm_compose_merged.jsonl（仅修正）",
            filetypes=[("JSONL", "*.jsonl"), ("全部", "*.*")],
        )
        if not p:
            return
        cp = Path(p)
        if not cp.is_file():
            return
        # compose 输入与图片输入互斥：选 compose 后清空图片列表与缩略图
        _clear_input_preview()
        work_dir[0] = None
        is_temp[0] = False
        compose_jsonl_in[0] = cp
        lbl_in.config(text=f"compose jsonl: {cp}", foreground="black")
        lbl_thumb_note.config(
            text="已选择 compose jsonl：将跳过 VLM/拆题，直接进行教材向修正（支持断点续跑）。",
            foreground="#444",
        )

    btn_dir["command"] = on_pick_dir
    btn_files["command"] = on_pick_files
    btn_compose["command"] = on_pick_compose_jsonl

    # 输出说明
    fr_out = ttk.LabelFrame(content, text="输出位置", padding=6)
    fr_out.pack(fill=tk.X, padx=8, pady=4)
    ex = (project_root() / "data" / "out" / "vlm_gui_YYYYMMDD_hhmmss_输入文件夹名").as_posix()
    lbl_path = ttk.Label(fr_out, text=f"开始转写时生成时间戳，目录形如: {ex}", wraplength=800)
    lbl_path.pack(anchor=tk.W)

    # 进度
    fr_pb = ttk.LabelFrame(content, text="进度", padding=6)
    fr_pb.pack(fill=tk.X, padx=8, pady=2)
    pb = ttk.Progressbar(fr_pb, mode="determinate", length=780, maximum=1000, value=0)
    pb.pack(fill=tk.X, pady=(0, 4))
    st_lbl = ttk.Label(fr_pb, text="空闲")
    st_lbl.pack(anchor=tk.W)
    fr_pb_btns = ttk.Frame(fr_pb)
    fr_pb_btns.pack(fill=tk.X, pady=2)
    btn_ref = ttk.Button(fr_pb_btns, text="打开输出目录", state=tk.DISABLED, width=18)
    btn_ref.pack(side=tk.LEFT)

    # 日志
    fr_log = ttk.LabelFrame(content, text="日志", padding=4)
    fr_log.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
    fr_log_bar = ttk.Frame(fr_log)
    fr_log_bar.pack(fill=tk.X, pady=(0, 4))
    # 主题切换
    ttk.Label(fr_log_bar, text="主题：").pack(side=tk.LEFT)
    style = ttk.Style()
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
    log_level_var = tk.StringVar(value="INFO")
    cb_level = ttk.Combobox(
        fr_log_bar,
        textvariable=log_level_var,
        values=["INFO", "DEBUG", "WARNING"],
        width=10,
        state="readonly",
    )
    cb_level.pack(side=tk.LEFT)

    def _apply_log_level(_: Any | None = None) -> None:
        v = (log_level_var.get() or "INFO").strip().upper()
        level = logging.INFO
        if v == "DEBUG":
            level = logging.DEBUG
        elif v in ("WARN", "WARNING"):
            level = logging.WARNING
        # 根 logger + GUI handler 同步；第三方库仍保持 WARNING 避免刷屏
        logging.getLogger().setLevel(level)
        th.setLevel(level)
        logging.getLogger("mm_qbank").setLevel(level)

    cb_level.bind("<<ComboboxSelected>>", _apply_log_level)
    _apply_log_level()

    font_ = _font_mono or (("Consolas", 11) if os.name == "nt" else ("monospace", 11))
    log_t = scrolledtext.ScrolledText(fr_log, height=16, state=tk.DISABLED, font=font_)

    def _append_t(msg: str) -> None:
        log_t.config(state=tk.NORMAL)
        log_t.insert(tk.END, msg)
        if not msg.endswith("\n"):
            log_t.insert(tk.END, "\n")
        log_t.see(tk.END)
        log_t.config(state=tk.DISABLED)

    log_t.pack(fill=tk.BOTH, expand=True)
    _append_t("将 mm-qbank 的日志显示在此处；请在项目根 .env 中配置 VLM_* / LLM_* 后启动本程序（或改 configs/default.yaml）。\n\n")

    def poll_q() -> None:
        if _LOG_QUEUE is None:
            return
        # 重要：限制每次 UI tick 处理的日志条数，避免日志刷屏时阻塞主线程导致“界面未响应”。
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

    # --- 开始 / 打开
    def on_open() -> None:
        p = last_out[0]
        if p and p.is_dir():
            try:
                _reveal_dir(p)
            except Exception as e:  # noqa: BLE001
                messagebox.showerror("无法打开", str(e))
        else:
            messagebox.showinfo("提示", "尚无可打开的目录。请先成功完成一次转写。")

    btn_ref["command"] = on_open

    def _set_input_buttons(state: str) -> None:
        btn_dir["state"] = state
        btn_files["state"] = state
        btn_compose["state"] = state

    def _reset_to_initial(*, keep_last_out: bool = True) -> None:
        """一次任务结束后回到初始可运行状态（保留日志；可选择保留 last_out 以便打开目录）。"""
        # 清空输入选择
        work_dir[0] = None
        is_temp[0] = False
        compose_jsonl_in[0] = None
        _clear_input_preview()
        lbl_in.config(text="未选择", foreground="#666")
        # 进度与状态
        pb["value"] = 0
        st_lbl.config(text="空闲", foreground="black")
        root.config(cursor="")
        # “打开输出目录”是否可用
        if keep_last_out and last_out[0] and Path(str(last_out[0])).is_dir():
            btn_ref["state"] = tk.NORMAL
        else:
            last_out[0] = None
            btn_ref["state"] = tk.DISABLED

    def _set_run_buttons(*, working: bool, paused: bool) -> None:
        is_running[0] = working
        if not working:
            btn_s["state"] = tk.NORMAL
            btn_pause["state"] = tk.DISABLED
            btn_pause["text"] = "暂停"
            user_paused[0] = False
            btn_end["state"] = tk.DISABLED
        else:
            btn_s["state"] = tk.DISABLED
            btn_pause["state"] = tk.NORMAL
            btn_pause["text"] = "继续" if paused else "暂停"
            btn_end["state"] = tk.NORMAL

    def on_run() -> None:
        if is_running[0]:
            return
        wd = work_dir[0]
        cp = compose_jsonl_in[0]
        if cp is None:
            if not wd or not wd.is_dir():
                messagebox.showwarning("提示", "请先通过「选文件夹」或「选多图」添加输入。")
                return
            n = len(_list_images(wd))
            if n < 1:
                messagebox.showwarning("提示", "当前输入路径下没有可处理的图片。")
                return
        else:
            n = 0
        was_temp = is_temp[0]
        run_cancel.clear()
        user_paused[0] = False
        # compose 模式：输出写到 compose jsonl 同目录，便于断点续跑复用 refined_merged_stream.csv
        if cp is not None:
            out = cp.parent
        else:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            src_name = "input"
            if wd is not None and isinstance(wd, Path) and wd.name:
                src_name = _safe_name(wd.name)
            out = project_root() / "data" / "out" / f"vlm_gui_{ts}_{src_name}"
            out.mkdir(parents=True, exist_ok=True)
        last_out[0] = out
        lbl_path.config(text=f"本次输出: {out.resolve()}", foreground="black")

        # 导出文件名也带上“输入文件夹名”，避免同目录下分不清来源
        if cp is not None:
            export_tag = _safe_name(cp.parent.name or "compose")
        else:
            export_tag = src_name
        out_jsonl_path = out / f"refined_merged_{export_tag}.jsonl"
        out_csv_path = out / f"refined_merged_stream_{export_tag}.csv"
        out_xlsx_path = out / f"refined_merged_{export_tag}.xlsx"

        if cp is not None:
            st_lbl.config(text="compose jsonl → 教材向修正（流式 CSV + xlsx）…")
        else:
            if bool(stream_refine_var.get()):
                st_lbl.config(
                    text=(
                        f"待处理 {n} 张 · 0%（流式：VLM 多图并行 + 流式落盘；"
                        f"按题号凑齐后触发教材向修正并流式写 CSV；页与页之间可暂停/结束）"
                    )
                )
            else:
                st_lbl.config(
                    text=(
                        f"待处理 {n} 张 · 0%（顺序：VLM 全部完成后再开始教材向修正；"
                        f"页与页之间可暂停/结束，单页请求中需等待返回）"
                    )
                )
        _set_input_buttons(tk.DISABLED)
        _set_run_buttons(working=True, paused=False)
        btn_ref["state"] = tk.DISABLED
        root.config(cursor="wait")
        root.after(0, lambda: pb.config(mode="determinate", maximum=1000, value=0))

        def paused_fn() -> bool:
            return user_paused[0]

        def on_vlm_page(i: int, t: int) -> None:
            if t <= 0:
                return
            # 进度：VLM 占 50%
            v = int(500 * i / t)

            def u() -> None:
                pb["value"] = min(1000, v)
                p = 100.0 * i / t
                if bool(stream_refine_var.get()):
                    st_lbl.config(text=f"VLM {i}/{t} · {p:.1f}%（流式：凑题缓存→教材向修正进行中…）")
                else:
                    st_lbl.config(text=f"VLM {i}/{t} · {p:.1f}%（顺序：VLM 完成后才开始教材向修正…）")

            root.after(0, u)

        def on_refine_progress(n_done: int, n_total: int) -> None:
            # 进度：LLM(修正) 占 50%
            if n_total <= 0:
                v = 500
            else:
                v = 500 + int(500 * min(max(n_done, 0), n_total) / max(1, n_total))

            def u() -> None:
                pb["value"] = min(1000, max(0, v))
                # 状态补充：让用户能直观看到 LLM 修正进度在走
                if n_total > 0:
                    st_lbl.config(text=f"教材向修正 {n_done}/{n_total} · {50.0 + 50.0 * min(max(n_done, 0), n_total) / max(1, n_total):.1f}%")

            root.after(0, u)

        def on_llm_page(j: int, t: int) -> None:
            if t <= 0:
                return
            v = 500 + int(500 * j / t)

            def u() -> None:
                pb["value"] = min(1000, v)
                p = 50.0 + 50.0 * j / t
                st_lbl.config(
                    text=f"拆题 {j}/{t} · {p:.1f}%（离线提取 composed_items，不额外调用模型）"
                )

            root.after(0, u)

        refined_n: list[int] = [0]

        def on_refine_item_done(n_done: int) -> None:
            refined_n[0] = int(n_done)

            def u() -> None:
                # 不改变进度条（由 VLM 页控制），只更新状态文字展示已修正题数
                if "text" in st_lbl.keys():
                    cur = st_lbl.cget("text")
                else:
                    cur = ""
                if cur:
                    st_lbl.config(text=f"{cur} · 已修正 {refined_n[0]} 题")
                else:
                    st_lbl.config(text=f"已修正 {refined_n[0]} 题")

            root.after(0, u)

        def work() -> None:
            err: str | None = None
            vlm: dict[str, Any] = {}
            try:
                runtime_cfg = _write_runtime_config(out)
                if cp is None:
                    if bool(stream_refine_var.get()):
                        # 流式模式：VLM 每页完成就进入题号缓冲，凑齐同题问题+解析即入批，满批就请求修正并流式写 CSV
                        root.after(0, lambda: st_lbl.config(text="流式：VLM →（凑题缓存）→ 教材向修正（LLM）…"))
                        s = run_vlm_text_and_refine_streaming(
                            input_dir=wd,  # type: ignore[arg-type]
                            out_dir=out,
                            config_path=runtime_cfg,
                            vlm_model=None,
                            refine_model=None,
                            out_jsonl=out_jsonl_path,
                            out_csv=out_csv_path,
                            out_xlsx=out_xlsx_path,
                            cancel_event=run_cancel,
                            paused=paused_fn,
                            on_vlm_page_done=on_vlm_page,
                            on_refine_item_done=on_refine_item_done,
                            on_refine_progress=on_refine_progress,
                        )
                        # GUI 旧结构兼容：把 refine 摘要塞回 vlm dict
                        rf = {
                            "n_rows": s.get("n_rows", 0),
                            "out_jsonl": s.get("out_jsonl", ""),
                            "out_csv": s.get("out_csv", ""),
                            "out_xlsx": s.get("out_xlsx", ""),
                            "model": s.get("refine_model", ""),
                            "web_search": s.get("web_search", False),
                        }
                        vlm = {k: v for k, v in s.items() if k not in ("n_rows", "out_jsonl", "out_csv", "out_xlsx", "refine_model")}
                        vlm["refine"] = rf
                    else:
                        # 顺序模式：先等待 VLM 全部页完成并落盘（pages.jsonl），再统一跑教材向修正（LLM）。
                        vlm = run_vlm_text_only(
                            input_dir=wd,  # type: ignore[arg-type]
                            out_dir=out,
                            config_path=None,
                            model=None,
                            cancel_event=run_cancel,
                            paused=paused_fn,
                            on_page_done=on_vlm_page,
                        )
                        mp = vlm.get("manifest")
                        ok_m = (
                            bool(mp)
                            and Path(str(mp)).is_file()
                            and any(L.strip() for L in Path(str(mp)).read_text(encoding="utf-8").splitlines())
                        )
                        if ok_m and not vlm.get("cancelled", False):
                            root.after(0, lambda: st_lbl.config(text="VLM 已完成，开始教材向修正（等待全部 VLM 后再给 LLM）…"))
                            rf = run_refine_vlm_merged(
                                manifest_path=Path(str(mp)),
                                out_jsonl=out_jsonl_path,
                                out_csv=out_csv_path,
                                out_xlsx=out_xlsx_path,
                                config_path=runtime_cfg,
                                model=None,
                                on_refine_progress=on_refine_progress,
                            )
                            # 与旧 streaming 返回结构兼容：把 refine 摘要塞回 vlm dict
                            vlm = {**vlm, "refine": rf}
                else:
                    vlm = {}
            except Exception as e:  # noqa: BLE001
                err = str(e)
                vlm = {}

            sm: dict[str, Any] = {}
            if cp is not None and err is None:
                sm = {"mode": "compose_only", "out_dir": str(out), "llm_compose": None, "refine": None}
                try:
                    root.after(0, lambda: st_lbl.config(text="教材向修正并导出 xlsx（compose 输入）…"))
                    sm["refine"] = run_refine_from_compose_jsonl(
                        compose_jsonl=cp,
                        out_jsonl=out_jsonl_path,
                        out_csv=out_csv_path,
                        out_xlsx=out_xlsx_path,
                        config_path=_write_runtime_config(out),
                        model=None,
                        on_refine_progress=on_refine_progress,
                    )
                except Exception as e3:  # noqa: BLE001
                    sm["refine"] = {"error": str(e3)}
            elif err is None and vlm and not vlm.get("cancelled", False):
                # 顺序模式：若上游已跑过 refine，则复用；否则仅做 compose
                sm = {**vlm, "llm_compose": None, "refine": (vlm.get("refine") if isinstance(vlm.get("refine"), dict) else None), "compose_error": None}
                mp = vlm.get("manifest")
                ok_m = (
                    bool(mp)
                    and Path(mp).is_file()
                    and any(L.strip() for L in Path(mp).read_text(encoding="utf-8").splitlines())
                )
                if ok_m:
                    mpath = Path(mp)
                    try:
                        comp_out = out / "llm_compose_merged.jsonl"
                        sm["llm_compose"] = run_llm_compose_manifest(
                            manifest_path=mpath,
                            out_jsonl=comp_out,
                            config_path=None,
                            model=None,
                            cancel_event=run_cancel,
                            paused=paused_fn,
                            on_page_done=on_llm_page,
                        )
                    except Exception as e2:  # noqa: BLE001
                        sm["compose_error"] = str(e2)
                        sm["llm_compose"] = None
                    # refine：顺序模式下可能已完成（vlm['refine']），否则不强行填充
            elif vlm:
                sm = {**vlm, "llm_compose": None, "refine": None}
            if not sm and vlm:
                sm = {**vlm, "llm_compose": None, "refine": None}

            try:
                if cp is None and was_temp and wd and wd.is_dir():
                    shutil.rmtree(wd, ignore_errors=True)
                    is_temp[0] = False
                    work_dir[0] = None

                    def _after_temp_removed() -> None:
                        _clear_input_preview()
                        lbl_in.config(text="多选图片的临时目录已删；请重新选择输入", foreground="#666")

                    root.after(0, _after_temp_removed)
            except OSError:  # noqa: BLE001
                pass

            e_done = err
            s_done = sm
            root.after(0, lambda e=e_done, s=s_done: on_done(e, s))

        def on_done(e: str | None, s: dict[str, Any]) -> None:
            root.config(cursor="")
            run_cancel.clear()
            _set_input_buttons(tk.NORMAL)
            _set_run_buttons(working=False, paused=False)
            if e is not None:
                pb["value"] = 0
                st_lbl.config(text="失败", foreground="red")
                _append_t(f"\n[错误] {e}\n")
                messagebox.showerror("转写失败", e)
                _reset_to_initial(keep_last_out=True)
                return
            lc = s.get("llm_compose")
            vlm_c = s.get("cancelled", False)
            llm_c = bool((lc or {}).get("cancelled", False) if isinstance(lc, dict) else False)
            if vlm_c or llm_c:
                pb["value"] = 0
                n_done = s.get("n_pages", 0)
                n_tot = s.get("n_total_images", n_done)
                phase = "LLM" if vlm_c is False and llm_c else "VLM/全程"
                st_lbl.config(text="已结束（用户）", foreground="#a60")
                mnf = s.get("manifest", "")
                _append_t(
                    f"\n==== 已结束/取消（{phase}）====  VLM: {n_done} / 共 {n_tot} 张\n清单: {mnf}\n"
                )
                if isinstance(lc, dict) and llm_c:
                    _append_t(
                        f"LLM 已写 {lc.get('n_written', 0)}/{lc.get('n_pages', 0)} 行 -> {lc.get('out_jsonl', '')}\n"
                    )
                messagebox.showinfo("已结束", f"已停止（{phase} 阶段可）。\n输出: {s.get('out_dir', out)}")
                od = s.get("out_dir") or str(out)
                last_path = Path(od)
                if last_path.is_dir():
                    last_out[0] = last_path
                btn_ref["state"] = tk.NORMAL
                try:
                    _reveal_dir(last_path)
                except Exception as ex:  # noqa: BLE001
                    _append_t(f"自动打开目录失败: {ex}\n")
                _reset_to_initial(keep_last_out=True)
                return
            pb["value"] = 1000
            od = s.get("out_dir") or str(out)
            last_path = Path(od)
            if last_path.is_dir():
                last_out[0] = last_path
            st_lbl.config(text="完成", foreground="green")
            btn_ref["state"] = tk.NORMAL
            npg = s.get("n_pages", "")
            mnf = s.get("manifest", "")
            _append_t(f"\n==== 完成 ====  VLM 页数: {npg}\nVLM 清单: {mnf}\n")
            if isinstance(lc, dict) and lc.get("out_jsonl"):
                _append_t(f"LLM 拆题: {lc.get('out_jsonl')}\n")
            rf = s.get("refine")
            if isinstance(rf, dict) and rf.get("out_xlsx") and not rf.get("error"):
                _append_t(f"修正 + xlsx: {rf.get('out_xlsx')}\n")
                if rf.get("out_csv"):
                    _append_t(f"修正（流式 CSV）: {rf.get('out_csv')}\n")
            elif isinstance(rf, dict) and rf.get("error"):
                _append_t(f"[修正/xlsx 未生成] {rf.get('error')}\n")
            ce = s.get("compose_error")
            if ce:
                _append_t(f"[LLM 拆题失败] {ce}\n")
            dmsg = f"已写入: {od}"
            if isinstance(lc, dict) and (lc.get("out_jsonl")):
                dmsg += f"\n\nLLM 拆题: {lc['out_jsonl']}"
            else:
                dmsg += "\n\n（本任务未跑拆题或无可拆页）"
            if isinstance(rf, dict) and rf.get("out_xlsx") and not rf.get("error"):
                dmsg += f"\n\nxlsx: {rf['out_xlsx']}"
            elif isinstance(rf, dict) and rf.get("error"):
                dmsg += f"\n\n修正/xlsx 失败: {rf.get('error', '')}"
            if ce:
                dmsg += f"\n\n拆题失败（已跳过或部分跳过）: {ce}"
            messagebox.showinfo("完成", f"{dmsg}\n\n将打开该文件夹。")
            try:
                _reveal_dir(last_out[0] or out)
            except Exception as ex:  # noqa: BLE001
                _append_t(f"自动打开目录失败: {ex}\n")
            _reset_to_initial(keep_last_out=True)

        threading.Thread(target=work, daemon=True).start()

    def on_toggle_pause() -> None:
        if not is_running[0]:
            return
        user_paused[0] = not user_paused[0]
        btn_pause["text"] = "继续" if user_paused[0] else "暂停"
        st_lbl.config(
            text=("已暂停（下一整页开始前可点继续）" if user_paused[0] else "处理中（页间可暂停/结束）"),
            foreground=("#a60" if user_paused[0] else "black"),
        )

    def on_end() -> None:
        if not is_running[0]:
            return
        run_cancel.set()
        user_paused[0] = False
        st_lbl.config(text="正在结束…（当前页若已请求网络则须等其返回）", foreground="#a60")
        # 不立即改 暂停 文案，on_done 会整组重置

    def open_kb_dialog() -> None:
        # 延迟导入：sentence-transformers/faiss 依赖较重，避免 GUI 启动即加载
        try:
            from mm_qbank.kb.kb_build import build_kb_from_pdf_dir
            from mm_qbank.kb.kb_store import kb_dir_from_arg, load_kb
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            help_lines = [
                "导入知识库模块失败。",
                "",
                f"错误：{msg}",
                "",
                "这通常是因为缺少依赖（embedding/向量库）。请在项目根目录执行：",
                "  pip install -e .",
                "",
                "如果你不是以可编辑模式安装，也可以直接装缺的包：",
                "  pip install sentence-transformers faiss-cpu pypdf",
                "",
                "提示：Windows 下 faiss-cpu 可能需要较新的 pip；若安装失败，把报错日志发我我来给你对应的安装命令。",
            ]
            messagebox.showerror("无法打开知识库工具", "\n".join(help_lines))
            return

        win = tk.Toplevel(root)
        win.title("知识库（PDF→分块→Embedding→FAISS）")
        win.geometry("760x560")
        win.minsize(720, 520)
        try:
            win.transient(root)
            win.grab_set()
        except Exception:  # noqa: BLE001
            pass

        nb = ttk.Notebook(win)
        nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        tab_build = ttk.Frame(nb, padding=10)
        tab_query = ttk.Frame(nb, padding=10)
        nb.add(tab_build, text="构建")
        nb.add(tab_query, text="查询")

        # --- 构建 tab
        # PDF 输入：支持“选择多文件”写入，也支持用户手动粘贴路径（分号/换行分隔）
        pdf_pick_var = tk.StringVar(value="")
        kb_parent_var = tk.StringVar(value="data/kb")
        local_default = (project_root() / "models" / "BAAI" / "bge-small-zh-v1.5").resolve()
        model_var = tk.StringVar(value=(str(local_default) if local_default.is_dir() else "BAAI/bge-small-zh-v1.5"))
        chunk_chars_var = tk.StringVar(value="900")
        overlap_var = tk.StringVar(value="120")
        batch_var = tk.StringVar(value="32")
        build_status_var = tk.StringVar(value="空闲")
        build_busy: list[bool] = [False]
        last_kb_root: list[Path | None] = [None]
        picked_pdfs: list[Path] = []

        fr_b_in = ttk.LabelFrame(tab_build, text="输入与参数", padding=8)
        fr_b_in.pack(fill=tk.X, pady=(0, 10))

        def _row(parent: Any) -> ttk.Frame:
            r = ttk.Frame(parent)
            r.pack(fill=tk.X, pady=4)
            return r

        r1 = _row(fr_b_in)
        ttk.Label(r1, text="PDF 文件：", width=10).pack(side=tk.LEFT)
        ent_pdf = ttk.Entry(r1, textvariable=pdf_pick_var)
        ent_pdf.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        def pick_pdf_files() -> None:
            nonlocal picked_pdfs
            files = filedialog.askopenfilenames(
                title="选择一个或多个 PDF（每个 PDF 构建一个 KB）",
                filetypes=[("PDF", "*.pdf"), ("全部", "*.*")],
            )
            if not files:
                return
            picked_pdfs = [Path(f) for f in files if Path(f).is_file()]
            # 把真实路径写进输入框，避免“只显示已选N个”导致构建时拿不到文件列表
            pdf_pick_var.set(";\n".join(str(p) for p in picked_pdfs))

        ttk.Button(r1, text="选择…", width=10, command=pick_pdf_files).pack(side=tk.LEFT)

        r2 = _row(fr_b_in)
        ttk.Label(r2, text="KB 父目录：", width=10).pack(side=tk.LEFT)
        ttk.Entry(r2, textvariable=kb_parent_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(r2, text="（默认 data/kb；每个 PDF 用文件名建子目录）", foreground="#666").pack(
            side=tk.LEFT, padx=(8, 0)
        )

        r3 = _row(fr_b_in)
        ttk.Label(r3, text="模型：", width=10).pack(side=tk.LEFT)
        ttk.Entry(r3, textvariable=model_var).pack(side=tk.LEFT, fill=tk.X, expand=True)

        r4 = _row(fr_b_in)
        ttk.Label(r4, text="chunk：", width=10).pack(side=tk.LEFT)
        ttk.Entry(r4, textvariable=chunk_chars_var, width=10).pack(side=tk.LEFT)
        ttk.Label(r4, text="overlap：", width=10).pack(side=tk.LEFT, padx=(10, 0))
        ttk.Entry(r4, textvariable=overlap_var, width=10).pack(side=tk.LEFT)
        ttk.Label(r4, text="batch：", width=10).pack(side=tk.LEFT, padx=(10, 0))
        ttk.Entry(r4, textvariable=batch_var, width=10).pack(side=tk.LEFT)

        fr_b_ops = ttk.Frame(tab_build)
        fr_b_ops.pack(fill=tk.X, pady=(0, 6))
        btn_build = ttk.Button(fr_b_ops, text="开始构建", width=14)
        btn_build.pack(side=tk.LEFT)
        btn_open_kb = ttk.Button(fr_b_ops, text="打开 KB 目录", width=14, state=tk.DISABLED)
        btn_open_kb.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Label(fr_b_ops, textvariable=build_status_var, foreground="#666").pack(side=tk.LEFT, padx=(12, 0))

        txt_build = scrolledtext.ScrolledText(tab_build, height=14, state=tk.DISABLED, font=_font_mono or font_)
        txt_build.pack(fill=tk.BOTH, expand=True)

        def _append_build(msg: str) -> None:
            txt_build.config(state=tk.NORMAL)
            txt_build.insert(tk.END, msg)
            if not msg.endswith("\n"):
                txt_build.insert(tk.END, "\n")
            txt_build.see(tk.END)
            txt_build.config(state=tk.DISABLED)

        def _set_build_busy(b: bool) -> None:
            build_busy[0] = b
            btn_build["state"] = (tk.DISABLED if b else tk.NORMAL)
            ent_pdf["state"] = (tk.DISABLED if b else tk.NORMAL)
            btn_open_kb["state"] = (tk.NORMAL if (not b and last_kb_root[0] and last_kb_root[0].is_dir()) else tk.DISABLED)

        def on_open_kb_dir() -> None:
            p = last_kb_root[0]
            if p and p.is_dir():
                _reveal_dir(p)

        btn_open_kb["command"] = on_open_kb_dir

        def on_build() -> None:
            if build_busy[0]:
                return
            kb_parent_s = (kb_parent_var.get() or "").strip()
            model_s = (model_var.get() or "").strip()
            # 兼容：若用户手动粘贴到输入框，则从输入框解析；否则用选择器缓存的 picked_pdfs
            pdfs: list[Path] = []
            if picked_pdfs:
                pdfs = list(picked_pdfs)
            else:
                raw = (pdf_pick_var.get() or "").strip()
                if raw:
                    parts = [p.strip().strip('"') for p in raw.replace("\r", "").replace("\n", ";").split(";")]
                    pdfs = [Path(p) for p in parts if p]
            pdfs = [p for p in pdfs if p.is_file() and p.suffix.lower() == ".pdf"]
            if not pdfs:
                messagebox.showwarning("提示", "请先选择一个或多个 PDF 文件（或在输入框粘贴 PDF 路径）。")
                return
            if not model_s:
                messagebox.showwarning("提示", "请填写 embedding 模型名。")
                return
            try:
                chunk_chars = int(chunk_chars_var.get() or "900")
                overlap = int(overlap_var.get() or "120")
                batch = int(batch_var.get() or "32")
            except ValueError:
                messagebox.showwarning("提示", "chunk/overlap/batch 必须是整数。")
                return
            kb_parent = Path(kb_parent_s) if kb_parent_s else (project_root() / "data" / "kb")
            if not kb_parent.is_absolute():
                kb_parent = (project_root() / kb_parent).resolve()
            kb_parent.mkdir(parents=True, exist_ok=True)

            _set_build_busy(True)
            build_status_var.set("构建中…")
            _append_build(f"\n==== 开始构建（每个 PDF 一个 KB）====\nPDF: {len(pdfs)} 个\nKB 父目录: {kb_parent}\nmodel: {model_s}\n")
            logging.getLogger(__name__).info("kb-build: n_pdfs=%s kb_parent=%s model=%s", len(pdfs), kb_parent, model_s)

            def work() -> None:
                err: str | None = None
                summaries: list[dict[str, Any]] = []
                kb_root: Path | None = None
                try:
                    from mm_qbank.kb.kb_build import build_kb_from_pdf_paths  # 延迟导入

                    used: set[str] = set()
                    total_books = len(pdfs)
                    last_ui_log_at = 0.0
                    invalid = '<>:"/\\\\|?*'

                    def _safe_dir_name(s: str) -> str:
                        # Windows 文件夹名清洗 + 去尾部空格/点
                        # 重要：FAISS 在 Windows 上对「非 ASCII 路径」可能无法写入（底层 fopen）。
                        # 因此：若包含非 ASCII，使用稳定 hash 作为目录名；否则做常规清洗。
                        raw = (s or "").strip()
                        if any(ord(ch) > 127 for ch in raw):
                            import hashlib

                            digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
                            return f"kb_{digest}"
                        out = "".join(("_" if ch in invalid else ch) for ch in raw)
                        out = out.strip().strip(".")
                        return out or "book"

                    def on_progress(stage: str, done: int, total: int | None, msg: str) -> None:
                        # embedding 录入会非常频繁，这里做节流，避免刷屏阻塞 GUI
                        nonlocal last_ui_log_at
                        now = time.time()
                        if stage == "embed_batch" and (now - last_ui_log_at) < 0.35:
                            return
                        last_ui_log_at = now

                        def u() -> None:
                            if total and total > 0:
                                _append_build(f"[{stage}] {msg}（{done}/{total}）")
                                build_status_var.set(f"{stage}: {done}/{total}")
                            else:
                                _append_build(f"[{stage}] {msg}")
                                build_status_var.set(f"{stage}: {done}")

                        root.after(0, u)

                    for bi, pdf_path in enumerate(list(pdfs), start=1):
                        display_name = (pdf_path.stem or f"book_{bi}").strip() or f"book_{bi}"
                        stem = _safe_dir_name(display_name)
                        # 避免同名覆盖
                        name = stem
                        k = 2
                        while name in used or (kb_parent / name).exists():
                            name = f"{stem}_{k}"
                            k += 1
                        used.add(name)
                        kb_root = (kb_parent / name).resolve()
                        # 显式创建目录，避免后续底层写入时报“目录不存在”
                        kb_root.mkdir(parents=True, exist_ok=True)
                        if not kb_root.is_dir():
                            raise OSError(f"无法创建知识库目录: {kb_root}")

                        # 维护一个父目录级别的映射：safe_name -> 原始显示名
                        try:
                            import json

                            mp_path = (kb_parent / "name_map.json").resolve()
                            mp: dict[str, str] = {}
                            if mp_path.is_file():
                                mp0 = json.loads(mp_path.read_text(encoding="utf-8") or "{}")
                                if isinstance(mp0, dict):
                                    mp = {str(k): str(v) for k, v in mp0.items()}
                            mp[name] = display_name
                            mp_path.write_text(json.dumps(mp, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                        except Exception:
                            pass

                        root.after(
                            0,
                            lambda bi=bi, tb=total_books, pdf_path=pdf_path, kb_root=kb_root: (
                                _append_build(f"\n---- [{bi}/{tb}] 开始：{pdf_path.name} -> {kb_root}\n"),
                                build_status_var.set(f"book {bi}/{tb}: start"),
                            ),
                        )

                        summary = build_kb_from_pdf_paths(
                            pdf_paths=[pdf_path],
                            kb_root=kb_root,
                            model_name=model_s,
                            chunk_chars=chunk_chars,
                            overlap=overlap,
                            embed_batch_size=batch,
                            kb_display_name=display_name,
                            on_progress=lambda st, d, t, m, bi=bi, tb=total_books: on_progress(
                                st,
                                d,
                                t,
                                f"[{bi}/{tb}] {m}",
                            ),
                        )
                        summaries.append(summary)
                        root.after(
                            0,
                            lambda bi=bi, tb=total_books, kb_root=kb_root: _append_build(
                                f"---- [{bi}/{tb}] 完成：{kb_root}\n"
                            ),
                        )
                except Exception as e:  # noqa: BLE001
                    err = str(e)

                def done() -> None:
                    _set_build_busy(False)
                    if err:
                        build_status_var.set("失败")
                        _append_build(f"[错误] {err}\n")
                        messagebox.showerror("构建失败", err)
                        return
                    last_kb_root[0] = kb_root
                    build_status_var.set("完成")
                    btn_open_kb["state"] = tk.NORMAL
                    _append_build(json.dumps({"books": summaries}, ensure_ascii=False, indent=2) + "\n")
                    # 优先展示原始书名（若有）
                    show_name = None
                    try:
                        if kb_root is not None:
                            mf = (Path(kb_root) / "kb_manifest.json").read_text(encoding="utf-8")
                            d = json.loads(mf)
                            if isinstance(d, dict) and d.get("kb_display_name"):
                                show_name = str(d.get("kb_display_name"))
                    except Exception:
                        show_name = None
                    tail = (f"{show_name} -> {kb_root}" if show_name else f"{kb_root}")
                    messagebox.showinfo("构建完成", f"已生成 {len(summaries)} 个知识库。\n最后一个：{tail}")

                root.after(0, done)

            threading.Thread(target=work, daemon=True).start()

        btn_build["command"] = on_build

        # --- 查询 tab
        q_kb_var = tk.StringVar(value="")
        q_model_var = tk.StringVar(value="")  # 可选覆盖 manifest 里的模型
        q_text_var = tk.StringVar(value="")
        q_topk_var = tk.StringVar(value="5")
        q_status_var = tk.StringVar(value="空闲")
        q_busy: list[bool] = [False]

        fr_q_in = ttk.LabelFrame(tab_query, text="查询", padding=8)
        fr_q_in.pack(fill=tk.X, pady=(0, 10))

        qr1 = _row(fr_q_in)
        ttk.Label(qr1, text="KB：", width=10).pack(side=tk.LEFT)
        ttk.Entry(qr1, textvariable=q_kb_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(qr1, text="topk：", width=8).pack(side=tk.LEFT, padx=(10, 0))
        ttk.Entry(qr1, textvariable=q_topk_var, width=6).pack(side=tk.LEFT)

        qr2 = _row(fr_q_in)
        ttk.Label(qr2, text="模型(可选)：", width=10).pack(side=tk.LEFT)
        ttk.Entry(qr2, textvariable=q_model_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(qr2, text="（空=用 KB manifest）", foreground="#666").pack(side=tk.LEFT, padx=(8, 0))

        qr3 = _row(fr_q_in)
        ttk.Label(qr3, text="问题：", width=10).pack(side=tk.LEFT)
        ent_q = ttk.Entry(qr3, textvariable=q_text_var)
        ent_q.pack(side=tk.LEFT, fill=tk.X, expand=True)

        fr_q_ops = ttk.Frame(tab_query)
        fr_q_ops.pack(fill=tk.X, pady=(0, 6))
        btn_search = ttk.Button(fr_q_ops, text="检索 TopK", width=14)
        btn_search.pack(side=tk.LEFT)
        ttk.Label(fr_q_ops, textvariable=q_status_var, foreground="#666").pack(side=tk.LEFT, padx=(12, 0))

        txt_q = scrolledtext.ScrolledText(tab_query, height=18, state=tk.DISABLED, font=_font_mono or font_)
        txt_q.pack(fill=tk.BOTH, expand=True)

        def _append_q(msg: str) -> None:
            txt_q.config(state=tk.NORMAL)
            txt_q.insert(tk.END, msg)
            if not msg.endswith("\n"):
                txt_q.insert(tk.END, "\n")
            txt_q.see(tk.END)
            txt_q.config(state=tk.DISABLED)

        def _set_q_busy(b: bool) -> None:
            q_busy[0] = b
            btn_search["state"] = (tk.DISABLED if b else tk.NORMAL)
            ent_q["state"] = (tk.DISABLED if b else tk.NORMAL)

        def on_search() -> None:
            if q_busy[0]:
                return
            kb_s = (q_kb_var.get() or "").strip()
            q = (q_text_var.get() or "").strip()
            if not q:
                messagebox.showwarning("提示", "请输入问题。")
                return
            try:
                topk = int(q_topk_var.get() or "5")
            except ValueError:
                messagebox.showwarning("提示", "topk 必须是整数。")
                return
            model_override = (q_model_var.get() or "").strip() or None

            _set_q_busy(True)
            q_status_var.set("检索中…")
            txt_q.config(state=tk.NORMAL)
            txt_q.delete("1.0", tk.END)
            txt_q.config(state=tk.DISABLED)
            _append_q(f"KB: {kb_s}\nQ: {q}\nTopK: {topk}\n")

            def work() -> None:
                err: str | None = None
                hits: list[Any] | None = None
                kb_root = None
                kb_used: list[str] | None = None
                try:
                    if kb_s:
                        kb_root = kb_dir_from_arg(kb_s)
                        store = load_kb(kb_root=kb_root, model_name=model_override)
                        hits = store.query(q, topk=topk)
                        kb_used = [str(kb_root)]
                    else:
                        # KB 为空：扫描 data/kb 下所有 KB，全库检索并取全局 TopK。
                        from mm_qbank.kb.embed import Embedder
                        from mm_qbank.kb.index_faiss import load_chunks_jsonl, load_faiss_index, load_kb_manifest, search

                        kb_parent = (project_root() / "data" / "kb").resolve()
                        roots: list[Path] = []
                        if kb_parent.is_dir():
                            for p in kb_parent.iterdir():
                                if not p.is_dir():
                                    continue
                                if (
                                    (p / "kb_manifest.json").is_file()
                                    and (p / "chunks.jsonl").is_file()
                                    and (p / "index.faiss").is_file()
                                ):
                                    roots.append(p.resolve())
                        if not roots:
                            raise FileNotFoundError(f"未找到任何 KB：{kb_parent}")

                        # 读取每个 KB 的 model_name；不同 embedding 模型的 score 不可直接比
                        model_groups: dict[str, list[Path]] = {}
                        kb_display: dict[str, str] = {}
                        for r in roots:
                            mf = load_kb_manifest((r / "kb_manifest.json").resolve())
                            mname = str(mf.get("model_name") or "").strip()
                            if not mname:
                                continue
                            model_groups.setdefault(mname, []).append(r)
                            disp = str(mf.get("kb_display_name") or "").strip() or r.name
                            kb_display[str(r)] = disp
                        if not model_groups:
                            raise ValueError(f"KB manifest 缺少 model_name：{kb_parent}")

                        primary_model = sorted(model_groups.items(), key=lambda kv: len(kv[1]), reverse=True)[0][0]
                        emb = Embedder(primary_model).embed_texts([q], batch_size=1)[0]

                        all_hits: list[Any] = []
                        for r in model_groups.get(primary_model, []):
                            idx = load_faiss_index((r / "index.faiss").resolve())
                            chunks = load_chunks_jsonl((r / "chunks.jsonl").resolve())
                            hs = search(index=idx, chunks=chunks, query_embedding=emb, topk=max(1, int(topk)))
                            disp = kb_display.get(str(r), r.name)
                            for h in hs:
                                # 复用 KBSearchHit 结构，让下方渲染逻辑保持一致：给 chunk 增加 book 字段不方便，
                                # 这里用一个轻量 dict 包装。
                                all_hits.append({"hit": h, "kb_root": r, "kb_display": disp})
                        all_hits.sort(key=lambda x: float(x["hit"].score), reverse=True)
                        hits = all_hits[: max(1, int(topk))]
                        kb_used = [str(p) for p in model_groups.get(primary_model, [])]
                except Exception as e:  # noqa: BLE001
                    err = str(e)

                def done() -> None:
                    _set_q_busy(False)
                    if err:
                        q_status_var.set("失败")
                        _append_q(f"\n[错误] {err}\n")
                        return
                    q_status_var.set("完成")
                    if kb_s:
                        _append_q(f"\nKB 目录: {kb_root}\n命中: {len(hits or [])}\n")
                    else:
                        _append_q(f"\nKB: （全库扫描 data/kb）\n参与 KB: {len(kb_used or [])}\n命中: {len(hits or [])}\n")
                    for i, h in enumerate(hits or [], start=1):
                        if isinstance(h, dict) and h.get("hit") is not None:
                            hh = h["hit"]
                            c = hh.chunk
                            src = f"{h.get('kb_display','')} | {c.pdf_name} | p.{c.page_index+1} | score={hh.score:.4f}"
                            _append_q(f"\n[{i}] {src}  id={c.id}\n{c.text}\n")
                        else:
                            c = h.chunk
                            _append_q(
                                (
                                    f"\n[{i}] score={h.score:.4f}  pdf={c.pdf_name}  page={c.page_index+1}  id={c.id}\n"
                                    f"{c.text}\n"
                                )
                            )

                root.after(0, done)

            threading.Thread(target=work, daemon=True).start()

        btn_search["command"] = on_search
        win.bind("<Return>", lambda _e: (on_search() if nb.index(nb.select()) == 1 else on_build()))

    # 不得放在主窗口最底部 + pack 在「可扩展的日志区」之后：在 Windows 上常会把底栏顶到视口外。
    # 用「转写控制」带标题框，插在「输出位置」和「进度」之间，保证始终可见。
    fr_ops = ttk.LabelFrame(content, text="转写控制", padding=6)
    fr_ops.pack(fill=tk.X, padx=8, pady=4, before=fr_pb)
    fr_ops_in = ttk.Frame(fr_ops)
    fr_ops_in.pack(fill=tk.X)
    btn_s = ttk.Button(fr_ops_in, text="开始", width=10, takefocus=1)
    btn_s.pack(side=tk.LEFT, padx=(0, 6))
    btn_pause = ttk.Button(fr_ops_in, text="暂停", width=10, state=tk.DISABLED, takefocus=1, command=on_toggle_pause)
    btn_pause.pack(side=tk.LEFT, padx=6)
    btn_end = ttk.Button(fr_ops_in, text="结束", width=10, state=tk.DISABLED, takefocus=1, command=on_end)
    btn_end.pack(side=tk.LEFT, padx=6)
    ttk.Separator(fr_ops_in, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6, pady=2)
    ttk.Label(
        fr_ops_in,
        text="VLM 整页转写 · 与模型单页请求进行中时须等其返回  ",
    ).pack(side=tk.LEFT)
    ttk.Button(fr_ops_in, text="知识库…", width=10, command=open_kb_dialog).pack(side=tk.RIGHT)
    btn_s["command"] = on_run

    # --- 教材向修正参数（与是否联网脱钩）
    fr_refine_flags = ttk.Frame(fr_ops)
    fr_refine_flags.pack(fill=tk.X, pady=(6, 0))
    ttk.Label(fr_refine_flags, text="修正参数：").pack(side=tk.LEFT)

    # 读取默认配置作为初始值（GUI 允许覆盖；未覆盖时仍是默认行为）
    _cfg0: dict[str, Any] = {}
    try:
        _cfg0 = load_config(None)
    except Exception:
        _cfg0 = {}
    _rcfg0 = dict((_cfg0.get("refine") or {}) if isinstance(_cfg0, dict) else {})
    _stream0 = bool(_rcfg0.get("stream_refine", False))
    _use_kb0 = bool(_rcfg0.get("use_kb", True))
    _web0 = bool(_rcfg0.get("web_search", False))
    stream_refine_var = tk.BooleanVar(value=_stream0)
    use_kb_var = tk.BooleanVar(value=_use_kb0)
    web_search_var = tk.BooleanVar(value=_web0)

    ttk.Checkbutton(fr_refine_flags, text="凑题缓存→流式修正", variable=stream_refine_var).pack(side=tk.LEFT, padx=(6, 0))
    ttk.Checkbutton(fr_refine_flags, text="使用离线KB", variable=use_kb_var).pack(side=tk.LEFT, padx=(6, 0))
    ttk.Checkbutton(fr_refine_flags, text="联网检索", variable=web_search_var).pack(side=tk.LEFT, padx=(12, 0))

    def _write_runtime_config(out_dir: Path) -> Path | None:
        """
        将 GUI 上的 refine 开关落到一个运行时 YAML，并把路径传给 refine 流程。
        仅覆盖 refine.use_kb / refine.web_search；其余沿用默认配置。
        """
        try:
            cfg = load_config(None)
        except Exception:
            cfg = {}
        if not isinstance(cfg, dict):
            cfg = {}
        rcfg = dict(cfg.get("refine") or {})
        rcfg["stream_refine"] = bool(stream_refine_var.get())
        rcfg["use_kb"] = bool(use_kb_var.get())
        rcfg["web_search"] = bool(web_search_var.get())
        cfg["refine"] = rcfg
        p = (out_dir / "_runtime_config.yaml").resolve()
        p.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return p

    root.mainloop()
    _LOG_QUEUE = None


if __name__ == "__main__":
    main()
