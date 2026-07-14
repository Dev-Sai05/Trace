#!/usr/bin/env python3
"""
traceviz_app.py - Desktop GUI for the trace log flow visualizer.

Pick a log file, click Run, watch live progress, then open the report.
No command line needed. Must sit in the same folder as traceviz.py.

Run with:
    python3 traceviz_app.py

Requires: Python 3 with tkinter (bundled with most Python installs).
"""

import os
import queue
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox
from tkinter import ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import traceviz as tv

BG = '#080b08'
PANEL = '#0e140f'
PANEL2 = '#101a12'
LINE = '#1c2b1e'
GREEN = '#4dff88'
GREEN_DIM = '#7fb894'
AMBER = '#ffb347'
RED = '#ff5c5c'
GRAY = '#5c6b60'
TEXT = '#c9e6d2'
MONO = ('Consolas', 10) if sys.platform.startswith('win') else ('Menlo', 11) if sys.platform == 'darwin' else ('DejaVu Sans Mono', 10)


class TraceVizApp:
    def __init__(self, root):
        self.root = root
        self.root.title('TRACEVIEW — Trace Log Flow Visualizer')
        self.root.geometry('880x600')
        self.root.configure(bg=BG)
        self.msg_queue = queue.Queue()
        self.worker = None
        self.result = None

        self._build_style()
        self._build_layout()
        self.root.after(120, self._poll_queue)

    def _build_style(self):
        style = ttk.Style()
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass
        style.configure('TProgressbar', troughcolor=PANEL2, background=GREEN,
                         bordercolor=LINE, lightcolor=GREEN, darkcolor=GREEN)

    def _label(self, parent, text, **kw):
        return tk.Label(parent, text=text, bg=kw.pop('bg', BG), fg=kw.pop('fg', TEXT),
                         font=kw.pop('font', MONO), anchor='w', **kw)

    def _button(self, parent, text, cmd, **kw):
        return tk.Button(parent, text=text, command=cmd, bg=PANEL2, fg=GREEN,
                          activebackground=LINE, activeforeground=GREEN,
                          font=MONO, relief='flat', padx=10, pady=6,
                          highlightbackground=LINE, highlightthickness=1, **kw)

    def _build_layout(self):
        # header
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill='x', padx=18, pady=(16, 6))
        tk.Label(header, text='TRACEVIEW', bg=BG, fg=GREEN,
                 font=(MONO[0], 16, 'bold')).pack(side='left')
        tk.Label(header, text='  //  streaming flow visualizer for mainframe trace logs',
                 bg=BG, fg=GRAY, font=MONO).pack(side='left')

        # file picker row
        picker = tk.Frame(self.root, bg=BG)
        picker.pack(fill='x', padx=18, pady=6)
        self._label(picker, 'Trace file:').pack(side='left')
        self.path_var = tk.StringVar()
        entry = tk.Entry(picker, textvariable=self.path_var, bg=PANEL2, fg=TEXT,
                          insertbackground=GREEN, font=MONO, relief='flat')
        entry.pack(side='left', fill='x', expand=True, padx=8, ipady=5)
        self._button(picker, 'Browse…', self._browse).pack(side='left', padx=(0, 4))

        # options row
        opts = tk.Frame(self.root, bg=BG)
        opts.pack(fill='x', padx=18, pady=(0, 6))
        self.stats_only_var = tk.BooleanVar(value=False)
        cb = tk.Checkbutton(opts, text='Stats only (skip HTML report — fastest for a quick check)',
                             variable=self.stats_only_var, bg=BG, fg=TEXT,
                             selectcolor=PANEL2, activebackground=BG, activeforeground=TEXT,
                             font=MONO)
        cb.pack(side='left')

        self.run_btn = self._button(opts, '▸ Run', self._start_run)
        self.run_btn.pack(side='right')

        # progress
        prog = tk.Frame(self.root, bg=BG)
        prog.pack(fill='x', padx=18, pady=(4, 8))
        self.progressbar = ttk.Progressbar(prog, mode='determinate', maximum=100)
        self.progressbar.pack(fill='x')
        self.progress_label = self._label(prog, 'Idle.', fg=GRAY)
        self.progress_label.pack(fill='x', pady=(4, 0))

        # console
        console_frame = tk.Frame(self.root, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        console_frame.pack(fill='both', expand=True, padx=18, pady=(0, 8))
        self.console = tk.Text(console_frame, bg='#050705', fg=GREEN_DIM, insertbackground=GREEN,
                                font=MONO, relief='flat', wrap='word', height=14)
        self.console.pack(fill='both', expand=True, padx=6, pady=6)
        self.console.configure(state='disabled')

        # result / summary bar
        self.summary_frame = tk.Frame(self.root, bg=PANEL)
        self.summary_frame.pack(fill='x', padx=18, pady=(0, 16))
        self.summary_label = self._label(self.summary_frame, '', bg=PANEL, fg=TEXT)
        self.summary_label.pack(side='left', padx=10, pady=8)
        self.open_report_btn = self._button(self.summary_frame, 'Open HTML report', self._open_report, state='disabled')
        self.open_report_btn.pack(side='right', padx=6, pady=6)
        self.open_errors_btn = self._button(self.summary_frame, 'Open error index', self._open_errors, state='disabled')
        self.open_errors_btn.pack(side='right', padx=6, pady=6)

    def _browse(self):
        path = filedialog.askopenfilename(title='Select trace log file',
                                           filetypes=[('Log/trace/text', '*.log *.trc *.txt'), ('All files', '*.*')])
        if path:
            self.path_var.set(path)

    def _log(self, text):
        self.console.configure(state='normal')
        self.console.insert('end', text + '\n')
        self.console.see('end')
        self.console.configure(state='disabled')

    def _start_run(self):
        path = self.path_var.get().strip()
        if not path:
            messagebox.showwarning('No file selected', 'Choose a trace log file first.')
            return
        if not os.path.isfile(path):
            messagebox.showerror('File not found', f'Could not find:\n{path}')
            return
        if self.worker and self.worker.is_alive():
            return

        self.console.configure(state='normal')
        self.console.delete('1.0', 'end')
        self.console.configure(state='disabled')
        self.open_report_btn.configure(state='disabled')
        self.open_errors_btn.configure(state='disabled')
        self.summary_label.configure(text='')
        self.run_btn.configure(state='disabled', text='Running…')
        self.progressbar['value'] = 0
        self.progress_label.configure(text='Starting…')
        self._log(f'Parsing: {path}')

        stats_only = self.stats_only_var.get()
        self.worker = threading.Thread(target=self._worker_run, args=(path, stats_only), daemon=True)
        self.worker.start()

    def _worker_run(self, path, stats_only):
        t0 = time.time()

        def progress_cb(line_no, pct, elapsed, stats):
            self.msg_queue.put(('progress', line_no, pct, elapsed, dict(stats)))

        try:
            root, header, error_index, stats, graph_json = tv.parse_stream(path, progress=progress_cb)
            out_html = out_err = None
            if not stats_only:
                tv.cap_children(root, tv.DEFAULT_MAX_CHILDREN)
                out_html = path + '.report.html'
                out_err = path + '.errors.txt'
                tv.render_html(root, header, error_index, stats, graph_json, out_html)
                tv.write_error_file(error_index, out_err)
            self.msg_queue.put(('done', header, stats, out_html, out_err, time.time() - t0))
        except Exception as e:
            self.msg_queue.put(('error', str(e)))

    def _poll_queue(self):
        try:
            while True:
                item = self.msg_queue.get_nowait()
                kind = item[0]
                if kind == 'progress':
                    _, line_no, pct, elapsed, stats = item
                    self.progressbar['value'] = pct if pct else 0
                    self.progress_label.configure(
                        text=f'{line_no:,} lines parsed  •  {elapsed:.1f}s  •  '
                             f'{stats.get("loops", 0):,} loops collapsed  •  {stats.get("errors", 0):,} errors found')
                    self._log(f'  ...{line_no:,} lines  ({pct:.1f}%)  {elapsed:.1f}s  '
                              f'errors so far: {stats.get("errors", 0)}')
                elif kind == 'done':
                    _, header, stats, out_html, out_err, total_time = item
                    self.progressbar['value'] = 100
                    self.run_btn.configure(state='normal', text='▸ Run')
                    rc = header.get('rc')
                    rc_bad = rc is not None and str(rc).strip().lstrip('-').isdigit() and int(rc) != 0
                    self._log('')
                    self._log(f'Program        : {header.get("program")}')
                    self._log(f'Return code    : {rc}' + ('  ⚠ NON-ZERO' if rc_bad else ''))
                    self._log(f'Lines parsed   : {stats["lines"]:,}')
                    self._log(f'Errors found   : {stats["errors"]:,}')
                    self._log(f'Loops collapsed: {stats["loops"]:,}  (iterations saved: {stats["iter_saved"]:,})')
                    self._log(f'Parse time     : {stats["elapsed"]:.2f}s')
                    self._log('Done.')
                    summary = (f'RC={rc}   errors={stats["errors"]}   '
                               f'loops collapsed={stats["loops"]:,}   time={stats["elapsed"]:.1f}s')
                    self.summary_label.configure(text=summary, fg=(RED if rc_bad or stats['errors'] else GREEN))
                    self.result = (out_html, out_err)
                    if out_html:
                        self.open_report_btn.configure(state='normal')
                    if out_err:
                        self.open_errors_btn.configure(state='normal')
                elif kind == 'error':
                    _, msg = item
                    self.run_btn.configure(state='normal', text='▸ Run')
                    self._log(f'ERROR: {msg}')
                    messagebox.showerror('Parsing failed', msg)
        except queue.Empty:
            pass
        self.root.after(120, self._poll_queue)

    def _open_report(self):
        if self.result and self.result[0]:
            webbrowser.open('file://' + os.path.abspath(self.result[0]))

    def _open_errors(self):
        if self.result and self.result[1]:
            path = os.path.abspath(self.result[1])
            try:
                if sys.platform.startswith('win'):
                    os.startfile(path)
                elif sys.platform == 'darwin':
                    os.system(f'open "{path}"')
                else:
                    os.system(f'xdg-open "{path}"')
            except Exception:
                webbrowser.open('file://' + path)


def main():
    root = tk.Tk()
    app = TraceVizApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
