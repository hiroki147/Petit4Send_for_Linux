#!/usr/bin/env python3
"""Petit4Send for Linux — GUI(Tkinter) と CLI。

  python3 p4send.py                      GUI
  python3 p4send.py ports                シリアルポート一覧
  python3 p4send.py send FILE -p /dev/ttyUSB0 [-n NAME] [-k txt|dat|grp] [-c auto|lzss|none] [-s -1..24]
  python3 p4send.py detect -p /dev/ttyUSB0
  python3 p4send.py restore page-1.png page-2.png -o OUTDIR
"""
import argparse
import json
import os
import queue
import sys
import threading

import p4core as c

CONFIG = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "petit4send.json")
KIND_BY_ARG = {"txt": c.KIND_TEXT, "dat": c.KIND_DATA, "grp": c.KIND_GRAPHICS}
COMP_BY_ARG = {"auto": c.COMPRESSION_AUTO, "lzss": c.COMPRESSION_LZSS, "none": c.COMPRESSION_NONE}


def prepare(path, kind, name, compression, sync_key, should_cancel=lambda: False):
    """ファイルを種別に合わせてバイト列にし、(stream, reports)を返す。"""
    if os.path.getsize(path) > c.MAXIMUM_SIZE:
        raise c.TransferError("入力ファイルが64 MiBを超えています。")
    width = height = 0
    if kind == c.KIND_TEXT:
        with open(path, "rb") as f:
            data = c.text_to_utf16le(f.read())
    elif kind == c.KIND_DATA:
        with open(path, "rb") as f:
            data = f.read()
    else:
        img = c.Raster.load(path)
        data, width, height = img.bgra, img.width, img.height
        if width > 65535 or height > 65535:
            raise c.TransferError("画像サイズが大きすぎます。")
    if not data:
        raise c.TransferError("空ファイルはSwitch側の受信処理に対応していません。")
    stream = c.build_stream(data, name, kind, compression, width, height, should_cancel)
    return data, stream, c.HIDReports(stream, sync_key)


# ------------------------------------------------------------------ CLI
def cli(argv):
    ap = argparse.ArgumentParser(description="Petit4Send for Linux")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("ports")
    s = sub.add_parser("send")
    s.add_argument("file"); s.add_argument("-p", "--port", required=True)
    s.add_argument("-n", "--name"); s.add_argument("-k", "--kind", choices=KIND_BY_ARG)
    s.add_argument("-c", "--compression", choices=COMP_BY_ARG, default="auto")
    s.add_argument("-s", "--sync-key", type=int, default=-1)
    d = sub.add_parser("detect"); d.add_argument("-p", "--port", required=True)
    r = sub.add_parser("restore"); r.add_argument("images", nargs="+"); r.add_argument("-o", "--out", default=".")
    a = ap.parse_args(argv)
    if a.cmd is None:
        return gui()
    try:
        if a.cmd == "ports":
            print("\n".join(c.available_ports()))
        elif a.cmd == "send":
            ext = os.path.splitext(a.file)[1].lstrip(".")
            kind = KIND_BY_ARG[a.kind] if a.kind else (c.infer_kind(ext) or c.KIND_DATA)
            name = a.name or c.ascii_upper(os.path.basename(a.file))[:32]
            data, stream, reports = prepare(a.file, kind, name, COMP_BY_ARG[a.compression], a.sync_key)
            total = int(len(stream) / 296)
            print(f"送信: {len(data)} バイト → 本体 {c.integer(stream, 112, 4)} バイト (約{total}秒)  Switchを USB RECEIVE の WAITING FILE... にしてください")
            tok = c.Cancellation()
            c.send(a.port, reports, tok, lambda v: print(f"\r{v*100:5.1f}%", end="", file=sys.stderr))
            print("\n送信終了。Switch側でCRC結果を確認し、Aボタンで保存してください")
        elif a.cmd == "detect":
            print("Switchで DETECT SYNC KEY → WAITING SYNC KEY... にしておいてください。約4秒の信号を送ります。")
            c.detect_sync_key(a.port, c.Cancellation())
            print("終了。Switchに表示された SYNC KEY の値を -s に指定してください。")
        elif a.cmd == "restore":
            pages = [c.ScreenshotPage.load(p) for p in a.images]
            for key, group in c.group_pages(pages).items():
                print(c.export(group, a.out))
    except c.TransferError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n中止しました。Switch側もBボタンで受信を終了してください。", file=sys.stderr)
        return 130
    return 0


