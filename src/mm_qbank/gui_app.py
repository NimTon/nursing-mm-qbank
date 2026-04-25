"""
图形界面：VLM 整页转写 → LLM 拆题 → **教材向修正并导出 xlsx**（vlm-refine）；按页数显示总进度（0–50% VLM、50–100% LLM 拆题，xlsx 在拆题后同批生成），
输出到带时间戳目录，并显示日志。
提供「开始 / 暂停·继续 / 结束」；在相邻两页间生效，当前页与模型正在通信时须等其返回。

  mm-qbank-gui
或  python -m mm_qbank.gui_app
"""

from __future__ import annotations

import logging
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

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
from PIL import Image, ImageTk

from mm_qbank import __version__
from mm_qbank.config import project_root
from mm_qbank.logging_utils import configure_logging
from mm_qbank.pipeline.llm_compose_run import run_llm_compose_manifest
from mm_qbank.pipeline.refine_vlm_run import run_refine_vlm_merged
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


def main() -> None:
    global _LOG_QUEUE  # noqa: PLW0603

    if tk is None:
        print("当前解释器未带 tkinter，请使用官方 Python（含 Tcl/Tk）。", file=sys.stderr)  # noqa: T201
        raise SystemExit(1) from _tk_import_error

    load_dotenv(project_root() / ".env")

    _LOG_QUEUE = queue.Queue()
    # 必须先配置根 logger（会清空已有 handlers），再挂 GUI 的队列 handler；
    # 若先 addHandler 再 configure_logging，后者会 clear 掉 _TextQueueHandler，导致界面收不到与终端同等的日志。
    configure_logging(verbose=True, quiet=False)
    th = _TextQueueHandler()
    th.setLevel(logging.DEBUG)
    th.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    logging.getLogger().addHandler(th)
    for name in ("httpx", "httpcore", "openai", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in ("mm_qbank",):
        logging.getLogger(name).setLevel(logging.DEBUG)

    root = tk.Tk()
    root.title(f"nursing-mm-qbank v{__version__} · VLM 转写 + LLM 拆题")
    root.geometry("820x620")
    root.minsize(720, 520)

    # --- 主界面滚动容器（窗口高度不够时可滚动查看全部区域）
    outer = ttk.Frame(root)
    outer.pack(fill=tk.BOTH, expand=True)
    vbar = ttk.Scrollbar(outer, orient=tk.VERTICAL)
    vbar.pack(side=tk.RIGHT, fill=tk.Y)
    canvas = tk.Canvas(outer, yscrollcommand=vbar.set, highlightthickness=0)
    canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    vbar.config(command=canvas.yview)

    content = ttk.Frame(canvas)
    win_id = canvas.create_window((0, 0), window=content, anchor="nw")

    def _sync_canvas_width(_: Any) -> None:
        # 让内容宽度随窗口变化，避免横向滚动
        canvas.itemconfigure(win_id, width=canvas.winfo_width())

    def _on_content_configure(_: Any) -> None:
        canvas.configure(scrollregion=canvas.bbox("all"))

    canvas.bind("<Configure>", _sync_canvas_width)
    content.bind("<Configure>", _on_content_configure)

    def _on_mousewheel(event: Any) -> str:
        # Windows: event.delta=120/-120；macOS 不同，但不影响基本可用
        delta = 0
        if hasattr(event, "delta") and event.delta:
            delta = int(-1 * (event.delta / 120))
        elif hasattr(event, "num") and event.num in (4, 5):  # X11
            delta = -1 if event.num == 4 else 1
        if delta:
            canvas.yview_scroll(delta, "units")
        return "break"

    # 鼠标进入内容区域时启用滚轮；离开时释放（避免影响别的窗口控件）
    def _bind_wheel(_: Any) -> None:
        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        canvas.bind_all("<Button-4>", _on_mousewheel)
        canvas.bind_all("<Button-5>", _on_mousewheel)

    def _unbind_wheel(_: Any) -> None:
        canvas.unbind_all("<MouseWheel>")
        canvas.unbind_all("<Button-4>")
        canvas.unbind_all("<Button-5>")

    content.bind("<Enter>", _bind_wheel)
    content.bind("<Leave>", _unbind_wheel)

    work_dir: list[Path | None] = [None]
    is_temp: list[bool] = [False]
    last_out: list[Path | None] = [None]
    # 运行/暂停/取消：与 vlm_text_run 内「页与页之间」协作用；单次模型请求中无法暂停或结束直到该页返回
    run_cancel = threading.Event()
    user_paused: list[bool] = [False]
    is_running: list[bool] = [False]

    # --- 顶部：输入（列表 + 缩略图与「选文件夹时」/「多选」时一致展示）
    list_font = ("Consolas", 9) if os.name == "nt" else ("monospace", 9)
    thumb_photos: list[Any] = []  # 持有 ImageTk 引用，避免被 GC

    fr_top = ttk.LabelFrame(content, text="图片输入", padding=8)
    fr_top.pack(fill=tk.X, padx=8, pady=4)
    fr_top_bar = ttk.Frame(fr_top)
    fr_top_bar.pack(fill=tk.X)
    ttk.Label(fr_top_bar, text=f"v{__version__}", foreground="#666", font=("", 9)).pack(
        side=tk.RIGHT, padx=(4, 0)
    )
    btn_dir = ttk.Button(fr_top_bar, text="选择图片文件夹…")
    btn_dir.pack(side=tk.LEFT, padx=(0, 8))
    btn_files = ttk.Button(fr_top_bar, text="选择图片（可多选）…")
    btn_files.pack(side=tk.LEFT)
    lbl_in = ttk.Label(fr_top, text="未选择", foreground="#666")
    lbl_in.pack(anchor=tk.W, pady=(4, 0))

    fr_list = ttk.LabelFrame(fr_top, text="将处理的图片（含子目录，与转写顺序一致）", padding=4)
    fr_list.pack(fill=tk.X, pady=(4, 0))
    lbf = ttk.Frame(fr_list)
    lbf.pack(fill=tk.X)
    yscroll = ttk.Scrollbar(lbf)
    yscroll.pack(side=tk.RIGHT, fill=tk.Y)
    lb = tk.Listbox(
        lbf, height=5, font=list_font, yscrollcommand=yscroll.set, selectmode=tk.EXTENDED
    )
    lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    yscroll["command"] = lb.yview

    fr_thumbs = ttk.LabelFrame(fr_top, text="缩略图预览", padding=4)
    fr_thumbs.pack(fill=tk.X, pady=(4, 0))
    thumb_inner = ttk.Frame(fr_thumbs)
    thumb_inner.pack()
    lbl_thumb_note = ttk.Label(
        fr_top,
        text="选择含图的文件夹或图片后，将列出全部文件，并显示部分缩略图。",
        foreground="#666",
        font=("", 8),
        wraplength=780,
    )
    lbl_thumb_note.pack(anchor=tk.W, pady=(2, 0))

    def _rel_path_line(base: Path, fpath: Path) -> str:
        try:
            return fpath.resolve().relative_to(base.resolve()).as_posix()
        except ValueError:
            return fpath.name

    def _clear_input_preview() -> None:
        lb.delete(0, tk.END)
        for w in thumb_inner.winfo_children():
            w.destroy()
        thumb_photos.clear()
        lbl_thumb_note.config(
            text="选择含图的文件夹或图片后，将列出全部文件，并显示部分缩略图。",
            foreground="#666",
        )

    def _thumb_pil_to_photo(p: Path) -> ImageTk.PhotoImage:
        im = Image.open(p)
        if im.mode in ("P",) and "transparency" in im.info:
            im = im.convert("RGBA")
        elif im.mode not in ("RGB", "RGBA", "L"):
            im = im.convert("RGB")
        im.thumbnail((100, 100), Image.Resampling.LANCZOS)
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGB")
        return ImageTk.PhotoImage(im)

    def _show_input_preview(base: Path, paths: list[Path]) -> None:
        _clear_input_preview()
        if not paths:
            lbl_thumb_note.config(text="该路径下无支持的图片格式。", foreground="#a60")
            return
        for fpath in paths:
            lb.insert(tk.END, _rel_path_line(base, fpath))
        max_t, ncols = 10, 5
        for i, p in enumerate(paths[:max_t]):
            r, c = divmod(i, ncols)
            try:
                ph = _thumb_pil_to_photo(p)
                thumb_photos.append(ph)
                cell = ttk.Frame(thumb_inner, padding=1)
                cell.grid(row=r, column=c, padx=2, pady=2, sticky=tk.N)
                il = ttk.Label(cell, image=ph, relief=tk.SOLID, borderwidth=1, padding=0)
                il.image = ph  # type: ignore[attr-defined]
                il.pack()
                nm = p.name if len(p.name) <= 20 else f"{p.stem[:12]}…{p.suffix}"
                ttk.Label(cell, text=nm, font=("", 7), width=16, anchor=tk.N, justify=tk.CENTER).pack()
            except OSError:
                cell = ttk.Frame(thumb_inner, padding=1)
                cell.grid(row=r, column=c, padx=2, pady=2, sticky=tk.N)
                ttk.Label(cell, text=f"{p.name}\n(无法打开)", font=("", 7), foreground="red", width=12).pack()
        n = len(paths)
        if n > max_t:
            lbl_thumb_note.config(
                text=f"共 {n} 张；上表为全部，下方为前 {max_t} 张缩略图。",
                foreground="#444",
            )
        else:
            lbl_thumb_note.config(text=f"共 {n} 张（全部已预览）。")

    def on_pick_dir() -> None:
        p = filedialog.askdirectory(title="选择含图片的文件夹", mustexist=True)
        if not p:
            return
        d = Path(p)
        img_paths = _list_images(d)
        work_dir[0] = d
        is_temp[0] = False
        nic = len(img_paths)
        lbl_in.config(text=f"文件夹: {d}  （共 {nic} 张图）", foreground="black")
        _show_input_preview(d, img_paths)
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
        _show_input_preview(tmp, img_paths)
        if not img_paths:
            messagebox.showwarning("提示", "未复制到有效图片，请重选。")

    btn_dir["command"] = on_pick_dir
    btn_files["command"] = on_pick_files

    # 输出说明
    fr_out = ttk.LabelFrame(content, text="输出位置", padding=6)
    fr_out.pack(fill=tk.X, padx=8, pady=4)
    ex = (project_root() / "data" / "out" / "vlm_gui_YYYYMMDD_hhmmss").as_posix()
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
    font_ = ("Consolas", 9) if os.name == "nt" else ("monospace", 9)
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
        if not wd or not wd.is_dir():
            messagebox.showwarning("提示", "请先通过「选文件夹」或「选多图」添加输入。")
            return
        n = len(_list_images(wd))
        if n < 1:
            messagebox.showwarning("提示", "当前输入路径下没有可处理的图片。")
            return
        was_temp = is_temp[0]
        run_cancel.clear()
        user_paused[0] = False
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = project_root() / "data" / "out" / f"vlm_gui_{ts}"
        out.mkdir(parents=True, exist_ok=True)
        last_out[0] = out
        lbl_path.config(text=f"本次输出: {out.resolve()}", foreground="black")

        st_lbl.config(
            text=(
                f"待处理 {n} 张 · 0%（0–50% 多图并行 VLM，50–100% 多页并行拆题；"
                f"拆题后教材修正；单页/单次请求不暂停/结束，页与页之间可）"
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
            v = int(500 * i / t)

            def u() -> None:
                pb["value"] = min(500, v)
                p = 50.0 * i / t
                st_lbl.config(
                    text=f"VLM 多模态 {i}/{t} · 总 {p:.1f}%（前半为 VLM，后半为 LLM 拆题）"
                )

            root.after(0, u)

        def on_llm_page(j: int, t: int) -> None:
            if t <= 0:
                return
            v = 500 + int(500 * j / t)

            def u() -> None:
                pb["value"] = min(1000, v)
                p = 50.0 + 50.0 * j / t
                st_lbl.config(
                    text=f"LLM 拆题 {j}/{t} · 总 {p:.1f}%（前半为 VLM，后半为 LLM 拆题）"
                )

            root.after(0, u)

        def work() -> None:
            err: str | None = None
            vlm: dict[str, Any] = {}
            try:
                vlm = run_vlm_text_only(
                    input_dir=wd,  # type: ignore[arg-type]
                    out_dir=out,
                    config_path=None,
                    model=None,
                    cancel_event=run_cancel,
                    paused=paused_fn,
                    on_page_done=on_vlm_page,
                )
            except Exception as e:  # noqa: BLE001
                err = str(e)
                vlm = {}

            sm: dict[str, Any] = {}
            if err is None and vlm and not vlm.get("cancelled", False):
                sm = {**vlm, "llm_compose": None, "refine": None, "compose_error": None}
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
                    try:
                        root.after(
                            0,
                            lambda: st_lbl.config(
                                text="教材向修正并导出 xlsx（每页一次 LLM，可能较慢）…"
                            ),
                        )
                        sm["refine"] = run_refine_vlm_merged(
                            manifest_path=mpath,
                            out_jsonl=out / "refined_merged.jsonl",
                            out_xlsx=out / "refined_merged.xlsx",
                            config_path=None,
                            model=None,
                        )
                    except Exception as e3:  # noqa: BLE001
                        sm["refine"] = {"error": str(e3)}
            elif vlm:
                sm = {**vlm, "llm_compose": None, "refine": None}
            if not sm and vlm:
                sm = {**vlm, "llm_compose": None, "refine": None}

            try:
                if was_temp and wd.is_dir():
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
    btn_s["command"] = on_run

    root.mainloop()
    _LOG_QUEUE = None


if __name__ == "__main__":
    main()
