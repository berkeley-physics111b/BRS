"""
lab_controller.py
=================
Tkinter-based laboratory measurement GUI with five tabs:
  1. Power Supply Control   – SPD3303X channel control & monitoring
  2. Scope Viewer           – ADS triggered capture + past-N-traces display
  3. Digital Output         – ADS digital pin high/low toggle
  4. Count-Rate vs Time     – Count triggers over an integration window; log to CSV
  5. Count-Rate vs Current  – Sweep current, count triggers; log to CSV

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

# Lazy imports so the app opens even without hardware attached
try:
    from spd3303c_power_supply import SPD3303X
    HAS_PSU = True
    HAS_PSU_SIGILENT = True
except Exception as e:
    print("Power supply SPD3303C package connection error:", e)
    HAS_PSU = False
    HAS_PSU_SIGILENT = False

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
    from e363xa_power_supply import *
    HAS_PSU = True
    HAS_PSU_KEYSIGHT = True
except Exception as e:
    print("Power supply E3633A package connection error:", e)
    HAS_PSU = False
    HAS_PSU_KEYSIGHT = False


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
    """Quick label factory."""
    lbl = tk.Label(parent, text=text, bg=PANEL, fg=FG, font=SANS, **kw)
    lbl.grid(column=col, row=row, sticky=sticky, padx=PADX, pady=PADY,
             columnspan=colspan)
    return lbl


def _ef(parent, textvariable, col=1, row=0, width=10, colspan=1):
    """Quick entry factory."""
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
    """Section divider label."""
    lbl = tk.Label(parent, text=f"  {title}  ", bg=ACCENT, fg=BG,
                   font=SANS_B)
    lbl.grid(column=0, row=row, columnspan=colspan, sticky="ew",
             padx=PADX, pady=(12, 2))
    return lbl


def _status_bar(parent, var, row=99, colspan=10):
    lbl = tk.Label(parent, textvariable=var, bg=BG, fg=YELLOW,
                   font=MONO, anchor="w")
    lbl.pack(side="bottom", fill="x")
    #lbl.grid(column=0, row=row, columnspan=colspan, sticky="ew", padx=4, pady=2)
    return lbl


# ---------------------------------------------------------------------------
# Shared scope-settings frame (reused across tabs)
# ---------------------------------------------------------------------------

class ScopeSettingsFrame(tk.LabelFrame):
    """
    Reusable frame that exposes scope controls:
      trigger_level, edge (rise/fall), sample_freq_hz,
      y_range_v, time_base_us (sets buffer_size), probe_invert, channel.
    """

    def __init__(self, parent, **kw):
        super().__init__(parent, text="Scope Settings", bg=PANEL, fg=ACCENT,
                         font=SANS_B, **kw)
        self._build()

    def _build(self):
        self.trig_level   = tk.DoubleVar(value=0.15)
        self.edge         = tk.StringVar(value="Rise")
        self.sample_freq  = tk.DoubleVar(value=1e8)
        self.y_range      = tk.DoubleVar(value=0.1)
        self.y_offset       = tk.DoubleVar(value=0.0)
        self.time_base_us = tk.DoubleVar(value=0.1)
        self.probe_invert = tk.BooleanVar(value=True)
        self.channel      = tk.IntVar(value=0)

        r = 0
        _lf(self, "Channel (0-based):", col=0, row=r)
        _ef(self, self.channel, col=1, row=r, width=5)

        r += 1
        _lf(self, "Trigger Level (V):", col=0, row=r)
        _ef(self, self.trig_level, col=1, row=r, width=8)

        r += 1
        _lf(self, "Edge:", col=0, row=r)
        om = ttk.OptionMenu(self, self.edge, "Rise", "Rise", "Fall")
        om.configure(style="TMenubutton")
        om.grid(column=1, row=r, sticky="ew", padx=PADX, pady=PADY)

        r += 1
        _lf(self, "Sample Freq (Hz):", col=0, row=r)
        _ef(self, self.sample_freq, col=1, row=r, width=10)

        r += 1
        _lf(self, "Y Range (V/div):", col=0, row=r)
        _ef(self, self.y_range, col=1, row=r, width=8)

        r += 1
        _lf(self, "Vertical Offset (V):", col=0, row=r)
        _ef(self, self.y_offset, col=1, row=r, width=8)

        r += 1
        _lf(self, "Time Base (μs):", col=0, row=r)
        _ef(self, self.time_base_us, col=1, row=r, width=8)

        r += 1
        cb = tk.Checkbutton(self, text="Invert Probe", variable=self.probe_invert,
                            bg=PANEL, fg=FG, selectcolor=ENTRY_BG,
                            activebackground=PANEL, font=SANS)
        cb.grid(column=0, row=r, columnspan=2, sticky="w", padx=PADX, pady=PADY)

    def get_params(self):
        fs = self.sample_freq.get()
        #tb = self.time_base_us.get() / 1000.0          # seconds
        tb = self.time_base_us.get() / 1e6          # seconds
        buffer_size = max(64, int(fs * tb))
        slope = DwfTriggerSlopeRise if self.edge.get() == "Rise" else DwfTriggerSlopeFall
        return dict(
            channel       = self.channel.get(),
            trigger_level = self.trig_level.get(),
            slope         = slope,
            sample_rate   = fs,
            y_range       = self.y_range.get(),
            y_offset      = self.y_offset.get(),
            time_base_us  = self.time_base_us.get(),
            buffer_size   = buffer_size,
            invert        = self.probe_invert.get(),
        )


# ===========================================================================
# TAB 1 – Power Supply
# ===========================================================================

class PowerSupplyTab(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent, bg=BG)
        self._psu  = None
        self._chan  = None
        self._poll_id = None
        self._status = tk.StringVar(value="Not connected")
        self._build()

    def _build(self):
        pad = dict(padx=PADX, pady=PADY)

        # ---- Connection ----
        conn_frame = tk.LabelFrame(self, text="Connection", bg=PANEL, fg=ACCENT,
                                   font=SANS_B)
        conn_frame.pack(fill="x", padx=10, pady=8)

        self.conn_type = tk.StringVar(value="USB")
        tk.Label(conn_frame, text="Interface:", bg=PANEL, fg=FG, font=SANS).grid(
            column=0, row=0, **pad, sticky="e")
        ttk.OptionMenu(conn_frame, self.conn_type, "USB", "USB", "Ethernet").grid(
            column=1, row=0, **pad, sticky="ew")

        self.host_var = tk.StringVar(value="192.168.1.100")
        tk.Label(conn_frame, text="Host (Ethernet):", bg=PANEL, fg=FG,
                 font=SANS).grid(column=2, row=0, **pad, sticky="e")
        tk.Entry(conn_frame, textvariable=self.host_var, width=16,
                 bg=ENTRY_BG, fg=FG, insertbackground=FG, font=MONO).grid(
            column=3, row=0, **pad, sticky="ew")

        _btn(conn_frame, "Connect", self._connect, col=4, row=0, bg=GREEN, fg=BG)
        _btn(conn_frame, "Disconnect", self._disconnect, col=5, row=0, bg=RED, fg=BG)

        # ---- Channel selection ----
        ctrl_frame = tk.LabelFrame(self, text="Channel Control", bg=PANEL,
                                   fg=ACCENT, font=SANS_B)
        ctrl_frame.pack(fill="x", padx=10, pady=4)

        self.ch_var = tk.StringVar(value="CH1")
        tk.Label(ctrl_frame, text="Channel:", bg=PANEL, fg=FG,
                 font=SANS).grid(column=0, row=0, **pad, sticky="e")
        ttk.OptionMenu(ctrl_frame, self.ch_var, "CH1", "CH1", "CH2").grid(
            column=1, row=0, **pad, sticky="ew")

        # ---- Set voltage / current ----
        self.volt_set = tk.DoubleVar(value=3.3)
        self.curr_set = tk.DoubleVar(value=0.5)

        tk.Label(ctrl_frame, text="Set Voltage (V):", bg=PANEL, fg=FG,
                 font=SANS).grid(column=0, row=1, **pad, sticky="e")
        tk.Entry(ctrl_frame, textvariable=self.volt_set, width=10,
                 bg=ENTRY_BG, fg=FG, insertbackground=FG, font=MONO).grid(
            column=1, row=1, **pad, sticky="ew")
        _btn(ctrl_frame, "Apply Voltage", self._set_voltage, col=2, row=1)

        tk.Label(ctrl_frame, text="Set Current (A):", bg=PANEL, fg=FG,
                 font=SANS).grid(column=0, row=2, **pad, sticky="e")
        tk.Entry(ctrl_frame, textvariable=self.curr_set, width=10,
                 bg=ENTRY_BG, fg=FG, insertbackground=FG, font=MONO).grid(
            column=1, row=2, **pad, sticky="ew")
        _btn(ctrl_frame, "Apply Current", self._set_current, col=2, row=2)

        # ---- Output on/off ----
        btn_row = tk.Frame(ctrl_frame, bg=PANEL)
        btn_row.grid(column=0, row=3, columnspan=4, **pad, sticky="ew")
        tk.Button(btn_row, text="Output ON", command=self._output_on,
                  bg=GREEN, fg=BG, font=SANS_B, padx=10, pady=4,
                  relief="flat").pack(side="left", padx=6)
        tk.Button(btn_row, text="Output OFF", command=self._output_off,
                  bg=RED, fg=BG, font=SANS_B, padx=10, pady=4,
                  relief="flat").pack(side="left", padx=6)

        # ---- Readings ----
        meas_frame = tk.LabelFrame(self, text="Live Measurements", bg=PANEL,
                                   fg=ACCENT, font=SANS_B)
        meas_frame.pack(fill="x", padx=10, pady=4)

        self.meas_volt = tk.StringVar(value="—")
        self.meas_curr = tk.StringVar(value="—")

        tk.Label(meas_frame, text="Measured Voltage:", bg=PANEL, fg=FG,
                 font=SANS).grid(column=0, row=0, **pad, sticky="e")
        tk.Label(meas_frame, textvariable=self.meas_volt, bg=PANEL,
                 fg=GREEN, font=("Courier", 14, "bold")).grid(
            column=1, row=0, **pad, sticky="w")

        tk.Label(meas_frame, text="Measured Current:", bg=PANEL, fg=FG,
                 font=SANS).grid(column=0, row=1, **pad, sticky="e")
        tk.Label(meas_frame, textvariable=self.meas_curr, bg=PANEL,
                 fg=GREEN, font=("Courier", 14, "bold")).grid(
            column=1, row=1, **pad, sticky="w")

        _btn(meas_frame, "Poll Once", self._poll_once, col=2, row=0)

        # Status bar
        _status_bar(self, self._status, row=99)

    # ---- helpers ----
    def _get_chan(self):
        if self._psu is None:
            messagebox.showerror("Error", "Not connected to power supply.")
            return None
        name = self.ch_var.get()
        return getattr(self._psu, name, None)

    def _connect(self):
        if not HAS_PSU:
            messagebox.showerror("Error", "spd3303c_power_supply module not available.")
            return
        try:
            if self.conn_type.get() == "USB":
                dev = SPD3303X.usb_device().__enter__()
            else:
                dev = SPD3303X.ethernet_device(self.host_var.get()).__enter__()
            self._psu = dev
            self._status.set("Connected to power supply")
            self._start_poll()
        except Exception as e:
            messagebox.showerror("Connection Error", str(e))

    def _disconnect(self):
        self._stop_poll()
        if self._psu:
            try:
                self._psu._inst.close()
            except Exception:
                pass
            self._psu = None
        self._status.set("Disconnected")
        self.meas_volt.set("—")
        self.meas_curr.set("—")

    def _set_voltage(self):
        ch = self._get_chan()
        if ch:
            try:
                ch.set_voltage(self.volt_set.get())
                self._status.set(f"Voltage set to {self.volt_set.get():.3f} V on {self.ch_var.get()}")
            except Exception as e:
                messagebox.showerror("Error", str(e))

    def _set_current(self):
        ch = self._get_chan()
        if ch:
            try:
                ch.set_current(self.curr_set.get())
                self._status.set(f"Current limit set to {self.curr_set.get():.3f} A on {self.ch_var.get()}")
            except Exception as e:
                messagebox.showerror("Error", str(e))

    def _output_on(self):
        ch = self._get_chan()
        if ch:
            try:
                ch.set_output(True)
                self._status.set(f"{self.ch_var.get()} output ON")
            except Exception as e:
                messagebox.showerror("Error", str(e))

    def _output_off(self):
        ch = self._get_chan()
        if ch:
            try:
                ch.set_output(False)
                self._status.set(f"{self.ch_var.get()} output OFF")
            except Exception as e:
                messagebox.showerror("Error", str(e))

    def _poll_once(self):
        ch = self._get_chan()
        if ch and hasattr(ch, "measure_voltage"):
            try:
                v = ch.measure_voltage()
                i = ch.measure_current()
                self.meas_volt.set(f"{float(v):.4f} V")
                self.meas_curr.set(f"{float(i):.4f} A")
            except Exception as e:
                self._status.set(f"Poll error: {e}")

    def _start_poll(self):
        self._do_poll()

    def _do_poll(self):
        self._poll_once()
        self._poll_id = self.after(1000, self._do_poll)

    def _stop_poll(self):
        if self._poll_id:
            self.after_cancel(self._poll_id)
            self._poll_id = None


# ===========================================================================
# TAB 2 – Scope Viewer
# ===========================================================================

class ScopeViewerTab(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent, bg=BG)
        self._running   = False
        self._thread    = None
        self._q         = queue.Queue()
        self._traces    = []          # list of (timestamp_str, np.array)
        self._status    = tk.StringVar(value="Idle")
        self._last_ts   = tk.StringVar(value="—")
        self._build()
        self.after(100, self._drain_queue)

    def _build(self):
        # Left: scope settings + controls
        left = tk.Frame(self, bg=BG)
        left.pack(side="left", fill="y", padx=6, pady=6)

        self.scope_cfg = ScopeSettingsFrame(left)
        self.scope_cfg.pack(fill="x", pady=4)

        ctrl = tk.LabelFrame(left, text="Capture Control", bg=PANEL,
                             fg=ACCENT, font=SANS_B)
        ctrl.pack(fill="x", pady=4)

        self.n_traces = tk.IntVar(value=3)
        _lf(ctrl, "Show last N traces:", col=0, row=0)
        _ef(ctrl, self.n_traces, col=1, row=0, width=5)

        _btn(ctrl, "Start", self._start, col=0, row=1, bg=GREEN, fg=BG)
        _btn(ctrl, "Stop",  self._stop,  col=1, row=1, bg=RED,   fg=BG)

        tk.Label(ctrl, text="Last trigger:", bg=PANEL, fg=FG,
                 font=SANS).grid(column=0, row=2, sticky="e", padx=PADX)
        tk.Label(ctrl, textvariable=self._last_ts, bg=PANEL, fg=YELLOW,
                 font=MONO).grid(column=1, row=2, sticky="w", padx=PADX)

        _status_bar(left, self._status, row=99)

        # Plot y scale parameters
        p = self.scope_cfg.get_params()
        y_range = p["y_range"]
        x_range = p["time_base_us"]
        NUM_DIVS_Y = 5
        NUM_DIVS_X = 5
        scale_min = -NUM_DIVS_Y * y_range
        scale_max = NUM_DIVS_Y * y_range
        time_min = -NUM_DIVS_X * x_range
        time_max = NUM_DIVS_X * x_range

        # Right: plot
        right = tk.Frame(self, bg=BG)
        right.pack(side="left", fill="both", expand=True, padx=4, pady=6)

        if HAS_MPL:
            self._fig = Figure(figsize=(6, 4), facecolor=BG)
            self._ax  = self._fig.add_subplot(111)
            self._ax.set_facecolor(PANEL)
            self._ax.tick_params(colors=FG)
            for sp in self._ax.spines.values():
                sp.set_edgecolor(FG)
            self._ax.set_xlabel("Time (ms)", color=FG)
            self._ax.set_ylabel("Voltage (V)", color=FG)
            self._ax.set_title("Scope Traces", color=ACCENT)
            self._ax.set_ylim(bottom=scale_min, top=scale_max)
            self._ax.set_xlim(left=time_min, right=time_max)
            self._canvas = FigureCanvasTkAgg(self._fig, master=right)
            self._canvas.get_tk_widget().pack(fill="both", expand=True)
        else:
            tk.Label(right, text="matplotlib not installed",
                     bg=BG, fg=RED, font=SANS_LG).pack(expand=True)

    def _start(self):
        if self._running:
            return
        if not HAS_ADS:
            messagebox.showerror("Error", "waveforms_ads module not available.")
            return
        self._running = True
        self._status.set("Running…")
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def _stop(self):
        self._running = False
        self._status.set("Stopped")

    def _capture_loop(self):
        p = self.scope_cfg.get_params()
        # User inputting settings assuming trigger on inverted data; modify analog_in_capture accordingly
        if p["invert"]:
            trigger_slope = (p["slope"] + 1)%2
            trigger_level = -p["trigger_level"]
        else:
            trigger_slope = p["slope"]
            trigger_level = p["trigger_level"]
        try:
            with WaveFormsADS() as dev:
                dev.analog_in_set_range(p["channel"], p["y_range"])
                while self._running:
                    data = dev.analog_in_capture(
                        channel          = p["channel"],
                        sample_rate_hz   = p["sample_rate"],
                        buffer_size      = p["buffer_size"],
                        trigger_level_v  = trigger_level,
                        trigger_condition= trigger_slope,
                        y_range          = p["y_range"],
                        y_offset         = p["y_offset"],
                        auto_timeout_s   = 0.0, #might need to change to 0?
                        timeout_s        = 2.0,
                    )
                    if p["invert"]:
                        data = -data
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
                n = max(1, self.n_traces.get())
                self._traces = self._traces[-n:]
                self._redraw()
            elif msg[0] == "error":
                self._status.set(f"Error: {msg[1]}")
                self._running = False
        self.after(100, self._drain_queue)

    def _redraw(self):
        p = self.scope_cfg.get_params()
        # Plot scale parameters
        y_range = p["y_range"]
        x_range = p["time_base_us"]
        NUM_DIVS_Y = 5
        NUM_DIVS_X = 5
        scale_min = -NUM_DIVS_Y * y_range
        scale_max = NUM_DIVS_Y * y_range
        time_min = -NUM_DIVS_X * x_range
        time_max = NUM_DIVS_X * x_range
        if not HAS_MPL:
            return
        self._ax.clear()
        self._ax.set_facecolor(PANEL)
        colors = [ACCENT, GREEN, YELLOW, RED, "#cba6f7"]
        for i, (ts, data) in enumerate(self._traces):
            col = colors[i % len(colors)]
            alpha = 0.4 + 0.6 * (i + 1) / len(self._traces)
            time_vals = np.array(range(len(data)))/p["sample_rate"]
            self._ax.plot(time_vals, data, color=col, alpha=alpha, linewidth=1,
                          label=ts)
        self._ax.axhline(y=p["trigger_level"], linestyle='--', label="Trigger")
        self._ax.set_xlabel("Time (ms)", color=FG)
        self._ax.set_ylabel("Voltage (V)", color=FG)
        self._ax.set_title("Scope Traces", color=ACCENT)
        self._ax.set_ylim(bottom=scale_min, top=scale_max)
        self._ax.set_xlim(left=time_min, right=time_max)
        self._ax.tick_params(colors=FG)
        self._ax.legend(fontsize=7, facecolor=PANEL, labelcolor=FG, loc='upper right')
        self._canvas.draw()


# ===========================================================================
# TAB 3 – Digital Output
# ===========================================================================

class DigitalOutputTab(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent, bg=BG)
        self._ads    = None
        self._status = tk.StringVar(value="Not connected")
        self._level  = tk.StringVar(value="LOW")
        self._build()

    def _build(self):
        pad = dict(padx=PADX, pady=PADY)

        conn = tk.LabelFrame(self, text="ADS Connection", bg=PANEL,
                             fg=ACCENT, font=SANS_B)
        conn.pack(fill="x", padx=10, pady=8)

        _btn(conn, "Connect ADS",    self._connect,    col=0, row=0, bg=GREEN, fg=BG)
        _btn(conn, "Disconnect ADS", self._disconnect, col=1, row=0, bg=RED,   fg=BG)

        ctrl = tk.LabelFrame(self, text="Digital Output Control", bg=PANEL,
                             fg=ACCENT, font=SANS_B)
        ctrl.pack(fill="x", padx=10, pady=4)

        self.pin_var = tk.IntVar(value=0)
        tk.Label(ctrl, text="Pin (0-based):", bg=PANEL, fg=FG,
                 font=SANS).grid(column=0, row=0, **pad, sticky="e")
        tk.Entry(ctrl, textvariable=self.pin_var, width=5,
                 bg=ENTRY_BG, fg=FG, insertbackground=FG, font=MONO).grid(
            column=1, row=0, **pad, sticky="ew")

        # Level indicator
        self._level_lbl = tk.Label(ctrl, textvariable=self._level,
                                   bg=PANEL, fg=RED,
                                   font=("Courier", 28, "bold"))
        self._level_lbl.grid(column=0, row=1, columnspan=2, **pad)

        btn_row = tk.Frame(ctrl, bg=PANEL)
        btn_row.grid(column=0, row=2, columnspan=3, **pad, sticky="ew")

        tk.Button(btn_row, text="Set HIGH", command=self._set_high,
                  bg=GREEN, fg=BG, font=SANS_B, padx=14, pady=6,
                  relief="flat").pack(side="left", padx=8)
        tk.Button(btn_row, text="Set LOW", command=self._set_low,
                  bg=RED, fg=BG, font=SANS_B, padx=14, pady=6,
                  relief="flat").pack(side="left", padx=8)
        tk.Button(btn_row, text="Toggle", command=self._toggle,
                  bg=BUTTON_BG, fg=FG, font=SANS_B, padx=14, pady=6,
                  relief="flat").pack(side="left", padx=8)

        _status_bar(self, self._status, row=99)

    def _connect(self):
        if not HAS_ADS:
            messagebox.showerror("Error", "waveforms_ads module not available.")
            return
        try:
            self._ads = WaveFormsADS()
            pin = self.pin_var.get()
            self._ads.digital_io_reset()
            self._ads.digital_io_set_output_enable(1 << pin)
            self._ads.digital_io_set_output(0)
            self._status.set(f"Connected – pin {pin} configured as output (LOW)")
            self._update_indicator(False)
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _disconnect(self):
        if self._ads:
            try:
                self._ads.close()
            except Exception:
                pass
            self._ads = None
        self._status.set("Disconnected")

    def _require_ads(self):
        if self._ads is None:
            messagebox.showerror("Error", "Not connected to ADS.")
            return False
        return True

    def _set_high(self):
        if not self._require_ads():
            return
        try:
            pin = self.pin_var.get()
            self._ads.digital_io_write_pin(pin, True)
            self._update_indicator(True)
            self._status.set(f"Pin {pin} → HIGH")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _set_low(self):
        if not self._require_ads():
            return
        try:
            pin = self.pin_var.get()
            self._ads.digital_io_write_pin(pin, False)
            self._update_indicator(False)
            self._status.set(f"Pin {pin} → LOW")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _toggle(self):
        if self._level.get() == "HIGH":
            self._set_low()
        else:
            self._set_high()

    def _update_indicator(self, high: bool):
        if high:
            self._level.set("HIGH")
            self._level_lbl.config(fg=GREEN)
        else:
            self._level.set("LOW")
            self._level_lbl.config(fg=RED)


# ===========================================================================
# TAB 4 – Count-Rate vs Time
# ===========================================================================

class CountRateTimeTab(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent, bg=BG)
        self._running   = False
        self._thread    = None
        self._q         = queue.Queue()
        self._data      = []          # list of (elapsed_s, count, timestamp_str)
        self._csv_file  = None
        self._csv_writer= None
        self._status    = tk.StringVar(value="Idle")
        self._last_trace= None
        self._build()
        self.after(150, self._drain_queue)

    def _build(self):
        left = tk.Frame(self, bg=BG)
        left.pack(side="left", fill="y", padx=6, pady=6)

        self.scope_cfg = ScopeSettingsFrame(left)
        self.scope_cfg.pack(fill="x", pady=4)

        meas = tk.LabelFrame(left, text="Measurement Settings", bg=PANEL,
                             fg=ACCENT, font=SANS_B)
        meas.pack(fill="x", pady=4)

        self.integ_time = tk.DoubleVar(value=1.0)
        _lf(meas, "Integration Time (s):", col=0, row=0)
        _ef(meas, self.integ_time, col=1, row=0)

        self.save_csv  = tk.BooleanVar(value=False)
        self.csv_path  = tk.StringVar(value="")

        cb = tk.Checkbutton(meas, text="Save to CSV", variable=self.save_csv,
                            command=self._toggle_csv_path,
                            bg=PANEL, fg=FG, selectcolor=ENTRY_BG,
                            activebackground=PANEL, font=SANS)
        cb.grid(column=0, row=1, columnspan=2, sticky="w", padx=PADX, pady=PADY)

        self._csv_entry = tk.Entry(meas, textvariable=self.csv_path, width=20,
                                   bg=ENTRY_BG, fg=FG, insertbackground=FG,
                                   font=MONO, state="disabled")
        self._csv_entry.grid(column=0, row=2, columnspan=2, sticky="ew",
                              padx=PADX, pady=PADY)
        _btn(meas, "Browse…", self._browse_csv, col=2, row=2)

        btn_row = tk.Frame(left, bg=BG)
        btn_row.pack(fill="x", pady=4)
        tk.Button(btn_row, text="Start", command=self._start,
                  bg=GREEN, fg=BG, font=SANS_B, padx=10, pady=4,
                  relief="flat").pack(side="left", padx=6)
        tk.Button(btn_row, text="Stop", command=self._stop,
                  bg=RED, fg=BG, font=SANS_B, padx=10, pady=4,
                  relief="flat").pack(side="left", padx=6)
        tk.Button(btn_row, text="Clear", command=self._clear,
                  bg=BUTTON_BG, fg=FG, font=SANS_B, padx=10, pady=4,
                  relief="flat").pack(side="left", padx=6)

        _status_bar(left, self._status, row=99)

        # Right: plot + last trace
        right = tk.Frame(self, bg=BG)
        right.pack(side="left", fill="both", expand=True, padx=4, pady=6)

        if HAS_MPL:
            self._fig  = Figure(figsize=(6, 5), facecolor=BG)
            self._ax_cr= self._fig.add_subplot(211)
            self._ax_tr= self._fig.add_subplot(212)
            for ax in (self._ax_cr, self._ax_tr):
                ax.set_facecolor(PANEL)
                ax.tick_params(colors=FG)
                for sp in ax.spines.values():
                    sp.set_edgecolor(FG)
            self._ax_cr.set_xlabel("Elapsed time (s)", color=FG)
            self._ax_cr.set_ylabel("Counts / window", color=FG)
            self._ax_cr.set_title("Count Rate vs Time", color=ACCENT)
            self._ax_tr.set_xlabel("Sample", color=FG)
            self._ax_tr.set_ylabel("Voltage (V)", color=FG)
            self._ax_tr.set_title("Most Recent Trigger Trace", color=ACCENT)
            self._fig.tight_layout(pad=2)
            self._canvas = FigureCanvasTkAgg(self._fig, master=right)
            self._canvas.get_tk_widget().pack(fill="both", expand=True)
        else:
            tk.Label(right, text="matplotlib not installed",
                     bg=BG, fg=RED, font=SANS_LG).pack(expand=True)

    def _toggle_csv_path(self):
        state = "normal" if self.save_csv.get() else "disabled"
        self._csv_entry.config(state=state)

    def _browse_csv(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All", "*.*")],
            title="Save Count-Rate CSV",
        )
        if path:
            self.csv_path.set(path)

    def _start(self):
        if self._running:
            return
        if not HAS_ADS:
            messagebox.showerror("Error", "waveforms_ads module not available.")
            return
        if self.save_csv.get():
            path = self.csv_path.get().strip()
            if not path:
                messagebox.showerror("Error", "Please choose a CSV path first.")
                return
            self._csv_file   = open(path, "w", newline="")
            self._csv_writer = csv.writer(self._csv_file)
            self._csv_writer.writerow(["Timestamp", "Elapsed_s", "Counts"])
        self._running = True
        self._status.set("Running…")
        self._thread = threading.Thread(target=self._measure_loop, daemon=True)
        self._thread.start()

    def _stop(self):
        self._running = False
        if self._csv_file:
            self._csv_file.close()
            self._csv_file   = None
            self._csv_writer = None
        self._status.set("Stopped")

    def _clear(self):
        self._data = []
        self._last_trace = None
        self._redraw()

    def _measure_loop(self):
        p        = self.scope_cfg.get_params()
        integ    = self.integ_time.get()
        t_start  = time.time()
        if p["invert"]:
            trigger_slope = (p["slope"] + 1)%2
            trigger_level = -p["trigger_level"]
        else:
            trigger_slope = p["slope"]
            trigger_level = p["trigger_level"]
        try:
            with WaveFormsADS() as dev:
                dev.analog_in_set_range(p["channel"], p["y_range"])
                while self._running:
                    window_start = time.time()
                    count        = 0
                    last_trace   = None
                    while time.time() - window_start < integ and self._running:
                        try:
                            data = dev.analog_in_capture(
                                channel             = p["channel"],
                                sample_rate_hz      = p["sample_rate"],
                                buffer_size         = p["buffer_size"],
                                trigger_level_v     = trigger_level,
                                trigger_condition   = trigger_slope,
                                auto_timeout_s      = 0.0,
                                timeout_s           = max(integ * 2, 1.0),
                            )
                            if p["invert"]:
                                data = -data
                            count      += 1
                            last_trace  = data
                        except Exception:
                            pass
                    elapsed  = time.time() - t_start
                    ts       = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
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
                if trace is not None:
                    self._last_trace = trace
                if self._csv_writer:
                    self._csv_writer.writerow([ts, f"{elapsed:.3f}", count])
                    self._csv_file.flush()
                self._status.set(f"Last: {ts}  |  {count} counts in {self.integ_time.get():.1f} s")
                self._redraw()
            elif msg[0] == "error":
                self._status.set(f"Error: {msg[1]}")
                self._running = False
        self.after(150, self._drain_queue)

    def _redraw(self):
        if not HAS_MPL or not self._data:
            return
        xs = [d[0] for d in self._data]
        ys = [d[1] for d in self._data]
        self._ax_cr.clear()
        self._ax_cr.set_facecolor(PANEL)
        self._ax_cr.tick_params(colors=FG)
        self._ax_cr.set_xlabel("Elapsed time (s)", color=FG)
        self._ax_cr.set_ylabel("Counts / window", color=FG)
        self._ax_cr.set_title("Count Rate vs Time", color=ACCENT)
        self._ax_cr.plot(xs, ys, color=ACCENT, linewidth=1.5, marker="o",
                         markersize=3)
        self._ax_tr.clear()
        self._ax_tr.set_facecolor(PANEL)
        self._ax_tr.tick_params(colors=FG)
        self._ax_tr.set_xlabel("Sample", color=FG)
        self._ax_tr.set_ylabel("Voltage (V)", color=FG)
        self._ax_tr.set_title("Most Recent Trigger Trace", color=ACCENT)
        if self._last_trace is not None:
            self._ax_tr.plot(self._last_trace, color=GREEN, linewidth=1)
        self._fig.tight_layout(pad=2)
        self._canvas.draw()


# ===========================================================================
# TAB 5 – Count-Rate vs Current
# ===========================================================================

class CountRateCurrentTab(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent, bg=BG)
        self._running    = False
        self._thread     = None
        self._q          = queue.Queue()
        self._data       = []          # list of (current, count)
        self._csv_file   = None
        self._csv_writer = None
        self._status     = tk.StringVar(value="Idle")
        self._elapsed    = tk.StringVar(value="—")
        self._eta        = tk.StringVar(value="—")
        self._t_start    = None
        self._build()
        self.after(150, self._drain_queue)

    def _build(self):
        # ---- Scrollable left panel ----
        left = tk.Frame(self, bg=BG)
        left.pack(side="left", fill="y", padx=6, pady=6)

        self.scope_cfg = ScopeSettingsFrame(left)
        self.scope_cfg.pack(fill="x", pady=4)

        sweep = tk.LabelFrame(left, text="Current Sweep", bg=PANEL,
                              fg=ACCENT, font=SANS_B)
        sweep.pack(fill="x", pady=4)

        self.i_start    = tk.DoubleVar(value=0.0)
        self.i_stop     = tk.DoubleVar(value=0.1)
        self.i_step     = tk.DoubleVar(value=0.01)
        self.integ_time = tk.DoubleVar(value=1.0)
        self.psu_ch     = tk.StringVar(value="CH1")
        self.psu_host   = tk.StringVar(value="")   # empty = USB
        self.quality    = tk.StringVar(value="High")
        self.flip_curr  = tk.BooleanVar(value=False)
        self.save_csv   = tk.BooleanVar(value=False)
        self.csv_path   = tk.StringVar(value="")

        r = 0
        _lf(sweep, "PSU Channel:", col=0, row=r)
        ttk.OptionMenu(sweep, self.psu_ch, "CH1", "CH1", "CH2").grid(
            column=1, row=r, sticky="ew", padx=PADX, pady=PADY)

        r += 1
        _lf(sweep, "PSU Host (blank=USB):", col=0, row=r)
        _ef(sweep, self.psu_host, col=1, row=r, width=14)

        r += 1
        _lf(sweep, "I start (A):", col=0, row=r)
        _ef(sweep, self.i_start, col=1, row=r)

        r += 1
        _lf(sweep, "I stop (A):", col=0, row=r)
        _ef(sweep, self.i_stop, col=1, row=r)

        r += 1
        _lf(sweep, "I step / bin (A):", col=0, row=r)
        _ef(sweep, self.i_step, col=1, row=r)

        r += 1
        _lf(sweep, "Integration Time (s):", col=0, row=r)
        _ef(sweep, self.integ_time, col=1, row=r)

        r += 1
        _lf(sweep, "Scan Quality:", col=0, row=r)
        ttk.OptionMenu(sweep, self.quality, "High", "Low", "High").grid(
            column=1, row=r, sticky="ew", padx=PADX, pady=PADY)

        r += 1
        cb_flip = tk.Checkbutton(sweep, text="Flip Current Direction",
                                 variable=self.flip_curr,
                                 command=self._on_flip_current,
                                 bg=PANEL, fg=FG, selectcolor=ENTRY_BG,
                                 activebackground=PANEL, font=SANS)
        cb_flip.grid(column=0, row=r, columnspan=2, sticky="w",
                     padx=PADX, pady=PADY)

        r += 1
        cb_csv = tk.Checkbutton(sweep, text="Save to CSV",
                                variable=self.save_csv,
                                command=self._toggle_csv_path,
                                bg=PANEL, fg=FG, selectcolor=ENTRY_BG,
                                activebackground=PANEL, font=SANS)
        cb_csv.grid(column=0, row=r, columnspan=2, sticky="w",
                    padx=PADX, pady=PADY)

        r += 1
        self._csv_entry = tk.Entry(sweep, textvariable=self.csv_path, width=16,
                                   bg=ENTRY_BG, fg=FG, insertbackground=FG,
                                   font=MONO, state="disabled")
        self._csv_entry.grid(column=0, row=r, columnspan=2, sticky="ew",
                              padx=PADX, pady=PADY)
        _btn(sweep, "Browse…", self._browse_csv, col=2, row=r)

        # Timing display
        timing = tk.LabelFrame(left, text="Scan Progress", bg=PANEL,
                               fg=ACCENT, font=SANS_B)
        timing.pack(fill="x", pady=4)
        tk.Label(timing, text="Elapsed:", bg=PANEL, fg=FG,
                 font=SANS).grid(column=0, row=0, sticky="e", padx=PADX, pady=2)
        tk.Label(timing, textvariable=self._elapsed, bg=PANEL, fg=GREEN,
                 font=MONO).grid(column=1, row=0, sticky="w", padx=PADX, pady=2)
        tk.Label(timing, text="Est. Remaining:", bg=PANEL, fg=FG,
                 font=SANS).grid(column=0, row=1, sticky="e", padx=PADX, pady=2)
        tk.Label(timing, textvariable=self._eta, bg=PANEL, fg=YELLOW,
                 font=MONO).grid(column=1, row=1, sticky="w", padx=PADX, pady=2)

        btn_row = tk.Frame(left, bg=BG)
        btn_row.pack(fill="x", pady=4)
        tk.Button(btn_row, text="Start", command=self._start,
                  bg=GREEN, fg=BG, font=SANS_B, padx=10, pady=4,
                  relief="flat").pack(side="left", padx=6)
        tk.Button(btn_row, text="Stop", command=self._stop,
                  bg=RED, fg=BG, font=SANS_B, padx=10, pady=4,
                  relief="flat").pack(side="left", padx=6)
        tk.Button(btn_row, text="Clear", command=self._clear,
                  bg=BUTTON_BG, fg=FG, font=SANS_B, padx=10, pady=4,
                  relief="flat").pack(side="left", padx=6)

        _status_bar(left, self._status, row=99)

        # ---- Right: plot ----
        right = tk.Frame(self, bg=BG)
        right.pack(side="left", fill="both", expand=True, padx=4, pady=6)

        if HAS_MPL:
            self._fig = Figure(figsize=(6, 4), facecolor=BG)
            self._ax  = self._fig.add_subplot(111)
            self._ax.set_facecolor(PANEL)
            self._ax.tick_params(colors=FG)
            for sp in self._ax.spines.values():
                sp.set_edgecolor(FG)
            self._ax.set_xlabel("Current (A)", color=FG)
            self._ax.set_ylabel("Counts / window", color=FG)
            self._ax.set_title("Count Rate vs Current", color=ACCENT)
            self._canvas = FigureCanvasTkAgg(self._fig, master=right)
            self._canvas.get_tk_widget().pack(fill="both", expand=True)
        else:
            tk.Label(right, text="matplotlib not installed",
                     bg=BG, fg=RED, font=SANS_LG).pack(expand=True)

        self._update_timer()

    # ---- Stubs / helpers ----

    def _on_flip_current(self):
        """Placeholder – connect to hardware polarity-flip logic here."""
        pass

    def _toggle_csv_path(self):
        state = "normal" if self.save_csv.get() else "disabled"
        self._csv_entry.config(state=state)

    def _browse_csv(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All", "*.*")],
            title="Save Current-Sweep CSV",
        )
        if path:
            self.csv_path.set(path)

    def _start(self):
        if self._running:
            return
        if not HAS_ADS:
            messagebox.showerror("Error", "waveforms_ads module not available.")
            return
        if not HAS_PSU:
            messagebox.showerror("Error", "spd3303c_power_supply module not available.")
            return
        if self.save_csv.get():
            path = self.csv_path.get().strip()
            if not path:
                messagebox.showerror("Error", "Please choose a CSV path first.")
                return
            self._csv_file   = open(path, "w", newline="")
            self._csv_writer = csv.writer(self._csv_file)
            self._csv_writer.writerow(["Current_A", "Counts", "Timestamp"])
        self._running  = True
        self._t_start  = time.time()
        self._status.set("Running…")
        self._thread   = threading.Thread(target=self._sweep_loop, daemon=True)
        self._thread.start()

    def _stop(self):
        self._running = False
        if self._csv_file:
            self._csv_file.close()
            self._csv_file   = None
            self._csv_writer = None
        self._status.set("Stopped")

    def _clear(self):
        self._data = []
        self._redraw()

    def _update_timer(self):
        if self._running and self._t_start is not None:
            elapsed = time.time() - self._t_start
            self._elapsed.set(self._fmt_time(elapsed))
        self.after(500, self._update_timer)

    @staticmethod
    def _fmt_time(secs):
        secs = int(secs)
        h, rem = divmod(secs, 3600)
        m, s   = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    # ---- Sweep loop ----

    def _sweep_loop(self):
        i_start  = self.i_start.get()
        i_stop   = self.i_stop.get()
        i_step   = self.i_step.get()
        integ    = self.integ_time.get()
        quality  = self.quality.get()          # "Low" or "High"
        settle   = 5.0 if quality == "High" else 0.5
        p        = self.scope_cfg.get_params()

        currents = []
        v = i_start
        while v <= i_stop + 1e-9:
            currents.append(round(v, 10))
            v += i_step
        n_steps   = len(currents)
        t_per_step= integ + settle

        psu_host = self.psu_host.get().strip()
        try:
            if psu_host:
                psu_ctx = SPD3303X.ethernet_device(psu_host)
            else:
                psu_ctx = SPD3303X.usb_device()

            with psu_ctx as psu:
                ch = getattr(psu, self.psu_ch.get())
                ch.set_output(True)

                with WaveFormsADS() as dev:
                    dev.analog_in_set_range(p["channel"], p["y_range"])
                    if p["invert"]:
                        trigger_slope = (p["slope"] + 1)%2
                        trigger_level = -p["trigger_level"]
                    else:
                        trigger_slope = p["slope"]
                        trigger_level = p["trigger_level"]

                    for idx, current in enumerate(currents):
                        if not self._running:
                            break
                        ch.set_current(current)
                        if settle > 0:
                            time.sleep(settle)

                        count = 0
                        window_start = time.time()
                        while time.time() - window_start < integ and self._running:
                            try:
                                dev.analog_in_capture(
                                    channel             = p["channel"],
                                    sample_rate_hz      = p["sample_rate"],
                                    buffer_size         = p["buffer_size"],
                                    trigger_level_v     = trigger_level,
                                    trigger_condition   = trigger_slope,
                                    auto_timeout_s      = 0.0,
                                    timeout_s           = max(integ * 2, 1.0),
                                )
                                count += 1
                            except Exception:
                                pass

                        elapsed = time.time() - self._t_start
                        done    = idx + 1
                        if done > 0:
                            rate   = elapsed / done
                            remain = rate * (n_steps - done)
                            self._q.put(("eta", remain))
                        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
                        self._q.put(("point", current, count, ts))

                ch.set_output(False)
        except Exception as e:
            self._q.put(("error", str(e)))
        self._running = False

    def _drain_queue(self):
        while not self._q.empty():
            msg = self._q.get_nowait()
            if msg[0] == "point":
                _, current, count, ts = msg
                self._data.append((current, count))
                if self._csv_writer:
                    self._csv_writer.writerow([f"{current:.6f}", count, ts])
                    self._csv_file.flush()
                self._status.set(f"I = {current:.4f} A  →  {count} counts")
                self._redraw()
            elif msg[0] == "eta":
                self._eta.set(self._fmt_time(msg[1]))
            elif msg[0] == "error":
                self._status.set(f"Error: {msg[1]}")
                self._running = False
        self.after(150, self._drain_queue)

    def _redraw(self):
        if not HAS_MPL or not self._data:
            return
        xs = [d[0] for d in self._data]
        ys = [d[1] for d in self._data]
        self._ax.clear()
        self._ax.set_facecolor(PANEL)
        self._ax.tick_params(colors=FG)
        for sp in self._ax.spines.values():
            sp.set_edgecolor(FG)
        self._ax.set_xlabel("Current (A)", color=FG)
        self._ax.set_ylabel("Counts / window", color=FG)
        self._ax.set_title("Count Rate vs Current", color=ACCENT)
        self._ax.plot(xs, ys, color=ACCENT, linewidth=1.5,
                      marker="o", markersize=4)
        self._ax.fill_between(xs, ys, alpha=0.15, color=ACCENT)
        self._canvas.draw()


# ===========================================================================
# TAB REGISTRY – add new tabs here
# ===========================================================================

TABS = [
    ("Power Supply",        PowerSupplyTab),
    ("Scope Viewer",        ScopeViewerTab),
    ("Digital Output",      DigitalOutputTab),
    ("Count Rate vs Time",  CountRateTimeTab),
    ("Count Rate vs Current", CountRateCurrentTab),
    # ("My New Tab",         MyNewTabClass),   ← extend here
]


# ===========================================================================
# Main application window
# ===========================================================================

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Beta Ray Spectroscopy Controller")
        self.configure(bg=BG)
        self.geometry("1100x700")
        self._apply_style()
        self._build_ui()

    def _apply_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TNotebook",        background=BG,    borderwidth=0)
        style.configure("TNotebook.Tab",    background=PANEL, foreground=FG,
                        padding=[12, 5], font=SANS_B)
        style.map("TNotebook.Tab",
                  background=[("selected", ACCENT)],
                  foreground=[("selected", BG)])
        style.configure("TMenubutton",      background=BUTTON_BG, foreground=FG,
                        relief="flat", font=SANS)
        style.configure("TCombobox",        fieldbackground=ENTRY_BG,
                        background=BUTTON_BG, foreground=FG)

    def _build_ui(self):
        # Header
        hdr = tk.Frame(self, bg=ACCENT, height=36)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Beta Ray Spectroscopy Controller", bg=ACCENT, fg=BG,
                 font=("Helvetica", 13, "bold")).pack(side="left", pady=4)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=4, pady=4)

        for tab_name, TabClass in TABS:
            frame = TabClass(nb)
            nb.add(frame, text=tab_name)


if __name__ == "__main__":
    app = App()
    app.mainloop()