# ------------------------------------------------------------------ GUI
def gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    root = tk.Tk()
    root.title("Petit4Send for Linux")
    root.minsize(720, 560)
    cfg = {}
    try:
        with open(CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        pass

    def save_cfg():
        try:
            os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
            with open(CONFIG, "w", encoding="utf-8") as f:
                json.dump(cfg, f)
        except OSError:
            pass

    st = dict(file="", ports=c.available_ports(), token=None, busy=False, closing=False)
    v_file = tk.StringVar(); v_name = tk.StringVar(); v_kind = tk.StringVar(value=c.KIND_TEXT)
    v_comp = tk.StringVar(value=c.COMPRESSION_AUTO); v_port = tk.StringVar(); v_sync = tk.IntVar(value=-1)
    v_status = tk.StringVar(value="ファイルとシリアルポートを選択してください"); v_prog = tk.DoubleVar()
    v_remain = tk.StringVar()
    ui = queue.Queue()  # ワーカースレッド → UI

    nb = ttk.Notebook(root); nb.pack(fill="both", expand=True, padx=8, pady=8)
    tab_send = ttk.Frame(nb, padding=10); tab_img = ttk.Frame(nb, padding=10)
    nb.add(tab_send, text="USB送信 (Linux → Switch)"); nb.add(tab_img, text="画像から復元 (Switch → Linux)")

    # ---- 送信タブ
    f = tab_send; f.columnconfigure(1, weight=1)
    ttk.Label(f, text="ファイル").grid(row=0, column=0, sticky="w", pady=3)
    ttk.Entry(f, textvariable=v_file, state="readonly").grid(row=0, column=1, sticky="ew", padx=4)
    b_choose = ttk.Button(f, text="選択…"); b_choose.grid(row=0, column=2)
    ttk.Label(f, text="Switch側の名前").grid(row=1, column=0, sticky="w", pady=3)
    ttk.Entry(f, textvariable=v_name).grid(row=1, column=1, sticky="ew", padx=4)
    lbl_name = ttk.Label(f, text=c.SWITCH_NAME_RULE); lbl_name.grid(row=2, column=1, sticky="w", padx=4)
    ttk.Label(f, text="種類").grid(row=3, column=0, sticky="w", pady=3)
    cb_kind = ttk.Combobox(f, textvariable=v_kind, values=c.KINDS, state="readonly", width=8); cb_kind.grid(row=3, column=1, sticky="w", padx=4)
    ttk.Label(f, text="圧縮").grid(row=4, column=0, sticky="w", pady=3)
    cb_comp = ttk.Combobox(f, textvariable=v_comp, values=c.COMPRESSIONS, state="readonly", width=8); cb_comp.grid(row=4, column=1, sticky="w", padx=4)
    ttk.Label(f, text="シリアルポート").grid(row=5, column=0, sticky="w", pady=3)
    cb_port = ttk.Combobox(f, textvariable=v_port, values=st["ports"]); cb_port.grid(row=5, column=1, sticky="ew", padx=4)
    b_refresh = ttk.Button(f, text="更新"); b_refresh.grid(row=5, column=2)
    ttk.Label(f, text="Sync Key (-1=自動)").grid(row=6, column=0, sticky="w", pady=3)
    sp = ttk.Spinbox(f, from_=-1, to=24, textvariable=v_sync, width=5); sp.grid(row=6, column=1, sticky="w", padx=4)
    b_detect = ttk.Button(f, text="Detect Sync Key"); b_detect.grid(row=6, column=2)
    ttk.Progressbar(f, variable=v_prog, maximum=1.0).grid(row=7, column=0, columnspan=3, sticky="ew", pady=(14, 2))
    ttk.Label(f, textvariable=v_remain).grid(row=8, column=0, columnspan=3, sticky="w")
    ttk.Label(f, textvariable=v_status, wraplength=660, justify="left").grid(row=9, column=0, columnspan=3, sticky="w", pady=4)
    bar = ttk.Frame(f); bar.grid(row=10, column=0, columnspan=3, sticky="w", pady=6)
    b_send = ttk.Button(bar, text="Switchへ送信"); b_send.pack(side="left")
    b_stop = ttk.Button(bar, text="中止", state="disabled"); b_stop.pack(side="left", padx=8)

    def sync_value():
        try:
            return max(-1, min(24, int(sp.get())))
        except ValueError:
            return -1

    def refresh_state(*_):
        err = c.switch_name_error(v_name.get())
        lbl_name.config(text=err or c.SWITCH_NAME_RULE, foreground="#c60" if err else "")
        ok = st["file"] and v_port.get() and not err and not st["busy"]
        b_send.config(state="normal" if ok else "disabled")
        b_detect.config(state="normal" if v_port.get() and not st["busy"] else "disabled")
        b_stop.config(state="normal" if st["busy"] else "disabled")
        for w in (b_choose, b_refresh, cb_kind, cb_comp, cb_port, sp):
            w.config(state="disabled" if st["busy"] else ("readonly" if w in (cb_kind, cb_comp) else "normal"))
        if not st["busy"] and v_status.get() in PROMPTS:
            v_status.set(err if st["file"] and v_port.get() and err else
                         PROMPTS[0] if not st["file"] and not v_port.get() else
                         PROMPTS[1] if not st["file"] else PROMPTS[2] if not v_port.get() else PROMPTS[3])

    PROMPTS = ["ファイルとシリアルポートを選択してください", "ファイルを選択してください", "シリアルポートを選択してください", "送信できます", c.SWITCH_NAME_ERROR]
    v_name.trace_add("write", refresh_state); v_port.trace_add("write", refresh_state)

    def choose_file():
        p = filedialog.askopenfilename()
        if p:
            st["file"] = p; v_file.set(p)
            v_name.set(c.ascii_upper(os.path.basename(p))[:32])
            k = c.infer_kind(os.path.splitext(p)[1].lstrip("."))
            if k: v_kind.set(k)
            refresh_state()
    b_choose.config(command=choose_file)

    def refresh_ports():
        cur = c.available_ports()
        v_port.set(c.choose_port(st["ports"], cur, v_port.get()))
        st["ports"] = cur; cb_port.config(values=cur); refresh_state()
    b_refresh.config(command=refresh_ports)

    def poll_ports():  # udevの代わりに2秒ごとに列挙(Macの/dev監視相当)
        if not st["closing"]:
            if c.available_ports() != st["ports"] and not st["busy"]:
                refresh_ports()
            root.after(2000, poll_ports)

    saved = c.restored_port(cfg.get("port", ""), st["ports"])
    if saved: v_port.set(saved)
    cb_port.bind("<<ComboboxSelected>>", lambda e: (cfg.update(port=v_port.get()), save_cfg()))

    def begin(msg):
        st["busy"] = True; v_prog.set(0); v_remain.set(""); v_status.set(msg); refresh_state()

    def worker(fn):
        tok = c.Cancellation(); st["token"] = tok
        def run():
            try:
                fn(tok); ui.put(("done", None))
            except c.TransferError as e:
                ui.put(("fail", (str(e), tok.is_cancelled)))
            except Exception as e:  # 想定外もUIに出す
                ui.put(("fail", (f"{type(e).__name__}: {e}", tok.is_cancelled)))
        threading.Thread(target=run, daemon=True).start()

    def do_send():
        kind, comp, name, port, path, sync = v_kind.get(), v_comp.get(), v_name.get(), v_port.get(), st["file"], sync_value()
        cfg.update(port=port); save_cfg(); sp.set(sync)
        st["mode"] = "send"
        begin("送信データを準備しています…")
        def fn(tok):
            data, stream, reports = prepare(path, kind, name, comp, sync, lambda: tok.is_cancelled)
            total = int(len(stream) / 296)
            ui.put(("status", f"送信中: {len(data)} バイト → 本体 {c.integer(stream, 112, 4)} バイト（ヘッダー込み {len(stream)} バイト、約{total}秒）"))
            c.send(port, reports, tok, lambda v: ui.put(("prog", (v, int(total * (1 - v))))))
            ui.put(("ok", "送信終了。Switch側でCRC結果を確認し、Aボタンで保存してください"))
        worker(fn)

    def do_detect():
        port = v_port.get(); cfg.update(port=port); save_cfg()
        st["mode"] = "detect"
        begin("Sync Key検出中… SwitchのDETECT SYNC KEY画面を確認してください")
        def fn(tok):
            c.detect_sync_key(port, tok, lambda v: ui.put(("prog", (v, None))))
            ui.put(("ok", "検出信号の送信終了。Switchの表示値をSync Keyに設定後、Bボタンで戻りUSB RECEIVEを選択"))
        worker(fn)

    def do_stop():
        if st["token"]: st["token"].cancel(); v_status.set("中止処理中…")
    b_send.config(command=do_send); b_detect.config(command=do_detect); b_stop.config(command=do_stop)

    # ---- 画像復元タブ
    g = tab_img; g.columnconfigure(0, weight=1); g.rowconfigure(1, weight=1)
    v_imgstatus = tk.StringVar(value="SwitchのSCREENSHOT SENDで保存した画像を追加してください")
    ttk.Label(g, textvariable=v_imgstatus, wraplength=660, justify="left").grid(row=0, column=0, sticky="w")
    tree = ttk.Treeview(g, columns=("file", "name", "page"), show="headings", selectmode="extended")
    for col, txt, w in (("file", "画像", 220), ("name", "ファイル", 260), ("page", "ページ", 80)):
        tree.heading(col, text=txt); tree.column(col, width=w)
    tree.grid(row=1, column=0, sticky="nsew", pady=6)
    loaded = []  # (filename, ScreenshotPage)
    ibar = ttk.Frame(g); ibar.grid(row=2, column=0, sticky="w")

    def redraw():
        tree.delete(*tree.get_children())
        groups = c.group_pages([p for _, p in loaded])
        for i, (fn_, p) in enumerate(loaded):
            miss = c.missing_pages(groups[p.group_key])
            note = f"{p.index+1}/{p.total}" + (f"  不足: {','.join(map(str, miss))}" if miss else "")
            tree.insert("", "end", iid=str(i), values=(fn_, p.name, note))

    def add_images(paths=None):
        paths = paths or filedialog.askopenfilenames(filetypes=[("画像", "*.png *.jpg *.jpeg *.bmp *.tif *.tiff")])
        errors = []
        for p in paths:
            if os.path.isdir(p):
                found = sorted(os.path.join(p, n) for n in os.listdir(p) if c.is_image(n))
                if not found: errors.append(f"{os.path.basename(p)}: 画像がありません。")
                add_images(found) if found else None
                continue
            try:
                loaded.append((os.path.basename(p), c.ScreenshotPage.load(p)))
            except c.TransferError as e:
                errors.append(f"{os.path.basename(p)}: {e}")
        redraw()
        v_imgstatus.set("\n".join(errors) if errors else f"{len(loaded)} 枚を読み込みました。全ページが揃ったファイルを保存できます。")

    def remove_selected():
        for i in sorted((int(s) for s in tree.selection()), reverse=True): del loaded[i]
        redraw()

    def save_images():
        d = filedialog.askdirectory(title="保存先フォルダ")
        if not d: return
        out, errs = [], []
        for key, group in c.group_pages([p for _, p in loaded]).items():
            try: out.append(c.export(group, d))
            except c.TransferError as e: errs.append(f"{group[0].name}: {e}")
        v_imgstatus.set("\n".join([f"保存: {o}" for o in out] + errs))

    ttk.Button(ibar, text="画像を追加…", command=add_images).pack(side="left")
    ttk.Button(ibar, text="選択を削除", command=remove_selected).pack(side="left", padx=6)
    ttk.Button(ibar, text="一覧をクリア", command=lambda: (loaded.clear(), redraw(), v_imgstatus.set("一覧をクリアしました。"))).pack(side="left")
    ttk.Button(ibar, text="復元して保存…", command=save_images).pack(side="right")

    # ---- UIキュー
    def pump():
        try:
            while True:
                kind, val = ui.get_nowait()
                if kind == "prog":
                    v_prog.set(val[0]); v_remain.set(f"残り約{val[1]}秒" if val[1] is not None else "")
                elif kind == "status":
                    v_status.set(val)
                elif kind == "ok":
                    v_status.set(val)
                elif kind in ("done", "fail"):
                    if kind == "fail":
                        msg, cancelled = val
                        v_status.set("検出を中止しました。Switch側はBボタンで戻れます。停止信号が届かず検出が続く場合はUSBを接続し直してください。" if cancelled and st.get("mode") == "detect" else msg)
                    st["busy"] = False; st["token"] = None; v_remain.set(""); refresh_state()
                    if st["closing"]: root.destroy(); return
        except queue.Empty:
            pass
        root.after(50, pump)

    def on_close():  # 送信中はキー解放が済むまで閉じない
        if st["busy"] and st["token"]:
            st["closing"] = True; st["token"].cancel(); v_status.set("終了処理中…")
        else:
            st["closing"] = True; root.destroy()
    root.protocol("WM_DELETE_WINDOW", on_close)
    refresh_state(); pump(); poll_ports()
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]) or 0)
