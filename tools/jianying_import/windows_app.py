"""Windows GUI and headless verification entry point for the frozen importer."""
import argparse
import json
from pathlib import Path
import queue
import sys
import threading

from import_draft import import_package


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path)
    parser.add_argument("--draft-dir", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--gui-smoke-report", type=Path)
    args = parser.parse_args()
    if args.gui_smoke_report:
        # Exercise the real Tk runtime in the frozen build, not just headless
        # media conversion. No visible window or file dialogs are opened.
        try:
            import tkinter as tk
            from tkinter import ttk, filedialog, messagebox
            root = tk.Tk()
            root.withdraw()
            frame = ttk.Frame(root)
            ttk.Entry(frame).pack()
            root.update()
            root.destroy()
            result = {"ok": True, "tk_version": tk.TkVersion}
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        args.gui_smoke_report.write_text(json.dumps(result), encoding="utf-8")
        return 0 if result["ok"] else 1
    if args.report:
        try:
            result = import_package(args.package, args.draft_dir)
            report = {"ok": True, "draft": str(result.path)}
        except Exception as exc:
            report = {"ok": False, "error": str(exc)}
        args.report.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        return 0 if report["ok"] else 1

    import os
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    root = tk.Tk()
    root.title("灵珠剪映导入工具")
    root.geometry("650x300")
    package = tk.StringVar()
    drafts = tk.StringVar(value=str(Path.home() / "AppData/Local/JianyingPro/User Data/Projects/com.lveditor.draft"))
    status = tk.StringVar(value="选择网页下载的素材包，再确认剪映草稿目录。")
    frame = ttk.Frame(root, padding=20)
    frame.pack(fill="both", expand=True)
    for row, label, variable, chooser in (
        (0, "素材包 ZIP", package, lambda: filedialog.askopenfilename(filetypes=[("素材包", "*.zip")], parent=root)),
        (2, "剪映草稿目录", drafts, lambda: filedialog.askdirectory(initialdir=drafts.get(), parent=root)),
    ):
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w")
        ttk.Entry(frame, textvariable=variable, width=65).grid(row=row+1, column=0, sticky="ew", pady=(4, 12))
        def choose(v=variable, c=chooser):
            value = c()
            if value:
                v.set(value)
        ttk.Button(frame, text="选择…", command=choose).grid(row=row+1, column=1, padx=8)
    ttk.Label(frame, textvariable=status, wraplength=590).grid(row=4, column=0, columnspan=2, sticky="w")
    progress = ttk.Progressbar(frame, mode="indeterminate")
    progress.grid(row=5, column=0, columnspan=2, sticky="ew", pady=10)
    results = queue.Queue()
    def start():
        source, target = Path(package.get()), Path(drafts.get())
        if not source.is_file() or not drafts.get().strip():
            messagebox.showerror("请选择文件", "请选择有效的素材包和剪映草稿目录。", parent=root)
            return
        button.configure(state="disabled")
        progress.start()
        status.set("正在校验素材包并生成草稿，请稍候…")
        def work():
            try:
                results.put((True, import_package(source, target).path))
            except Exception as exc:
                results.put((False, str(exc)))
        threading.Thread(target=work, daemon=True).start()
        root.after(100, poll)
    def poll():
        try:
            ok, value = results.get_nowait()
        except queue.Empty:
            root.after(100, poll)
            return
        progress.stop()
        button.configure(state="normal")
        if ok:
            status.set(f"已生成：{value}")
            messagebox.showinfo("导入完成", "草稿已创建。请打开或重启剪映，在草稿列表中检查。", parent=root)
            os.startfile(str(value.parent))
        else:
            status.set("导入失败，可修正后重试。")
            messagebox.showerror("导入失败", value, parent=root)
    button = ttk.Button(frame, text="生成剪映草稿", command=start)
    button.grid(row=6, column=0, sticky="w")
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
