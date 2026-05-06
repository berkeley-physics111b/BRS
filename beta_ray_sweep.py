"""
lab_controller.py
=================
Tkinter-based laboratory measurement GUI with six tabs:
  1. Power Supply Control   – SPD3303X channel control & monitoring
  2. Scope Viewer           – ADS triggered capture + past-N-traces display
  3. Digital Output         – ADS digital pin high/low toggle
  4. Wavegen Control        – ADS analog out DC high/low
  5. Count-Rate vs Time     – Count triggers over an integration window; log to CSV
  6. Count-Rate vs Current  – Sweep current up/down N times, count triggers; log to CSV

Requires:
  pip install matplotlib numpy
  waveforms_ads.py and spd3303c_power_supply.py alongside this file.
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import threading
import time
import queue
import csv
import datetime
import os
import numpy as np

try:
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    HAS_MPL = True
except ImportError:
    HAS_MPL = False

try:
    from spd3303c_power_supply import SPD3303X
    HAS_PSU = True
except Exception as e:
    print("Power supply SPD3303C package connection error:", e)
    HAS_PSU = False

try:
    from waveforms_ads import (
        WaveFormsADS,
        DwfTriggerSlopeRise, DwfTriggerSlopeFall,
        DwfStateDone,
    )
    HAS_ADS = True
except Exception as e:
    print("ADS package connection error:", e)
    HAS_ADS = False

try:
    from temp_monitor import TempMonitor
    HAS_DMM = True
except Exception as e:
    print("DMM temperature monitor connection error:", e)
    HAS_DMM = False

TEMP_LIMIT_C = 80.0   # Emergency-stop threshold


# ---------------------------------------------------------------------------
# Palette / style constants
# ---------------------------------------------------------------------------
BG        = "#1e1e2e"
FG        = "#cdd6f4"
ACCENT    = "#89b4fa"
PANEL     = "#313244"
ENTRY_BG  = "#45475a"
BUTTON_BG = "#585b70"
GREEN     = "#a6e3a1"
RED       = "#f38ba8"
YELLOW    = "#f9e2af"
PURPLE    = "#cba6f7"
MONO      = ("Courier", 10)
SANS      = ("Helvetica", 10)
SANS_B    = ("Helvetica", 10, "bold")
SANS_LG   = ("Helvetica", 12, "bold")

PADX = 8
PADY = 4


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _lf(parent, text, col=0, row=0, sticky="e", colspan=1, **kw):
    lbl = tk.Label(parent, text=text, bg=PANEL, fg=FG, font=SANS, **kw)
    lbl.grid(column=col, row=row, sticky=sticky, padx=PADX, pady=PADY,
             columnspan=colspan)
    return lbl


def _ef(parent, textvariable, col=1, row=0, width=10, colspan=1):
    e = tk.Entry(parent, textvariable=textvariable, width=width,
                 bg=ENTRY_BG, fg=FG, insertbackground=FG,
                 relief="flat", font=MONO)
    e.grid(column=col, row=row, sticky="ew", padx=PADX, pady=PADY,
           columnspan=colspan)
    return e


def _btn(parent, text, cmd, col=0, row=0, fg=FG, bg=BUTTON_BG, colspan=1, **kw):
    b = tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                  activebackground=ACCENT, activeforeground=BG,
                  relief="flat", font=SANS_B, padx=6, pady=3, **kw)
    b.grid(column=col, row=row, padx=PADX, pady=PADY, sticky="ew",
           columnspan=colspan)
    return b


def _section(parent, title, row=0, colspan=10):
    lbl = tk.Label(parent, text=f"  {title}  ", bg=ACCENT, fg=BG, font=SANS_B)
    lbl.grid(column=0, row=row, columnspan=colspan, sticky="ew",
             padx=PADX, pady=(12, 2))
    return lbl


def _status_bar(parent, var):
    lbl = tk.Label(parent, textvariable=var, bg=BG, fg=YELLOW,
                   font=MONO, anchor="w")
    lbl.pack(side="bottom", fill="x")
    return lbl


def _ax_style(ax, xlabel="", ylabel="", title=""):
    ax.set_facecolor(PANEL)
    ax.tick_params(colors=FG)
    for sp in ax.spines.values():
        sp.set_edgecolor(FG)
    ax.set_xlabel(xlabel, color=FG)
    ax.set_ylabel(ylabel, color=FG)
    ax.set_title(title, color=ACCENT)


# ---------------------------------------------------------------------------
# Scrollable frame
# ---------------------------------------------------------------------------

class ScrollableFrame(tk.Frame):
    """A vertically-scrollable container. Use .inner for child widgets."""

    def __init__(self, parent, bg=BG, width=320, **kw):
        super().__init__(parent, bg=bg, **kw)
        self._canvas = tk.Canvas(self, bg=bg, highlightthickness=0, width=width)
        sb = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self.inner = tk.Frame(self._canvas, bg=bg)
        self._win = self._canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self._canvas.configure(yscrollcommand=sb.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.inner.bind("<Configure>", lambda _e: self._canvas.configure(
            scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>", lambda e: self._canvas.itemconfig(
            self._win, width=e.width))
        self._canvas.bind("<Enter>",  lambda _e: self._canvas.bind_all("<MouseWheel>", self._scroll))
        self._canvas.bind("<Leave>",  lambda _e: self._canvas.unbind_all("<MouseWheel>"))

    def _scroll(self, event):
        self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")


# ---------------------------------------------------------------------------
# Shared scope-settings frame
# ---------------------------------------------------------------------------

class ScopeSettingsFrame(tk.LabelFrame):
    def __init__(self, parent, **kw):
        super().__init__(parent, text="Scope Settings", bg=PANEL, fg=ACCENT,
                         font=SANS_B, **kw)
        self._build()

    def _build(self):
        self.trig_level   = tk.DoubleVar(value=0.15)
        self.edge         = tk.StringVar(value="Rise")
        self.sample_freq  = tk.DoubleVar(value=1e8)
        self.y_range      = tk.DoubleVar(value=0.1)
        self.y_offset     = tk.DoubleVar(value=0.0)
        self.time_base_us = tk.DoubleVar(value=0.1)
        self.probe_invert = tk.BooleanVar(value=True)
        self.channel      = tk.IntVar(value=0)

        rows = [
            ("Channel (0-based):", self.channel,      5),
            ("Trigger Level (V):", self.trig_level,   8),
            ("Sample Freq (Hz):",  self.sample_freq,  10),
            ("Y Range (V/div):",   self.y_range,      8),
            ("Vertical Offset (V):", self.y_offset,   8),
            ("Time Base (μs):",    self.time_base_us, 8),
        ]
        for r, (label, var, width) in enumerate(rows):
            _lf(self, label, col=0, row=r)
            _ef(self, var, col=1, row=r, width=width)

        r = len(rows)
        _lf(self, "Edge:", col=0, row=r)
        om = ttk.OptionMenu(self, self.edge, "Rise", "Rise", "Fall")
        om.configure(style="TMenubutton")
        om.grid(column=1, row=r, sticky="ew", padx=PADX, pady=PADY)

        r += 1
        tk.Checkbutton(self, text="Invert Probe", variable=self.probe_invert,
                       bg=PANEL, fg=FG, selectcolor=ENTRY_BG,
                       activebackground=PANEL, font=SANS).grid(
            column=0, row=r, columnspan=2, sticky="w", padx=PADX, pady=PADY)

    def get_params(self):
        fs   = self.sample_freq.get()
        tb   = self.time_base_us.get() / 1e6
        buf  = max(64, int(fs * tb))
        slope = DwfTriggerSlopeRise if self.edge.get() == "Rise" else DwfTriggerSlopeFall
        return dict(
            channel=self.channel.get(), trigger_level=self.trig_level.get(),
            slope=slope, sample_rate=fs, y_range=self.y_range.get(),
            y_offset=self.y_offset.get(), time_base_us=self.time_base_us.get(),
            buffer_size=buf, invert=self.probe_invert.get(),
        )


# ===========================================================================
# TAB 1 – Power Supply
# ===========================================================================

class PowerSupplyTab(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent, bg=BG)
        self._psu     = None
        self._poll_id = None
        self._status  = tk.StringVar(value="Not connected")
        self._build()

    def _build(self):
        pad = dict(padx=PADX, pady=PADY)

        conn = tk.LabelFrame(self, text="Connection", bg=PANEL, fg=ACCENT, font=SANS_B)
        conn.pack(fill="x", padx=10, pady=8)
        self.conn_type = tk.StringVar(value="USB")
        tk.Label(conn, text="Interface:", bg=PANEL, fg=FG, font=SANS).grid(column=0, row=0, **pad, sticky="e")
        ttk.OptionMenu(conn, self.conn_type, "USB", "USB", "Ethernet").grid(column=1, row=0, **pad, sticky="ew")
        self.host_var = tk.StringVar(value="192.168.1.100")
        tk.Label(conn, text="Host (Ethernet):", bg=PANEL, fg=FG, font=SANS).grid(column=2, row=0, **pad, sticky="e")
        tk.Entry(conn, textvariable=self.host_var, width=16,
                 bg=ENTRY_BG, fg=FG, insertbackground=FG, font=MONO).grid(column=3, row=0, **pad, sticky="ew")
        _btn(conn, "Connect",    self._connect,    col=4, row=0, bg=GREEN, fg=BG)
        _btn(conn, "Disconnect", self._disconnect, col=5, row=0, bg=RED,   fg=BG)

        ctrl = tk.LabelFrame(self, text="Channel Control", bg=PANEL, fg=ACCENT, font=SANS_B)
        ctrl.pack(fill="x", padx=10, pady=4)
        self.ch_var = tk.StringVar(value="CH1")
        tk.Label(ctrl, text="Channel:", bg=PANEL, fg=FG, font=SANS).grid(column=0, row=0, **pad, sticky="e")
        ttk.OptionMenu(ctrl, self.ch_var, "CH1", "CH1", "CH2").grid(column=1, row=0, **pad, sticky="ew")

        self.volt_set = tk.DoubleVar(value=3.3)
        self.curr_set = tk.DoubleVar(value=0.5)
        tk.Label(ctrl, text="Set Voltage (V):", bg=PANEL, fg=FG, font=SANS).grid(column=0, row=1, **pad, sticky="e")
        tk.Entry(ctrl, textvariable=self.volt_set, width=10,
                 bg=ENTRY_BG, fg=FG, insertbackground=FG, font=MONO).grid(column=1, row=1, **pad, sticky="ew")
        _btn(ctrl, "Apply Voltage", self._set_voltage, col=2, row=1)
        tk.Label(ctrl, text="Set Current (A):", bg=PANEL, fg=FG, font=SANS).grid(column=0, row=2, **pad, sticky="e")
        tk.Entry(ctrl, textvariable=self.curr_set, width=10,
                 bg=ENTRY_BG, fg=FG, insertbackground=FG, font=MONO).grid(column=1, row=2, **pad, sticky="ew")
        _btn(ctrl, "Apply Current", self._set_current, col=2, row=2)

        btn_row = tk.Frame(ctrl, bg=PANEL)
        btn_row.grid(column=0, row=3, columnspan=4, **pad, sticky="ew")
        tk.Button(btn_row, text="Output ON",  command=self._output_on,  bg=GREEN, fg=BG, font=SANS_B, padx=10, pady=4, relief="flat").pack(side="left", padx=6)
        tk.Button(btn_row, text="Output OFF", command=self._output_off, bg=RED,   fg=BG, font=SANS_B, padx=10, pady=4, relief="flat").pack(side="left", padx=6)

        meas = tk.LabelFrame(self, text="Live Measurements", bg=PANEL, fg=ACCENT, font=SANS_B)
        meas.pack(fill="x", padx=10, pady=4)
        self.meas_volt = tk.StringVar(value="—")
        self.meas_curr = tk.StringVar(value="—")
        tk.Label(meas, text="Measured Voltage:", bg=PANEL, fg=FG, font=SANS).grid(column=0, row=0, **pad, sticky="e")
        tk.Label(meas, textvariable=self.meas_volt, bg=PANEL, fg=GREEN, font=("Courier", 14, "bold")).grid(column=1, row=0, **pad, sticky="w")
        tk.Label(meas, text="Measured Current:", bg=PANEL, fg=FG, font=SANS).grid(column=0, row=1, **pad, sticky="e")
        tk.Label(meas, textvariable=self.meas_curr, bg=PANEL, fg=GREEN, font=("Courier", 14, "bold")).grid(column=1, row=1, **pad, sticky="w")
        _btn(meas, "Poll Once", self._poll_once, col=2, row=0)

        _status_bar(self, self._status)

    def _get_chan(self):
        if self._psu is None:
            messagebox.showerror("Error", "Not connected to power supply.")
            return None
        return getattr(self._psu, self.ch_var.get(), None)

    def _connect(self):
        if not HAS_PSU:
            messagebox.showerror("Error", "Power supply module not available."); return
        try:
            dev = (SPD3303X.usb_device() if self.conn_type.get() == "USB"
                   else SPD3303X.ethernet_device(self.host_var.get())).__enter__()
            self._psu = dev
            self._status.set("Connected to power supply")
            self._do_poll()
        except Exception as e:
            messagebox.showerror("Connection Error", str(e))

    def _disconnect(self):
        if self._poll_id: self.after_cancel(self._poll_id); self._poll_id = None
        if self._psu:
            try: self._psu._inst.close()
            except Exception: pass
            self._psu = None
        self._status.set("Disconnected")
        self.meas_volt.set("—"); self.meas_curr.set("—")

    def _set_voltage(self):
        ch = self._get_chan()
        if ch:
            try: ch.set_voltage(self.volt_set.get()); self._status.set(f"Voltage → {self.volt_set.get():.3f} V")
            except Exception as e: messagebox.showerror("Error", str(e))

    def _set_current(self):
        ch = self._get_chan()
        if ch:
            try: ch.set_current(self.curr_set.get()); self._status.set(f"Current limit → {self.curr_set.get():.3f} A")
            except Exception as e: messagebox.showerror("Error", str(e))

    def _output_on(self):
        ch = self._get_chan()
        if ch:
            try: ch.set_output(True);  self._status.set(f"{self.ch_var.get()} output ON")
            except Exception as e: messagebox.showerror("Error", str(e))

    def _output_off(self):
        ch = self._get_chan()
        if ch:
            try: ch.set_output(False); self._status.set(f"{self.ch_var.get()} output OFF")
            except Exception as e: messagebox.showerror("Error", str(e))

    def _poll_once(self):
        ch = self._get_chan()
        if ch and hasattr(ch, "measure_voltage"):
            try:
                self.meas_volt.set(f"{float(ch.measure_voltage()):.4f} V")
                self.meas_curr.set(f"{float(ch.measure_current()):.4f} A")
            except Exception as e: self._status.set(f"Poll error: {e}")

    def _do_poll(self):
        self._poll_once()
        self._poll_id = self.after(1000, self._do_poll)


# ===========================================================================
# TAB 2 – Scope Viewer
# ===========================================================================

class ScopeViewerTab(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent, bg=BG)
        self._running = False
        self._thread  = None
        self._q       = queue.Queue()
        self._traces  = []
        self._status  = tk.StringVar(value="Idle")
        self._last_ts = tk.StringVar(value="—")
        self._build()
        self.after(100, self._drain_queue)

    def _build(self):
        left = tk.Frame(self, bg=BG)
        left.pack(side="left", fill="y", padx=6, pady=6)
        self.scope_cfg = ScopeSettingsFrame(left)
        self.scope_cfg.pack(fill="x", pady=4)
        ctrl = tk.LabelFrame(left, text="Capture Control", bg=PANEL, fg=ACCENT, font=SANS_B)
        ctrl.pack(fill="x", pady=4)
        self.n_traces = tk.IntVar(value=3)
        _lf(ctrl, "Show last N traces:", col=0, row=0); _ef(ctrl, self.n_traces, col=1, row=0, width=5)
        _btn(ctrl, "Start", self._start, col=0, row=1, bg=GREEN, fg=BG)
        _btn(ctrl, "Stop",  self._stop,  col=1, row=1, bg=RED,   fg=BG)
        tk.Label(ctrl, text="Last trigger:", bg=PANEL, fg=FG, font=SANS).grid(column=0, row=2, sticky="e", padx=PADX)
        tk.Label(ctrl, textvariable=self._last_ts, bg=PANEL, fg=YELLOW, font=MONO).grid(column=1, row=2, sticky="w", padx=PADX)
        _status_bar(left, self._status)

        right = tk.Frame(self, bg=BG)
        right.pack(side="left", fill="both", expand=True, padx=4, pady=6)
        if HAS_MPL:
            self._fig = Figure(figsize=(6, 4), facecolor=BG)
            self._ax  = self._fig.add_subplot(111)
            _ax_style(self._ax, "Time (μs)", "Voltage (V)", "Scope Traces")
            self._canvas = FigureCanvasTkAgg(self._fig, master=right)
            self._canvas.get_tk_widget().pack(fill="both", expand=True)
        else:
            tk.Label(right, text="matplotlib not installed", bg=BG, fg=RED, font=SANS_LG).pack(expand=True)

    def _start(self):
        if self._running: return
        if not HAS_ADS: messagebox.showerror("Error", "waveforms_ads module not available."); return
        self._running = True; self._status.set("Running…")
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def _stop(self):
        self._running = False; self._status.set("Stopped")

    def _capture_loop(self):
        p = self.scope_cfg.get_params()
        att = -1 if p["invert"] else 1
        try:
            with WaveFormsADS() as dev:
                dev.analog_in_set_range(p["channel"], p["y_range"])
                while self._running:
                    data = dev.analog_in_capture(
                        channel=p["channel"], sample_rate_hz=p["sample_rate"],
                        buffer_size=p["buffer_size"], trigger_level_v=p["trigger_level"],
                        trigger_condition=p["slope"], y_range=p["y_range"],
                        y_offset=p["y_offset"], attenuation=att,
                        auto_timeout_s=0.0, timeout_s=2.0)
                    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
                    self._q.put(("trace", ts, data))
        except Exception as e:
            self._q.put(("error", str(e)))
        self._running = False

    def _drain_queue(self):
        while not self._q.empty():
            msg = self._q.get_nowait()
            if msg[0] == "trace":
                _, ts, data = msg
                self._last_ts.set(ts)
                self._traces.append((ts, data))
                self._traces = self._traces[-max(1, self.n_traces.get()):]
                self._redraw()
            elif msg[0] == "error":
                self._status.set(f"Error: {msg[1]}"); self._running = False
        self.after(100, self._drain_queue)

    def _redraw(self):
        if not HAS_MPL: return
        p = self.scope_cfg.get_params()
        self._ax.clear(); _ax_style(self._ax, "Time (μs)", "Voltage (V)", "Scope Traces")
        colors = [ACCENT, GREEN, YELLOW, RED, PURPLE]
        for i, (ts, data) in enumerate(self._traces):
            alpha = 0.4 + 0.6 * (i + 1) / len(self._traces)
            t_us = np.arange(len(data)) / p["sample_rate"] * 1e6
            self._ax.plot(t_us, data, color=colors[i % len(colors)],
                          alpha=alpha, linewidth=1, label=ts)
        self._ax.axhline(y=p["trigger_level"], color=YELLOW, linestyle='--', linewidth=0.8, label="Trigger")
        self._ax.legend(fontsize=7, facecolor=PANEL, labelcolor=FG, loc='upper right')
        self._canvas.draw()


# ===========================================================================
# TAB 3 – Digital Output
# ===========================================================================

class DigitalOutputTab(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent, bg=BG)
        self._ads = None
        self._status = tk.StringVar(value="Not connected")
        self._level  = tk.StringVar(value="LOW")
        self._build()

    def _build(self):
        pad = dict(padx=PADX, pady=PADY)
        conn = tk.LabelFrame(self, text="ADS Connection", bg=PANEL, fg=ACCENT, font=SANS_B)
        conn.pack(fill="x", padx=10, pady=8)
        _btn(conn, "Connect ADS",    self._connect,    col=0, row=0, bg=GREEN, fg=BG)
        _btn(conn, "Disconnect ADS", self._disconnect, col=1, row=0, bg=RED,   fg=BG)

        ctrl = tk.LabelFrame(self, text="Digital Output Control", bg=PANEL, fg=ACCENT, font=SANS_B)
        ctrl.pack(fill="x", padx=10, pady=4)
        self.pin_var = tk.IntVar(value=0)
        tk.Label(ctrl, text="Pin (0-based):", bg=PANEL, fg=FG, font=SANS).grid(column=0, row=0, **pad, sticky="e")
        tk.Entry(ctrl, textvariable=self.pin_var, width=5,
                 bg=ENTRY_BG, fg=FG, insertbackground=FG, font=MONO).grid(column=1, row=0, **pad, sticky="ew")
        self._level_lbl = tk.Label(ctrl, textvariable=self._level, bg=PANEL, fg=RED, font=("Courier", 28, "bold"))
        self._level_lbl.grid(column=0, row=1, columnspan=2, **pad)
        btn_row = tk.Frame(ctrl, bg=PANEL)
        btn_row.grid(column=0, row=2, columnspan=3, **pad, sticky="ew")
        tk.Button(btn_row, text="Set HIGH", command=self._set_high, bg=GREEN, fg=BG, font=SANS_B, padx=14, pady=6, relief="flat").pack(side="left", padx=8)
        tk.Button(btn_row, text="Set LOW",  command=self._set_low,  bg=RED,   fg=BG, font=SANS_B, padx=14, pady=6, relief="flat").pack(side="left", padx=8)
        tk.Button(btn_row, text="Toggle",   command=self._toggle,   bg=BUTTON_BG, fg=FG, font=SANS_B, padx=14, pady=6, relief="flat").pack(side="left", padx=8)
        _status_bar(self, self._status)

    def _connect(self):
        if not HAS_ADS: messagebox.showerror("Error", "waveforms_ads not available."); return
        try:
            self._ads = WaveFormsADS(); pin = self.pin_var.get()
            self._ads.digital_io_reset()
            self._ads.digital_io_set_output_enable(1 << pin)
            self._ads.digital_io_set_output(0)
            self._status.set(f"Connected – pin {pin} as output (LOW)")
            self._update_indicator(False)
        except Exception as e: messagebox.showerror("Error", str(e))

    def _disconnect(self):
        if self._ads:
            try: self._ads.close()
            except Exception: pass
            self._ads = None
        self._status.set("Disconnected")

    def _require_ads(self):
        if not self._ads: messagebox.showerror("Error", "Not connected."); return False
        return True

    def _set_high(self):
        if not self._require_ads(): return
        try:
            self._ads.digital_io_write_pin(self.pin_var.get(), True)
            self._update_indicator(True); self._status.set(f"Pin {self.pin_var.get()} → HIGH")
        except Exception as e: messagebox.showerror("Error", str(e))

    def _set_low(self):
        if not self._require_ads(): return
        try:
            self._ads.digital_io_write_pin(self.pin_var.get(), False)
            self._update_indicator(False); self._status.set(f"Pin {self.pin_var.get()} → LOW")
        except Exception as e: messagebox.showerror("Error", str(e))

    def _toggle(self):
        self._set_low() if self._level.get() == "HIGH" else self._set_high()

    def _update_indicator(self, high):
        self._level.set("HIGH" if high else "LOW")
        self._level_lbl.config(fg=GREEN if high else RED)


# ===========================================================================
# TAB 4 – Wavegen Control
# ===========================================================================

class WavegenControlTab(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent, bg=BG)
        self._ads = None
        self._status = tk.StringVar(value="Not connected")
        self._level  = tk.StringVar(value="LOW")
        self._build()

    def _build(self):
        pad = dict(padx=PADX, pady=PADY)
        conn = tk.LabelFrame(self, text="ADS Connection", bg=PANEL, fg=ACCENT, font=SANS_B)
        conn.pack(fill="x", padx=10, pady=8)
        _btn(conn, "Connect ADS",    self._connect,    col=0, row=0, bg=GREEN, fg=BG)
        _btn(conn, "Disconnect ADS", self._disconnect, col=1, row=0, bg=RED,   fg=BG)

        ctrl = tk.LabelFrame(self, text="Wavegen Output Control", bg=PANEL, fg=ACCENT, font=SANS_B)
        ctrl.pack(fill="x", padx=10, pady=4)
        self.channel_var = tk.IntVar(value=0)
        tk.Label(ctrl, text="Channel (0-based):", bg=PANEL, fg=FG, font=SANS).grid(column=0, row=0, **pad, sticky="e")
        tk.Entry(ctrl, textvariable=self.channel_var, width=5,
                 bg=ENTRY_BG, fg=FG, insertbackground=FG, font=MONO).grid(column=1, row=0, **pad, sticky="ew")
        self._level_lbl = tk.Label(ctrl, textvariable=self._level, bg=PANEL, fg=RED, font=("Courier", 28, "bold"))
        self._level_lbl.grid(column=0, row=1, columnspan=2, **pad)
        btn_row = tk.Frame(ctrl, bg=PANEL)
        btn_row.grid(column=0, row=2, columnspan=3, **pad, sticky="ew")
        tk.Button(btn_row, text="Set HIGH", command=self._set_high, bg=GREEN, fg=BG, font=SANS_B, padx=14, pady=6, relief="flat").pack(side="left", padx=8)
        tk.Button(btn_row, text="Set LOW",  command=self._set_low,  bg=RED,   fg=BG, font=SANS_B, padx=14, pady=6, relief="flat").pack(side="left", padx=8)
        tk.Button(btn_row, text="Toggle",   command=self._toggle,   bg=BUTTON_BG, fg=FG, font=SANS_B, padx=14, pady=6, relief="flat").pack(side="left", padx=8)
        _status_bar(self, self._status)

    def _connect(self):
        if not HAS_ADS: messagebox.showerror("Error", "waveforms_ads not available."); return
        try:
            self._ads = WaveFormsADS(); ch = self.channel_var.get()
            self._ads.analog_out_reset()
            self._ads.analog_out_enable_node(channel=ch)
            self._ads.analog_out_set_dc(channel=ch, voltage_v=0)
            self._status.set(f"Connected – ch {ch} as output (LOW)")
            self._update_indicator(False)
        except Exception as e: messagebox.showerror("Error", str(e))

    def _disconnect(self):
        if self._ads:
            try: self._ads.close()
            except Exception: pass
            self._ads = None
        self._status.set("Disconnected")

    def _require_ads(self):
        if not self._ads: messagebox.showerror("Error", "Not connected."); return False
        return True

    def _set_high(self):
        if not self._require_ads(): return
        try:
            self._ads.analog_out_set_dc(channel=self.channel_var.get(), voltage_v=5)
            self._update_indicator(True); self._status.set(f"Ch {self.channel_var.get()} → HIGH")
        except Exception as e: messagebox.showerror("Error", str(e))

    def _set_low(self):
        if not self._require_ads(): return
        try:
            self._ads.analog_out_set_dc(channel=self.channel_var.get(), voltage_v=0)
            self._update_indicator(False); self._status.set(f"Ch {self.channel_var.get()} → LOW")
        except Exception as e: messagebox.showerror("Error", str(e))

    def _toggle(self):
        self._set_low() if self._level.get() == "HIGH" else self._set_high()

    def _update_indicator(self, high):
        self._level.set("HIGH" if high else "LOW")
        self._level_lbl.config(fg=GREEN if high else RED)


# ===========================================================================
# TAB 4.5 – Multimeter Readout  (voltage, current, temperature)
# ===========================================================================

class MultimeterReadoutTab(tk.Frame):
    """
    Connects to two Keysight 2110 DMMs via pyvisa (through TempMonitor).
    Polls voltage, current, and derived temperature every few seconds.
    """
    POLL_INTERVAL_MS = 3000   # ms between auto-polls

    def __init__(self, parent):
        super().__init__(parent, bg=BG)
        self._monitor   = None
        self._poll_id   = None
        self._status    = tk.StringVar(value="Not connected")
        self._volt_var  = tk.StringVar(value="—")
        self._curr_var  = tk.StringVar(value="—")
        self._temp_var  = tk.StringVar(value="—")
        self._temp_lbl  = None   # coloured label widget, set in _build
        self._build()

    def _build(self):
        pad = dict(padx=PADX, pady=PADY)

        # ---- Connection ----
        conn = tk.LabelFrame(self, text="DMM Connection", bg=PANEL, fg=ACCENT, font=SANS_B)
        conn.pack(fill="x", padx=10, pady=8)

        self._ammeter_var  = tk.StringVar(value="USB0::0x05E6::0x2110::1373999::INSTR")
        self._voltmeter_var= tk.StringVar(value="USB0::0x05E6::0x2110::1415286::INSTR")

        tk.Label(conn, text="Ammeter VISA:", bg=PANEL, fg=FG, font=SANS).grid(
            column=0, row=0, **pad, sticky="e")
        tk.Entry(conn, textvariable=self._ammeter_var, width=38,
                 bg=ENTRY_BG, fg=FG, insertbackground=FG, font=MONO).grid(
            column=1, row=0, **pad, sticky="ew", columnspan=2)

        tk.Label(conn, text="Voltmeter VISA:", bg=PANEL, fg=FG, font=SANS).grid(
            column=0, row=1, **pad, sticky="e")
        tk.Entry(conn, textvariable=self._voltmeter_var, width=38,
                 bg=ENTRY_BG, fg=FG, insertbackground=FG, font=MONO).grid(
            column=1, row=1, **pad, sticky="ew", columnspan=2)

        btn_row = tk.Frame(conn, bg=PANEL)
        btn_row.grid(column=0, row=2, columnspan=3, **pad, sticky="w")
        tk.Button(btn_row, text="Connect",    command=self._connect,
                  bg=GREEN, fg=BG, font=SANS_B, padx=10, pady=4,
                  relief="flat").pack(side="left", padx=6)
        tk.Button(btn_row, text="Disconnect", command=self._disconnect,
                  bg=RED, fg=BG, font=SANS_B, padx=10, pady=4,
                  relief="flat").pack(side="left", padx=6)

        self._poll_iv = tk.IntVar(value=self.POLL_INTERVAL_MS // 1000)
        tk.Label(btn_row, text="  Poll interval (s):", bg=PANEL, fg=FG,
                 font=SANS).pack(side="left", padx=(12, 2))
        tk.Entry(btn_row, textvariable=self._poll_iv, width=4,
                 bg=ENTRY_BG, fg=FG, insertbackground=FG, font=MONO).pack(side="left")

        # ---- Readout panel ----
        rdout = tk.LabelFrame(self, text="Live Readings", bg=PANEL, fg=ACCENT, font=SANS_B)
        rdout.pack(fill="x", padx=10, pady=8)

        BIG = ("Courier", 26, "bold")
        MED = ("Helvetica", 11)

        def _row(parent, label, var, row, color=GREEN, unit=""):
            tk.Label(parent, text=label, bg=PANEL, fg=FG, font=MED).grid(
                column=0, row=row, sticky="e", padx=PADX, pady=8)
            lbl = tk.Label(parent, textvariable=var, bg=PANEL,
                           fg=color, font=BIG, anchor="w")
            lbl.grid(column=1, row=row, sticky="w", padx=PADX, pady=8)
            tk.Label(parent, text=unit, bg=PANEL, fg=FG, font=MED).grid(
                column=2, row=row, sticky="w")
            return lbl

        _row(rdout, "Voltage:",     self._volt_var, 0, GREEN,  "V")
        _row(rdout, "Current:",     self._curr_var, 1, GREEN,  "A")
        self._temp_lbl = _row(rdout, "Temperature:", self._temp_var, 2, GREEN, "°C")

        _btn(rdout, "Poll once", self._poll_once, col=0, row=3, colspan=3)

        _status_bar(self, self._status)

    # ---- Connection ----
    def _connect(self):
        if not HAS_DMM:
            messagebox.showerror("Error", "temp_monitor module not available."); return
        try:
            self._monitor = TempMonitor(
                ammeter  = self._ammeter_var.get().strip(),
                voltmeter= self._voltmeter_var.get().strip(),
            )
            self._status.set("Connected — polling every "
                             f"{self._poll_iv.get()} s")
            self._schedule_poll()
        except Exception as e:
            messagebox.showerror("Connection Error", str(e))

    def _disconnect(self):
        self._cancel_poll()
        if self._monitor:
            try:
                self._monitor.ammeter.close()
                self._monitor.voltmeter.close()
            except Exception: pass
            self._monitor = None
        self._status.set("Disconnected")
        self._volt_var.set("—"); self._curr_var.set("—"); self._temp_var.set("—")
        if self._temp_lbl: self._temp_lbl.config(fg=GREEN)

    def _cancel_poll(self):
        if self._poll_id:
            self.after_cancel(self._poll_id); self._poll_id = None

    def _schedule_poll(self):
        interval_ms = max(500, self._poll_iv.get() * 1000)
        self._poll_id = self.after(interval_ms, self._auto_poll)

    def _auto_poll(self):
        self._poll_once()
        self._schedule_poll()   # reschedule

    def _poll_once(self):
        if not self._monitor:
            messagebox.showerror("Error", "Not connected to DMMs."); return
        # Run in background so we don't freeze the GUI during slow VISA queries
        threading.Thread(target=self._do_poll_thread, daemon=True).start()

    def _do_poll_thread(self):
        try:
            v = self._monitor.measure_voltage()
            i = self._monitor.measure_current()
            t = self._monitor.measure_temperature()
            self.after(0, lambda: self._update_display(v, i, t))
        except Exception as e:
            self.after(0, lambda: self._status.set(f"Poll error: {e}"))

    def _update_display(self, v, i, t):
        self._volt_var.set(f"{v:.6f}")
        self._curr_var.set(f"{i:.6f}")
        self._temp_var.set(f"{t:.2f}")
        # Colour the temperature red if approaching the safety limit
        if t >= TEMP_LIMIT_C:
            self._temp_lbl.config(fg=RED)
        elif t >= TEMP_LIMIT_C - 10:
            self._temp_lbl.config(fg=YELLOW)
        else:
            self._temp_lbl.config(fg=GREEN)
        self._status.set(
            f"Last poll: {datetime.datetime.now().strftime('%H:%M:%S')}  "
            f"| T={t:.1f}°C  V={v:.4f}V  I={i:.4f}A")


# ===========================================================================
# TAB 5 – Count-Rate vs Time
# ===========================================================================

class CountRateTimeTab(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent, bg=BG)
        self._running = False; self._thread = None
        self._q = queue.Queue(); self._data = []
        self._csv_file = None; self._csv_writer = None
        self._status = tk.StringVar(value="Idle")
        self._last_trace = None
        self._build()
        self.after(150, self._drain_queue)

    def _build(self):
        left = tk.Frame(self, bg=BG)
        left.pack(side="left", fill="y", padx=6, pady=6)
        self.scope_cfg = ScopeSettingsFrame(left)
        self.scope_cfg.pack(fill="x", pady=4)

        meas = tk.LabelFrame(left, text="Measurement Settings", bg=PANEL, fg=ACCENT, font=SANS_B)
        meas.pack(fill="x", pady=4)
        self.integ_time = tk.DoubleVar(value=1.0)
        _lf(meas, "Integration Time (s):", col=0, row=0); _ef(meas, self.integ_time, col=1, row=0)
        self.save_csv = tk.BooleanVar(value=False); self.csv_path = tk.StringVar(value="")
        tk.Checkbutton(meas, text="Save to CSV", variable=self.save_csv,
                       command=self._toggle_csv_path, bg=PANEL, fg=FG,
                       selectcolor=ENTRY_BG, activebackground=PANEL, font=SANS).grid(
            column=0, row=1, columnspan=2, sticky="w", padx=PADX, pady=PADY)
        self._csv_entry = tk.Entry(meas, textvariable=self.csv_path, width=20,
                                   bg=ENTRY_BG, fg=FG, insertbackground=FG,
                                   font=MONO, state="disabled")
        self._csv_entry.grid(column=0, row=2, columnspan=2, sticky="ew", padx=PADX, pady=PADY)
        _btn(meas, "Browse…", self._browse_csv, col=2, row=2)

        btn_row = tk.Frame(left, bg=BG)
        btn_row.pack(fill="x", pady=4)
        tk.Button(btn_row, text="Start", command=self._start, bg=GREEN, fg=BG, font=SANS_B, padx=10, pady=4, relief="flat").pack(side="left", padx=6)
        tk.Button(btn_row, text="Stop",  command=self._stop,  bg=RED,   fg=BG, font=SANS_B, padx=10, pady=4, relief="flat").pack(side="left", padx=6)
        tk.Button(btn_row, text="Clear", command=self._clear, bg=BUTTON_BG, fg=FG, font=SANS_B, padx=10, pady=4, relief="flat").pack(side="left", padx=6)
        _status_bar(left, self._status)

        right = tk.Frame(self, bg=BG)
        right.pack(side="left", fill="both", expand=True, padx=4, pady=6)
        if HAS_MPL:
            self._fig  = Figure(figsize=(6, 5), facecolor=BG)
            self._ax_cr = self._fig.add_subplot(211)
            self._ax_tr = self._fig.add_subplot(212)
            _ax_style(self._ax_cr, "Elapsed time (s)", "Counts / window", "Count Rate vs Time")
            _ax_style(self._ax_tr, "Sample", "Voltage (V)", "Most Recent Trigger Trace")
            self._fig.tight_layout(pad=2)
            self._canvas = FigureCanvasTkAgg(self._fig, master=right)
            self._canvas.get_tk_widget().pack(fill="both", expand=True)
        else:
            tk.Label(right, text="matplotlib not installed", bg=BG, fg=RED, font=SANS_LG).pack(expand=True)

    def _toggle_csv_path(self):
        self._csv_entry.config(state="normal" if self.save_csv.get() else "disabled")

    def _browse_csv(self):
        path = filedialog.asksaveasfilename(defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All", "*.*")])
        if path: self.csv_path.set(path)

    def _start(self):
        if self._running: return
        if not HAS_ADS: messagebox.showerror("Error", "waveforms_ads not available."); return
        if self.save_csv.get():
            path = self.csv_path.get().strip()
            if not path: messagebox.showerror("Error", "Choose a CSV path first."); return
            self._csv_file = open(path, "w", newline="")
            self._csv_writer = csv.writer(self._csv_file)
            self._csv_writer.writerow(["Timestamp", "Elapsed_s", "Counts"])
        self._running = True; self._status.set("Running…")
        self._thread = threading.Thread(target=self._measure_loop, daemon=True)
        self._thread.start()

    def _stop(self):
        self._running = False
        if self._csv_file: self._csv_file.close(); self._csv_file = self._csv_writer = None
        self._status.set("Stopped")

    def _clear(self):
        self._data = []; self._last_trace = None; self._redraw()

    def _measure_loop(self):
        p = self.scope_cfg.get_params()
        integ = self.integ_time.get(); t0 = time.time()
        att = -1 if p["invert"] else 1
        try:
            with WaveFormsADS() as dev:
                dev.analog_in_set_range(p["channel"], p["y_range"])
                while self._running:
                    window_start = time.time(); count = 0; last_trace = None
                    while time.time() - window_start < integ and self._running:
                        try:
                            data = dev.analog_in_capture(
                                channel=p["channel"], sample_rate_hz=p["sample_rate"],
                                buffer_size=p["buffer_size"], trigger_level_v=p["trigger_level"],
                                trigger_condition=p["slope"], y_range=p["y_range"],
                                y_offset=p["y_offset"], attenuation=att,
                                auto_timeout_s=0.0, timeout_s=max(integ * 2, 1.0))
                            count += 1; last_trace = data
                        except Exception: pass
                    elapsed = time.time() - t0
                    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
                    self._q.put(("point", elapsed, count, ts, last_trace))
        except Exception as e:
            self._q.put(("error", str(e)))
        self._running = False

    def _drain_queue(self):
        while not self._q.empty():
            msg = self._q.get_nowait()
            if msg[0] == "point":
                _, elapsed, count, ts, trace = msg
                self._data.append((elapsed, count, ts))
                if trace is not None: self._last_trace = trace
                if self._csv_writer:
                    self._csv_writer.writerow([ts, f"{elapsed:.3f}", count])
                    self._csv_file.flush()
                self._status.set(f"Last: {ts}  |  {count} counts in {self.integ_time.get():.1f} s")
                self._redraw()
            elif msg[0] == "error":
                self._status.set(f"Error: {msg[1]}"); self._running = False
        self.after(150, self._drain_queue)

    def _redraw(self):
        if not HAS_MPL or not self._data: return
        xs = [d[0] for d in self._data]; ys = [d[1] for d in self._data]
        self._ax_cr.clear(); _ax_style(self._ax_cr, "Elapsed time (s)", "Counts / window", "Count Rate vs Time")
        self._ax_cr.plot(xs, ys, color=ACCENT, linewidth=1.5, marker="o", markersize=3)
        self._ax_tr.clear(); _ax_style(self._ax_tr, "Sample", "Voltage (V)", "Most Recent Trigger Trace")
        if self._last_trace is not None: self._ax_tr.plot(self._last_trace, color=GREEN, linewidth=1)
        self._fig.tight_layout(pad=2); self._canvas.draw()


# ===========================================================================

# ===========================================================================
# TAB 6 – Count-Rate vs Current  (full rewrite)
# ===========================================================================

class CountRateCurrentTab(tk.Frame):
    """
    Sweeps current i_start → i_stop (UP) then i_stop → i_start (DOWN),
    repeated N times.

    Features:
      • X-axis uses actual current read from DMM, not the commanded value
      • Live temperature display; E-stop if T > TEMP_LIMIT_C mid-settle
      • Each plot updates point-by-point as the scan progresses
      • Per-pulse metrics (max, FWHM, noise floor avg, timestamp) saved to a
        separate CSV; written in a batch at the end of each integration window
        so no data is lost due to capture deadtime
      • Main CSV: scan / direction / measured_current / counts / temperature / ts
      • Degauss stub called before first scan
    """

    def __init__(self, parent):
        super().__init__(parent, bg=BG)
        self._running    = False
        self._thread     = None
        self._q          = queue.Queue()
        self._csv_file   = None
        self._csv_writer = None
        self._pulse_csv_file   = None
        self._pulse_csv_writer = None
        self._status     = tk.StringVar(value="Idle")
        self._scan_status= tk.StringVar(value="—")
        self._elapsed    = tk.StringVar(value="—")
        self._eta        = tk.StringVar(value="—")
        self._temp_disp  = tk.StringVar(value="—")
        self._curr_disp  = tk.StringVar(value="—")
        self._t_start    = None

        # Accumulated data – keyed by direction
        self._last_up    = []     # [(meas_current, count), ...]  current scan
        self._last_down  = []
        self._sum_up     = None   # np.ndarray of summed counts
        self._sum_down   = None
        self._cur_up     = None   # current axes matching sum arrays
        self._cur_down   = None

        self._build()
        self.after(150, self._drain_queue)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build(self):
        # ---- LEFT: scrollable settings panel ----
        left_sf = ScrollableFrame(self, bg=BG, width=315)
        left_sf.pack(side="left", fill="y")
        inn = left_sf.inner

        self.scope_cfg = ScopeSettingsFrame(inn)
        self.scope_cfg.pack(fill="x", padx=2, pady=4)

        sweep = tk.LabelFrame(inn, text="Current Sweep", bg=PANEL, fg=ACCENT, font=SANS_B)
        sweep.pack(fill="x", padx=2, pady=4)

        self.i_start       = tk.DoubleVar(value=0.0)
        self.i_stop        = tk.DoubleVar(value=0.1)
        self.i_step        = tk.DoubleVar(value=0.01)
        self.integ_time    = tk.DoubleVar(value=1.0)
        self.settling_time = tk.DoubleVar(value=0.5)
        self.n_scans       = tk.IntVar(value=1)
        self.psu_ch        = tk.StringVar(value="CH1")
        self.psu_host      = tk.StringVar(value="")
        self.save_csv      = tk.BooleanVar(value=False)
        self.csv_path      = tk.StringVar(value="")
        self.save_pulse_csv = tk.BooleanVar(value=False)
        self.pulse_csv_path = tk.StringVar(value="")
        # DMM VISA strings (same defaults as MultimeterReadoutTab)
        self.dmm_ammeter   = tk.StringVar(value="USB0::0x05E6::0x2110::1373999::INSTR")
        self.dmm_voltmeter = tk.StringVar(value="USB0::0x05E6::0x2110::1415286::INSTR")

        r = 0
        _lf(sweep, "PSU Channel:", col=0, row=r)
        ttk.OptionMenu(sweep, self.psu_ch, "CH1", "CH1", "CH2").grid(
            column=1, row=r, sticky="ew", padx=PADX, pady=PADY)
        r += 1; _lf(sweep, "PSU Host (blank=USB):", col=0, row=r); _ef(sweep, self.psu_host, col=1, row=r, width=14)
        r += 1; _lf(sweep, "I start (A):",          col=0, row=r); _ef(sweep, self.i_start,       col=1, row=r)
        r += 1; _lf(sweep, "I stop (A):",           col=0, row=r); _ef(sweep, self.i_stop,        col=1, row=r)
        r += 1; _lf(sweep, "I step / bin (A):",     col=0, row=r); _ef(sweep, self.i_step,        col=1, row=r)
        r += 1; _lf(sweep, "Integration Time (s):", col=0, row=r); _ef(sweep, self.integ_time,    col=1, row=r)
        r += 1; _lf(sweep, "Settling Time (s):",    col=0, row=r); _ef(sweep, self.settling_time, col=1, row=r)
        r += 1; _lf(sweep, "Number of scans (N):",  col=0, row=r); _ef(sweep, self.n_scans,       col=1, row=r)

        # DMM VISA addresses
        r += 1
        _lf(sweep, "Ammeter VISA:", col=0, row=r)
        _ef(sweep, self.dmm_ammeter, col=1, row=r, width=28)
        r += 1
        _lf(sweep, "Voltmeter VISA:", col=0, row=r)
        _ef(sweep, self.dmm_voltmeter, col=1, row=r, width=28)

        r += 1
        tk.Checkbutton(sweep, text="Save counts CSV", variable=self.save_csv,
                       command=self._toggle_csv_path,
                       bg=PANEL, fg=FG, selectcolor=ENTRY_BG,
                       activebackground=PANEL, font=SANS).grid(
            column=0, row=r, columnspan=2, sticky="w", padx=PADX, pady=PADY)
        r += 1
        self._csv_entry = tk.Entry(sweep, textvariable=self.csv_path, width=16,
                                   bg=ENTRY_BG, fg=FG, insertbackground=FG,
                                   font=MONO, state="disabled")
        self._csv_entry.grid(column=0, row=r, columnspan=2, sticky="ew", padx=PADX, pady=PADY)
        _btn(sweep, "Browse…", self._browse_csv, col=2, row=r)

        r += 1
        tk.Checkbutton(sweep, text="Save pulse metrics CSV", variable=self.save_pulse_csv,
                       command=self._toggle_pulse_csv_path,
                       bg=PANEL, fg=FG, selectcolor=ENTRY_BG,
                       activebackground=PANEL, font=SANS).grid(
            column=0, row=r, columnspan=2, sticky="w", padx=PADX, pady=PADY)
        r += 1
        self._pulse_csv_entry = tk.Entry(sweep, textvariable=self.pulse_csv_path, width=16,
                                         bg=ENTRY_BG, fg=FG, insertbackground=FG,
                                         font=MONO, state="disabled")
        self._pulse_csv_entry.grid(column=0, row=r, columnspan=2, sticky="ew", padx=PADX, pady=PADY)
        _btn(sweep, "Browse…", self._browse_pulse_csv, col=2, row=r)

        # ---- Live readouts ----
        live = tk.LabelFrame(inn, text="Live Hardware Readings", bg=PANEL, fg=ACCENT, font=SANS_B)
        live.pack(fill="x", padx=2, pady=4)

        tk.Label(live, text="Temperature:", bg=PANEL, fg=FG, font=SANS).grid(
            column=0, row=0, sticky="e", padx=PADX, pady=2)
        self._temp_lbl = tk.Label(live, textvariable=self._temp_disp,
                                  bg=PANEL, fg=GREEN, font=("Courier", 13, "bold"))
        self._temp_lbl.grid(column=1, row=0, sticky="w", padx=PADX)
        tk.Label(live, text="°C", bg=PANEL, fg=FG, font=SANS).grid(column=2, row=0, sticky="w")

        tk.Label(live, text="Measured Current:", bg=PANEL, fg=FG, font=SANS).grid(
            column=0, row=1, sticky="e", padx=PADX, pady=2)
        tk.Label(live, textvariable=self._curr_disp,
                 bg=PANEL, fg=ACCENT, font=("Courier", 13, "bold")).grid(
            column=1, row=1, sticky="w", padx=PADX)
        tk.Label(live, text="A", bg=PANEL, fg=FG, font=SANS).grid(column=2, row=1, sticky="w")

        # ---- Progress box ----
        prog = tk.LabelFrame(inn, text="Scan Progress", bg=PANEL, fg=ACCENT, font=SANS_B)
        prog.pack(fill="x", padx=2, pady=4)

        tk.Label(prog, text="State:", bg=PANEL, fg=FG, font=SANS).grid(
            column=0, row=0, sticky="ne", padx=PADX, pady=2)
        tk.Label(prog, textvariable=self._scan_status, bg=PANEL, fg=ACCENT,
                 font=SANS_B, anchor="w", wraplength=180, justify="left").grid(
            column=1, row=0, sticky="w", padx=PADX, pady=2)
        tk.Label(prog, text="Elapsed:", bg=PANEL, fg=FG, font=SANS).grid(
            column=0, row=1, sticky="e", padx=PADX, pady=2)
        tk.Label(prog, textvariable=self._elapsed, bg=PANEL, fg=GREEN,
                 font=MONO).grid(column=1, row=1, sticky="w", padx=PADX, pady=2)
        tk.Label(prog, text="Est. Remaining:", bg=PANEL, fg=FG, font=SANS).grid(
            column=0, row=2, sticky="e", padx=PADX, pady=2)
        tk.Label(prog, textvariable=self._eta, bg=PANEL, fg=YELLOW,
                 font=MONO).grid(column=1, row=2, sticky="w", padx=PADX, pady=2)

        btn_row = tk.Frame(inn, bg=BG)
        btn_row.pack(fill="x", padx=2, pady=4)
        tk.Button(btn_row, text="Start", command=self._start, bg=GREEN, fg=BG, font=SANS_B, padx=10, pady=4, relief="flat").pack(side="left", padx=6)
        tk.Button(btn_row, text="Stop",  command=self._stop,  bg=RED,   fg=BG, font=SANS_B, padx=10, pady=4, relief="flat").pack(side="left", padx=6)
        tk.Button(btn_row, text="Clear", command=self._clear, bg=BUTTON_BG, fg=FG, font=SANS_B, padx=10, pady=4, relief="flat").pack(side="left", padx=6)

        tk.Label(inn, textvariable=self._status, bg=BG, fg=YELLOW,
                 font=MONO, anchor="w", wraplength=290).pack(fill="x", padx=4, pady=2)

        # ---- RIGHT: scrollable plot panel ----
        right_sf = ScrollableFrame(self, bg=BG, width=760)
        right_sf.pack(side="left", fill="both", expand=True)

        if not HAS_MPL:
            tk.Label(right_sf.inner, text="matplotlib not installed",
                     bg=BG, fg=RED, font=SANS_LG).pack(expand=True)
            self._update_timer()
            return

        # Five stacked subplots in one tall figure
        self._fig = Figure(figsize=(6.5, 15), facecolor=BG)
        self._fig.subplots_adjust(hspace=0.42, top=0.97, bottom=0.03,
                                  left=0.13, right=0.97)
        axes = self._fig.subplots(5, 1)
        (self._ax_up_last, self._ax_up_sum,
         self._ax_dn_last, self._ax_dn_sum,
         self._ax_hyst) = axes

        _ax_style(self._ax_up_last, "Current (A)", "Counts",        "Latest Up Scan")
        _ax_style(self._ax_up_sum,  "Current (A)", "Summed Counts", "Sum – Up Scans")
        _ax_style(self._ax_dn_last, "Current (A)", "Counts",        "Latest Down Scan")
        _ax_style(self._ax_dn_sum,  "Current (A)", "Summed Counts", "Sum – Down Scans")
        _ax_style(self._ax_hyst,    "Current (A)", "Counts",        "Hysteresis (Up vs Down)")

        self._canvas = FigureCanvasTkAgg(self._fig, master=right_sf.inner)
        self._canvas.get_tk_widget().pack(fill="both", expand=True)

        self._update_timer()

    # ------------------------------------------------------------------
    # Stubs / helpers
    # ------------------------------------------------------------------

    def _degauss(self, ads, power_supply_ch, relay_ch=0):
        """
        Called once before the first scan begins.
        ads                – WaveFormsADS object.
        power_supply_ch    – PSU channel with .set_current() / .set_output().
        relay_ch           – ADS analog-out channel used for polarity relay.
        """
        NEG_RELAY_V = 5.0
        POS_RELAY_V = 0.0
        SETTLE      = 5

        ads.analog_out_reset()
        ads.analog_out_enable_node(channel=relay_ch)
        ads.analog_out_set_dc(channel=relay_ch, voltage_v=NEG_RELAY_V)
        power_supply_ch.set_current(0.6)
        time.sleep(SETTLE)
        power_supply_ch.set_current(0)
        time.sleep(SETTLE)
        ads.analog_out_set_dc(channel=relay_ch, voltage_v=POS_RELAY_V)

    def _toggle_csv_path(self):
        self._csv_entry.config(state="normal" if self.save_csv.get() else "disabled")

    def _toggle_pulse_csv_path(self):
        self._pulse_csv_entry.config(
            state="normal" if self.save_pulse_csv.get() else "disabled")

    def _browse_csv(self):
        path = filedialog.asksaveasfilename(defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All", "*.*")],
            title="Save counts CSV")
        if path: self.csv_path.set(path)

    def _browse_pulse_csv(self):
        path = filedialog.asksaveasfilename(defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All", "*.*")],
            title="Save pulse-metrics CSV")
        if path: self.pulse_csv_path.set(path)

    @staticmethod
    def _fmt_time(secs):
        secs = int(secs); h, rem = divmod(secs, 3600); m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    def _update_timer(self):
        if self._running and self._t_start is not None:
            self._elapsed.set(self._fmt_time(time.time() - self._t_start))
        self.after(500, self._update_timer)

    def _build_currents(self):
        currents = []
        v = self.i_start.get(); stop = self.i_stop.get(); step = self.i_step.get()
        while v <= stop + 1e-9:
            currents.append(round(v, 10)); v += step
        return currents

    @staticmethod
    def _pulse_metrics(trace: np.ndarray):
        """
        Return (peak_max, fwhm_samples, noise_floor_avg) for one triggered trace.
        noise_floor_avg = mean of the first 10% of samples (pre-trigger baseline).
        FWHM computed on the absolute waveform above half-max.
        """
        n       = len(trace)
        baseline= float(np.mean(trace[:max(1, n // 10)]))
        shifted = trace - baseline
        peak    = float(np.max(np.abs(shifted)))
        half    = peak / 2.0
        above   = np.abs(shifted) >= half
        # FWHM: number of samples where |shifted| >= half-max
        fwhm    = int(np.sum(above))
        return peak, fwhm, baseline

    # ------------------------------------------------------------------
    # Start / Stop / Clear
    # ------------------------------------------------------------------

    def _start(self):
        if self._running: return
        if not HAS_ADS: messagebox.showerror("Error", "waveforms_ads not available."); return
        if not HAS_PSU: messagebox.showerror("Error", "Power supply module not available."); return
        if not HAS_DMM: messagebox.showerror("Error", "temp_monitor (DMM) not available."); return

        if self.save_csv.get():
            path = self.csv_path.get().strip()
            if not path: messagebox.showerror("Error", "Choose a counts CSV path first."); return
            self._csv_file   = open(path, "w", newline="")
            self._csv_writer = csv.writer(self._csv_file)
            self._csv_writer.writerow(
                ["Scan", "Direction", "Measured_Current_A",
                 "Counts", "Temperature_C", "Timestamp"])

        if self.save_pulse_csv.get():
            path = self.pulse_csv_path.get().strip()
            if not path: messagebox.showerror("Error", "Choose a pulse-metrics CSV path first."); return
            self._pulse_csv_file   = open(path, "w", newline="")
            self._pulse_csv_writer = csv.writer(self._pulse_csv_file)
            self._pulse_csv_writer.writerow(
                ["Scan", "Direction", "Measured_Current_A",
                 "Pulse_Timestamp", "Peak_V", "FWHM_samples", "Noise_Floor_V"])

        self._running = True; self._t_start = time.time()
        self._status.set("Running…")
        self._thread = threading.Thread(target=self._sweep_loop, daemon=True)
        self._thread.start()

    def _stop(self):
        self._running = False
        for f in (self._csv_file, self._pulse_csv_file):
            if f:
                try: f.close()
                except Exception: pass
        self._csv_file = self._csv_writer = None
        self._pulse_csv_file = self._pulse_csv_writer = None
        self._status.set("Stopped"); self._scan_status.set("Stopped")

    def _emergency_stop(self, ch, reason=""):
        """Turn PSU off immediately and signal the main thread."""
        try: ch.set_output(False)
        except Exception: pass
        self._running = False
        self._q.put(("estop", reason))

    def _clear(self):
        self._last_up = []; self._last_down = []
        self._sum_up  = None; self._sum_down  = None
        self._cur_up  = None; self._cur_down  = None
        self._redraw()

    # ------------------------------------------------------------------
    # Sweep loop (background thread)
    # ------------------------------------------------------------------

    def _sweep_loop(self):
        n_scans  = max(1, self.n_scans.get())
        integ    = self.integ_time.get()
        settle   = self.settling_time.get()
        p        = self.scope_cfg.get_params()
        att      = -1 if p["invert"] else 1

        currents_up   = self._build_currents()
        currents_down = list(reversed(currents_up))
        n_half        = len(currents_up)
        n_total       = n_scans * 2 * n_half

        host = self.psu_host.get().strip()
        try:
            psu_ctx = SPD3303X.ethernet_device(host) if host else SPD3303X.usb_device()
            with psu_ctx as psu:
                ch = getattr(psu, self.psu_ch.get())
                ch.set_output(True)

                # Open DMM for temperature and current readback
                dmm = TempMonitor(
                    ammeter  = self.dmm_ammeter.get().strip(),
                    voltmeter= self.dmm_voltmeter.get().strip(),
                )

                with WaveFormsADS() as dev:
                    dev.analog_in_set_range(p["channel"], p["y_range"])
                    steps_done = 0

                    # --- Degauss ---
                    self._q.put(("status", "Degaussing…"))
                    self._degauss(ads=dev, power_supply_ch=ch, relay_ch=0)

                    for scan_idx in range(n_scans):
                        if not self._running: break

                        for direction, currents in (("up",   currents_up),
                                                    ("down", currents_down)):
                            if not self._running: break
                            label = f"Scan {scan_idx+1}/{n_scans} – {direction}"
                            self._q.put(("status", label))

                            for cmd_current in currents:
                                if not self._running: break
                                ch.set_current(cmd_current)

                                # ---- Settling phase with mid-point temp check ----
                                if settle > 0:
                                    half_settle = settle / 2.0
                                    self._q.put(("status",
                                        f"{label} | Settling {settle:.1f} s "
                                        f"(cmd={cmd_current:.4f} A)"))
                                    time.sleep(half_settle)

                                    # Temperature check at midpoint
                                    try:
                                        t_check = dmm.measure_temperature()
                                        t_spike = dmm.temp_spike
                                        self._q.put(("temp_check", t_check))
                                        if t_check > TEMP_LIMIT_C and not t_spike:
                                            self._emergency_stop(ch,
                                                f"TEMPERATURE LIMIT EXCEEDED: "
                                                f"{t_check:.1f}°C > {TEMP_LIMIT_C}°C")
                                            return
                                    except Exception as e:
                                        self._q.put(("status",
                                            f"{label} | Temp check failed: {e}"))

                                    time.sleep(half_settle)

                                # ---- Read actual current from DMM ----
                                try:
                                    meas_current = dmm.measure_current()
                                    meas_temp    = dmm.measure_temperature()
                                except Exception:
                                    meas_current = cmd_current   # fallback
                                    meas_temp    = float("nan")

                                self._q.put(("live", meas_current, meas_temp))
                                self._q.put(("status",
                                    f"{label} | Integrating {integ:.1f} s "
                                    f"(I={meas_current:.4f} A, T={meas_temp:.1f}°C)"))

                                # ---- Integration window ----
                                count = 0
                                # Pulse metrics accumulated during window;
                                # written in batch afterwards to avoid deadtime
                                pulse_batch = []   # [(ts, peak, fwhm, noise), ...]
                                t_win = time.time()

                                while time.time() - t_win < integ and self._running:
                                    try:
                                        data = dev.analog_in_capture(
                                            channel=p["channel"],
                                            sample_rate_hz=p["sample_rate"],
                                            buffer_size=p["buffer_size"],
                                            trigger_level_v=p["trigger_level"],
                                            trigger_condition=p["slope"],
                                            y_offset=p["y_offset"],
                                            y_range=p["y_range"],
                                            attenuation=att,
                                            auto_timeout_s=0.0,
                                            timeout_s=max(integ * 2, 1.0))
                                        count += 1
                                        pulse_ts = datetime.datetime.now().strftime(
                                            "%H:%M:%S.%f")[:-3]
                                        peak, fwhm, noise = self._pulse_metrics(data)
                                        pulse_batch.append(
                                            (pulse_ts, peak, fwhm, noise))
                                    except Exception:
                                        pass

                                # ---- Write pulse CSV batch ----
                                if self._pulse_csv_writer and pulse_batch:
                                    for pts, pk, fw, ns in pulse_batch:
                                        self._pulse_csv_writer.writerow([
                                            scan_idx + 1, direction,
                                            f"{meas_current:.6f}",
                                            pts, f"{pk:.6f}",
                                            fw, f"{ns:.6f}"])
                                    self._pulse_csv_file.flush()

                                steps_done += 1
                                elapsed = time.time() - self._t_start
                                if steps_done:
                                    remain = (elapsed / steps_done) * (n_total - steps_done)
                                    self._q.put(("eta", remain))

                                ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
                                self._q.put(("point", direction, scan_idx,
                                             meas_current, count, meas_temp, ts))

                            # Full half-scan done
                            self._q.put(("scan_done", direction))

                ch.set_output(False)

        except Exception as e:
            self._q.put(("error", str(e)))

        self._running = False
        self._q.put(("status", "Finished"))

    # ------------------------------------------------------------------
    # Queue drain (main thread, every 150 ms)
    # ------------------------------------------------------------------

    def _drain_queue(self):
        redraw = False
        while not self._q.empty():
            msg = self._q.get_nowait()

            if msg[0] == "status":
                self._scan_status.set(msg[1])

            elif msg[0] == "live":
                _, meas_i, meas_t = msg
                self._curr_disp.set(f"{meas_i:.5f}")
                self._temp_disp.set(f"{meas_t:.2f}")
                if meas_t >= TEMP_LIMIT_C:
                    self._temp_lbl.config(fg=RED)
                elif meas_t >= TEMP_LIMIT_C - 10:
                    self._temp_lbl.config(fg=YELLOW)
                else:
                    self._temp_lbl.config(fg=GREEN)

            elif msg[0] == "temp_check":
                # Mid-settle temperature update
                _, t = msg
                self._temp_disp.set(f"{t:.2f}")
                self._temp_lbl.config(fg=(RED if t >= TEMP_LIMIT_C
                                         else YELLOW if t >= TEMP_LIMIT_C - 10
                                         else GREEN))

            elif msg[0] == "point":
                _, direction, scan_idx, meas_i, count, meas_t, ts = msg

                # Write counts CSV
                if self._csv_writer:
                    self._csv_writer.writerow(
                        [scan_idx + 1, direction, f"{meas_i:.6f}",
                         count, f"{meas_t:.2f}", ts])
                    self._csv_file.flush()

                self._status.set(
                    f"{direction.upper()} | I={meas_i:.4f} A "
                    f"T={meas_t:.1f}°C → {count} counts")

                # Append to live scan list and redraw immediately (point-by-point)
                if direction == "up":
                    self._last_up.append((meas_i, count))
                else:
                    self._last_down.append((meas_i, count))
                redraw = True

            elif msg[0] == "scan_done":
                _, direction = msg
                # Accumulate into sum arrays when a full half-scan completes
                if direction == "up" and self._last_up:
                    arr_i = np.array([d[0] for d in self._last_up])
                    arr_c = np.array([d[1] for d in self._last_up], dtype=float)
                    self._cur_up  = arr_i
                    self._sum_up  = (arr_c.copy() if self._sum_up is None
                                     or len(self._sum_up) != len(arr_c)
                                     else self._sum_up + arr_c)
                    self._last_up = []   # reset for next scan

                elif direction == "down" and self._last_down:
                    arr_i = np.array([d[0] for d in self._last_down])
                    arr_c = np.array([d[1] for d in self._last_down], dtype=float)
                    self._cur_down  = arr_i
                    self._sum_down  = (arr_c.copy() if self._sum_down is None
                                       or len(self._sum_down) != len(arr_c)
                                       else self._sum_down + arr_c)
                    self._last_down = []

                redraw = True

            elif msg[0] == "eta":
                self._eta.set(self._fmt_time(msg[1]))

            elif msg[0] == "estop":
                _, reason = msg
                self._status.set(f"EMERGENCY STOP: {reason}")
                self._scan_status.set("⚠ STOPPED")
                self._running = False
                messagebox.showerror("Emergency Stop", reason)

            elif msg[0] == "error":
                self._status.set(f"Error: {msg[1]}")
                self._scan_status.set("Error")
                self._running = False

        if redraw:
            self._redraw()

        self.after(150, self._drain_queue)

    # ------------------------------------------------------------------
    # Plotting
    # ------------------------------------------------------------------

    def _plot_single(self, ax, xs, ys, color, title, fill=True):
        ax.clear(); _ax_style(ax, "Current (A)", "Counts", title)
        if xs is not None and len(xs):
            ax.plot(xs, ys, color=color, linewidth=1.5, marker="o", markersize=3)
            if fill:
                ax.fill_between(xs, ys, alpha=0.12, color=color)

    def _redraw(self):
        if not HAS_MPL: return

        # ---- Latest up (may be in-progress) ----
        up_data = self._last_up   # live accumulation during scan
        if not up_data and self._sum_up is not None:
            # scan_done was received – show nothing for "latest" until next scan starts
            up_data = []
        if up_data:
            xs = np.array([d[0] for d in up_data])
            ys = np.array([d[1] for d in up_data], dtype=float)
            self._plot_single(self._ax_up_last, xs, ys, ACCENT, "Latest Up Scan")
        else:
            self._plot_single(self._ax_up_last, None, None, ACCENT, "Latest Up Scan")

        # ---- Sum up ----
        if self._sum_up is not None and self._cur_up is not None:
            self._plot_single(self._ax_up_sum, self._cur_up, self._sum_up,
                              GREEN, "Sum – Up Scans")
        else:
            self._plot_single(self._ax_up_sum, None, None, GREEN, "Sum – Up Scans")

        # ---- Latest down (may be in-progress) ----
        dn_data = self._last_down
        if dn_data:
            xs = np.array([d[0] for d in dn_data])
            ys = np.array([d[1] for d in dn_data], dtype=float)
            self._plot_single(self._ax_dn_last, xs, ys, PURPLE, "Latest Down Scan")
        else:
            self._plot_single(self._ax_dn_last, None, None, PURPLE, "Latest Down Scan")

        # ---- Sum down ----
        if self._sum_down is not None and self._cur_down is not None:
            self._plot_single(self._ax_dn_sum, self._cur_down, self._sum_down,
                              YELLOW, "Sum – Down Scans")
        else:
            self._plot_single(self._ax_dn_sum, None, None, YELLOW, "Sum – Down Scans")

        # ---- Hysteresis: last completed up + last completed down ----
        self._ax_hyst.clear()
        _ax_style(self._ax_hyst, "Current (A)", "Counts", "Hysteresis (Up vs Down)")
        handles = []

        # Use sum arrays as the "last complete" reference for the hysteresis plot
        if self._sum_up is not None and self._cur_up is not None:
            l, = self._ax_hyst.plot(self._cur_up, self._sum_up / max(1, self._sum_up.max()) * 1,
                                     color=ACCENT, linewidth=1.5, marker="o",
                                     markersize=3, label="Up (last complete)")
            handles.append(l)
        if self._sum_down is not None and self._cur_down is not None:
            xs_d = self._cur_down
            ys_d = self._sum_down
            # Sort by ascending current for left→right line
            order = np.argsort(xs_d)
            l, = self._ax_hyst.plot(xs_d[order], ys_d[order],
                                     color=PURPLE, linewidth=1.5,
                                     marker="s", markersize=3,
                                     linestyle="--", label="Down (last complete)")
            handles.append(l)
        if handles:
            self._ax_hyst.legend(handles=handles, fontsize=8,
                                  facecolor=PANEL, labelcolor=FG)

        self._canvas.draw()

# ===========================================================================
# TAB REGISTRY – add new tabs here
# ===========================================================================

TABS = [
    ("Power Supply",          PowerSupplyTab),
    ("Scope Viewer",          ScopeViewerTab),
    ("Digital Output",        DigitalOutputTab),
    ("Wavegen Control",       WavegenControlTab),
    ("Multimeter Readout",    MultimeterReadoutTab),
    ("Count Rate vs Time",    CountRateTimeTab),
    ("Count Rate vs Current", CountRateCurrentTab),
    # ("My New Tab",           MyNewTabClass),
]


# ===========================================================================
# Main application window
# ===========================================================================

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Beta Ray Spectroscopy Controller")
        self.configure(bg=BG)
        self.geometry("1200x750")
        self._apply_style()
        self._build_ui()

    def _apply_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TNotebook",     background=BG,    borderwidth=0)
        style.configure("TNotebook.Tab", background=PANEL, foreground=FG,
                        padding=[12, 5], font=SANS_B)
        style.map("TNotebook.Tab",
                  background=[("selected", ACCENT)],
                  foreground=[("selected", BG)])
        style.configure("TMenubutton",           background=BUTTON_BG, foreground=FG,
                        relief="flat", font=SANS)
        style.configure("Vertical.TScrollbar",   background=BUTTON_BG,
                        troughcolor=PANEL, arrowcolor=FG)

    def _build_ui(self):
        hdr = tk.Frame(self, bg=ACCENT, height=36)
        hdr.pack(fill="x")
        tk.Label(hdr, text="  Beta Ray Spectroscopy Controller",
                 bg=ACCENT, fg=BG,
                 font=("Helvetica", 13, "bold")).pack(side="left", pady=4)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=4, pady=4)
        for name, Cls in TABS:
            nb.add(Cls(nb), text=name)


if __name__ == "__main__":
    app = App()
    app.mainloop()