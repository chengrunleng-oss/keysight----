from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import matplotlib

matplotlib.use("TkAgg")
matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
matplotlib.rcParams["axes.unicode_minus"] = False
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure
import numpy as np

from .acquisition import AcquisitionSession, SessionEvent, SessionSettings
from .connection_gate import (
    ConnectionGate,
    ConnectionFingerprint,
    ParameterFingerprint,
    make_fingerprint,
    make_parameter_fingerprint,
)
from .instrument import (
    InstrumentSettings,
    Keysight53230A,
    ParameterCheck,
    ParameterValidationError,
    Simulated53230A,
)


class StabilityAnalyzerApp(tk.Tk):
    BG = "#f3f5f7"
    PANEL = "#ffffff"
    TEXT = "#17212b"
    MUTED = "#64707d"
    GREEN = "#16794b"
    RED = "#b42318"
    BLUE = "#1769aa"

    def __init__(self) -> None:
        super().__init__()
        self.title("53230A 实时频率稳定度分析")
        self.geometry("1320x820")
        self.minsize(1050, 680)
        self.configure(bg=self.BG)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.events: queue.Queue[SessionEvent] = queue.Queue()
        self.session: AcquisitionSession | None = None
        self.latest_result = None
        self.current_session_dir = ""
        self.current_segment = 0
        self.config_inputs: list[tuple[ttk.Widget, str]] = []
        self._close_deadline: float | None = None
        self.connection_gate = ConnectionGate()
        self._connection_test_id = 0
        self._parameter_test_id = 0

        self.host_var = tk.StringVar(value="192.168.1.123")
        self.port_var = tk.StringVar(value="5025")
        self.channel_var = tk.StringVar(value="2")
        self.gate_var = tk.StringVar(value="0.1")
        self.impedance_var = tk.StringVar(value="1 MΩ")
        self.reference_var = tk.StringVar(value="0")
        self.window_var = tk.StringVar(value="200000")
        self.output_var = tk.StringVar(value=str(Path(__file__).resolve().parents[1] / "data"))
        self.simulated_var = tk.BooleanVar(value=False)
        self.show_allan_var = tk.BooleanVar(value=True)
        self.show_hadamard_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="未开始")
        self.count_var = tk.StringVar(value="0")
        self.elapsed_var = tk.StringVar(value="00:00:00")
        self.latest_var = tk.StringVar(value="-")
        self.reference_display_var = tk.StringVar(value="-")

        self._configure_style()
        self._build_ui()
        self._bind_connection_inputs()
        self._update_start_button_state()
        self.after(100, self._poll_events)

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("App.TFrame", background=self.BG)
        style.configure("Panel.TFrame", background=self.PANEL)
        style.configure("TLabel", font=("Microsoft YaHei UI", 9))
        style.configure("Header.TLabel", font=("Microsoft YaHei UI", 15, "bold"), background=self.BG, foreground=self.TEXT)
        style.configure("Subtle.TLabel", font=("Microsoft YaHei UI", 9), background=self.BG, foreground=self.MUTED)
        style.configure("Panel.TLabel", background=self.PANEL, foreground=self.TEXT)
        style.configure("Metric.TLabel", font=("Segoe UI", 14, "bold"), background=self.PANEL, foreground=self.TEXT)
        style.configure("Start.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=(16, 8))
        style.configure("Stop.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=(16, 8))

    def _build_ui(self) -> None:
        root = ttk.Frame(self, style="App.TFrame", padding=16)
        root.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(root, style="App.TFrame")
        header.pack(fill=tk.X, pady=(0, 12))
        title_box = ttk.Frame(header, style="App.TFrame")
        title_box.pack(side=tk.LEFT)
        ttk.Label(title_box, text="53230A 实时频率稳定度分析", style="Header.TLabel").pack(anchor=tk.W)
        ttk.Label(title_box, textvariable=self.status_var, style="Subtle.TLabel").pack(anchor=tk.W, pady=(3, 0))
        self.stop_button = ttk.Button(header, text="停止", style="Stop.TButton", command=self._stop, state=tk.DISABLED)
        self.stop_button.pack(side=tk.RIGHT, padx=(8, 0))
        self.start_button = ttk.Button(
            header, text="开始采集", style="Start.TButton", command=self._start, state=tk.DISABLED
        )
        self.start_button.pack(side=tk.RIGHT)
        self.parameter_check_button = ttk.Button(
            header,
            text="写入并检查参数",
            command=self._write_and_check_parameters,
            state=tk.DISABLED,
        )
        self.parameter_check_button.pack(side=tk.RIGHT, padx=(0, 8))
        self.test_connection_button = ttk.Button(header, text="测试连接", command=self._test_connection)
        self.test_connection_button.pack(side=tk.RIGHT, padx=(0, 8))

        body = ttk.Panedwindow(root, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True)
        controls = ttk.Frame(body, style="Panel.TFrame", padding=14, width=300)
        chart_panel = ttk.Frame(body, style="Panel.TFrame", padding=8)
        body.add(controls, weight=0)
        body.add(chart_panel, weight=1)
        self._build_controls(controls)
        self._build_charts(chart_panel)

    def _build_controls(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(1, weight=1)
        row = 0
        ttk.Label(parent, text="仪器", style="Panel.TLabel", font=("Microsoft YaHei UI", 10, "bold")).grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=(0, 10))
        row += 1
        self._entry_row(parent, row, "IP 地址", self.host_var)
        row += 1
        self._entry_row(parent, row, "端口", self.port_var)
        row += 1
        self._combo_row(parent, row, "通道", self.channel_var, ("1", "2"))
        row += 1
        self._entry_row(parent, row, "Gate time (s)", self.gate_var)
        row += 1
        self._combo_row(parent, row, "输入阻抗", self.impedance_var, ("1 MΩ", "50 Ω"))
        row += 1
        self.simulated_check = ttk.Checkbutton(parent, text="使用模拟数据", variable=self.simulated_var)
        self.simulated_check.grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=(5, 14))
        self.config_inputs.append((self.simulated_check, "normal"))

        row += 1
        ttk.Separator(parent).grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=(0, 14))
        row += 1
        ttk.Label(parent, text="实时分析", style="Panel.TLabel", font=("Microsoft YaHei UI", 10, "bold")).grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=(0, 10))
        row += 1
        self._entry_row(parent, row, "参考频率 (Hz)", self.reference_var)
        row += 1
        ttk.Label(parent, text="填 0 时使用当前分析窗口均值", style="Panel.TLabel", foreground=self.MUTED).grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=(0, 8))
        row += 1
        self._entry_row(parent, row, "分析窗口 (点)", self.window_var)
        row += 1
        checks = ttk.Frame(parent, style="Panel.TFrame")
        checks.grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=(4, 14))
        ttk.Checkbutton(checks, text="Allan", variable=self.show_allan_var, command=self._refresh_deviation_plot).pack(side=tk.LEFT)
        ttk.Checkbutton(checks, text="Hadamard", variable=self.show_hadamard_var, command=self._refresh_deviation_plot).pack(side=tk.LEFT, padx=(12, 0))

        row += 1
        ttk.Separator(parent).grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=(0, 14))
        row += 1
        ttk.Label(parent, text="本地数据", style="Panel.TLabel", font=("Microsoft YaHei UI", 10, "bold")).grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=(0, 10))
        row += 1
        output_entry = ttk.Entry(parent, textvariable=self.output_var)
        output_entry.grid(row=row, column=0, columnspan=2, sticky=tk.EW, pady=4)
        browse_button = ttk.Button(parent, text="浏览", command=self._browse_output)
        browse_button.grid(row=row, column=2, padx=(6, 0), pady=4)
        self.config_inputs.extend(((output_entry, "normal"), (browse_button, "normal")))

        row += 1
        ttk.Separator(parent).grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=14)
        row += 1
        metrics = (
            ("采样点数", self.count_var),
            ("名义时长", self.elapsed_var),
            ("最新频率 (Hz)", self.latest_var),
            ("分析参考 (Hz)", self.reference_display_var),
        )
        for label, variable in metrics:
            ttk.Label(parent, text=label, style="Panel.TLabel", foreground=self.MUTED).grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=(5, 0))
            row += 1
            ttk.Label(parent, textvariable=variable, style="Metric.TLabel").grid(row=row, column=0, columnspan=3, sticky=tk.W)
            row += 1

    def _entry_row(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(parent, text=label, style="Panel.TLabel").grid(row=row, column=0, sticky=tk.W, pady=4)
        entry = ttk.Entry(parent, textvariable=variable, width=18)
        entry.grid(row=row, column=1, columnspan=2, sticky=tk.EW, padx=(8, 0), pady=4)
        self.config_inputs.append((entry, "normal"))

    def _combo_row(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar, values: tuple[str, ...]) -> None:
        ttk.Label(parent, text=label, style="Panel.TLabel").grid(row=row, column=0, sticky=tk.W, pady=4)
        combobox = ttk.Combobox(parent, textvariable=variable, values=values, state="readonly", width=16)
        combobox.grid(row=row, column=1, columnspan=2, sticky=tk.EW, padx=(8, 0), pady=4)
        self.config_inputs.append((combobox, "readonly"))

    def _set_config_enabled(self, enabled: bool) -> None:
        for widget, normal_state in self.config_inputs:
            widget.configure(state=normal_state if enabled else tk.DISABLED)

    def _bind_connection_inputs(self) -> None:
        for variable in (self.host_var, self.port_var, self.simulated_var):
            variable.trace_add("write", self._connection_settings_changed)
        for variable in (self.channel_var, self.gate_var, self.impedance_var):
            variable.trace_add("write", self._parameter_settings_changed)

    def _connection_settings_changed(self, *_args) -> None:
        if self.session is None or not self.session.is_running:
            self.connection_gate.invalidate()
            self.test_connection_button.configure(state=tk.NORMAL)
            self.parameter_check_button.configure(state=tk.DISABLED)
            self._update_start_button_state()

    def _parameter_settings_changed(self, *_args) -> None:
        if self.session is None or not self.session.is_running:
            self.connection_gate.invalidate_parameters()
            self._update_parameter_button_state()
            self._update_start_button_state()

    def _connection_fingerprint(self) -> ConnectionFingerprint:
        return make_fingerprint(
            self.simulated_var.get(), self.host_var.get(), self.port_var.get()
        )

    def _parameter_fingerprint(self) -> ParameterFingerprint:
        return make_parameter_fingerprint(
            self._connection_fingerprint(),
            self.channel_var.get(),
            self.gate_var.get(),
            self.impedance_var.get(),
        )

    def _update_parameter_button_state(self) -> None:
        enabled = self.connection_gate.is_verified(self._connection_fingerprint())
        if self.session is not None and self.session.is_running:
            enabled = False
        self.parameter_check_button.configure(state=tk.NORMAL if enabled else tk.DISABLED)

    def _update_start_button_state(self) -> None:
        can_start = self.connection_gate.is_verified(self._connection_fingerprint())
        can_start = can_start and self.connection_gate.is_parameter_verified(
            self._connection_fingerprint(), self._parameter_fingerprint()
        )
        if self.session is not None and self.session.is_running:
            can_start = False
        self.start_button.configure(state=tk.NORMAL if can_start else tk.DISABLED)

    def _build_charts(self, parent: ttk.Frame) -> None:
        self.figure = Figure(figsize=(9, 7), dpi=100, facecolor=self.PANEL)
        self.frequency_axis = self.figure.add_subplot(211)
        self.deviation_axis = self.figure.add_subplot(212)
        self.figure.subplots_adjust(left=0.1, right=0.97, top=0.96, bottom=0.09, hspace=0.34)
        self.frequency_line, = self.frequency_axis.plot([], [], color=self.BLUE, linewidth=1.0)
        self.allan_line, = self.deviation_axis.loglog([], [], color=self.BLUE, marker="o", markersize=3, label="Overlapping Allan")
        self.hadamard_line, = self.deviation_axis.loglog([], [], color=self.GREEN, marker="s", markersize=3, label="Overlapping Hadamard")
        self.frequency_axis.set_title("频率随时间变化", fontsize=11)
        self.frequency_axis.set_xlabel("最近窗口时间 (s)")
        self.frequency_axis.set_ylabel("频率 (Hz)")
        self.deviation_axis.set_title("实时稳定度", fontsize=11)
        self.deviation_axis.set_xlabel("Tau (s)")
        self.deviation_axis.set_ylabel("偏差")
        for axis in (self.frequency_axis, self.deviation_axis):
            axis.grid(True, which="both", color="#d9dee3", linewidth=0.6)
            axis.tick_params(labelsize=8)
        self.deviation_axis.legend(loc="best", fontsize=8)

        self.canvas = FigureCanvasTkAgg(self.figure, master=parent)
        self.canvas.draw()
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        toolbar = NavigationToolbar2Tk(self.canvas, parent, pack_toolbar=False)
        toolbar.update()
        toolbar.pack(fill=tk.X)

    def _browse_output(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.output_var.get() or str(Path.cwd()))
        if selected:
            self.output_var.set(selected)

    def _session_settings(self) -> SessionSettings:
        simulated = self.simulated_var.get()
        entered_host = self.host_var.get().strip()
        instrument = InstrumentSettings(
            host=entered_host or ("simulation" if simulated else ""),
            port=int(self.port_var.get()),
            channel=int(self.channel_var.get()),
            gate_time_s=float(self.gate_var.get()),
            impedance_ohm=50 if self.impedance_var.get().startswith("50") else 1_000_000,
        )
        settings = SessionSettings(
            instrument=instrument,
            reference_hz=float(self.reference_var.get()),
            analysis_window_points=int(self.window_var.get()),
            output_root=Path(self.output_var.get()),
            simulated=simulated,
        )
        settings.validate()
        return settings

    def _start(self) -> None:
        settings_fingerprint = self._connection_fingerprint()
        parameter_fingerprint = self._parameter_fingerprint()
        if not self.connection_gate.is_parameter_verified(
            settings_fingerprint, parameter_fingerprint
        ):
            messagebox.showwarning(
                "需要完成两阶段检查",
                "请依次完成“测试连接”和“写入并检查参数”。修改相关参数后需要重新检查。",
                parent=self,
            )
            self._update_start_button_state()
            return
        try:
            settings = self._session_settings()
        except (ValueError, OSError) as exc:
            messagebox.showerror("参数错误", str(exc), parent=self)
            return
        source = Simulated53230A(settings.instrument) if settings.simulated else Keysight53230A(settings.instrument)
        self.session = AcquisitionSession(settings, source, self.events)
        self.latest_result = None
        self.current_session_dir = ""
        self.current_segment = 0
        self.count_var.set("0")
        self.elapsed_var.set("00:00:00")
        self.latest_var.set("-")
        self.reference_display_var.set("-")
        self.status_var.set("正在连接仪器…")
        self._set_config_enabled(False)
        self.test_connection_button.configure(state=tk.DISABLED)
        self.parameter_check_button.configure(state=tk.DISABLED)
        self.start_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL)
        self.session.start()

    def _test_connection(self) -> None:
        try:
            port = int(self.port_var.get())
        except ValueError as exc:
            messagebox.showerror("连接参数错误", f"端口必须是整数: {exc}", parent=self)
            return
        try:
            if not self.simulated_var.get() and not self.host_var.get().strip():
                raise ValueError("硬件模式必须填写 IP 地址")
            host = self.host_var.get().strip() or "simulation"
            settings = InstrumentSettings(host=host, port=port)
            settings.validate()
        except (ValueError, OSError) as exc:
            messagebox.showerror("连接参数错误", str(exc), parent=self)
            return
        fingerprint = self._connection_fingerprint()
        self._connection_test_id = self.connection_gate.begin_test(fingerprint)
        test_id = self._connection_test_id
        self.test_connection_button.configure(state=tk.DISABLED)
        self.start_button.configure(state=tk.DISABLED)
        self.status_var.set("正在测试连接…")

        def worker() -> None:
            source = Simulated53230A(settings.instrument) if settings.simulated else Keysight53230A(settings.instrument)
            try:
                identity = source.test_connection() if hasattr(source, "test_connection") else source.connect()
                self.events.put(
                    SessionEvent(
                        "connection_test_ok",
                        message=identity.raw if hasattr(identity, "raw") else identity,
                        connection_fingerprint=fingerprint,
                        connection_test_id=test_id,
                    )
                )
            except Exception as exc:
                self.events.put(
                    SessionEvent(
                        "connection_test_error",
                        message=str(exc),
                        connection_fingerprint=fingerprint,
                        connection_test_id=test_id,
                    )
                )
            finally:
                source.close()

        threading.Thread(target=worker, name="connection-test", daemon=True).start()

    def _write_and_check_parameters(self) -> None:
        connection = self._connection_fingerprint()
        if not self.connection_gate.is_verified(connection):
            messagebox.showwarning(
                "需要先测试连接", "请先完成当前仪器的连接测试。", parent=self
            )
            self._update_parameter_button_state()
            return
        try:
            settings = self._session_settings()
        except (ValueError, OSError) as exc:
            messagebox.showerror("参数错误", str(exc), parent=self)
            return
        fingerprint = self._parameter_fingerprint()
        self._parameter_test_id = self.connection_gate.begin_parameter_test(fingerprint)
        test_id = self._parameter_test_id
        self.parameter_check_button.configure(state=tk.DISABLED)
        self.start_button.configure(state=tk.DISABLED)
        self.status_var.set("正在写入并检查参数…")

        def worker() -> None:
            source = (
                Simulated53230A(settings.instrument)
                if settings.simulated
                else Keysight53230A(settings.instrument)
            )
            try:
                source.connect()
                checks = source.configure_and_validate()
                self.events.put(
                    SessionEvent(
                        "parameter_test_ok",
                        parameter_fingerprint=fingerprint,
                        connection_test_id=test_id,
                        parameter_checks=tuple(checks),
                    )
                )
            except ParameterValidationError as exc:
                self.events.put(
                    SessionEvent(
                        "parameter_test_error",
                        message=str(exc),
                        parameter_fingerprint=fingerprint,
                        connection_test_id=test_id,
                        parameter_checks=tuple(exc.checks),
                    )
                )
            except Exception as exc:
                self.events.put(
                    SessionEvent(
                        "parameter_test_error",
                        message=str(exc),
                        parameter_fingerprint=fingerprint,
                        connection_test_id=test_id,
                    )
                )
            finally:
                source.close()

        threading.Thread(target=worker, name="parameter-test", daemon=True).start()

    def _stop(self) -> None:
        if self.session is not None:
            self.status_var.set("正在停止并保存剩余数据…")
            self.stop_button.configure(state=tk.DISABLED)
            self.session.stop()

    def _poll_events(self) -> None:
        redraw_frequency = False
        try:
            while True:
                event = self.events.get_nowait()
                if event.kind == "connection_test_ok":
                    accepted = self.connection_gate.complete_success(
                        event.connection_fingerprint or self._connection_fingerprint(),
                        event.connection_test_id,
                    )
                    if not accepted:
                        continue
                    self.test_connection_button.configure(state=tk.NORMAL)
                    self._update_parameter_button_state()
                    self._update_start_button_state()
                    self.status_var.set("连接测试成功")
                    messagebox.showinfo("连接测试成功", self._format_identity(event.message), parent=self)
                elif event.kind == "connection_test_error":
                    accepted = self.connection_gate.complete_failure(event.connection_test_id)
                    if not accepted:
                        continue
                    self.test_connection_button.configure(state=tk.NORMAL)
                    self.parameter_check_button.configure(state=tk.DISABLED)
                    self._update_start_button_state()
                    self.status_var.set(f"连接测试失败 · {event.message}")
                    messagebox.showerror("连接测试失败", event.message, parent=self)
                elif event.kind == "parameter_test_ok":
                    accepted = self.connection_gate.complete_parameter_success(
                        event.parameter_fingerprint or self._parameter_fingerprint(),
                        event.connection_test_id,
                    )
                    if not accepted:
                        continue
                    self.parameter_check_button.configure(state=tk.NORMAL)
                    self._update_start_button_state()
                    passed = sum(item.passed for item in event.parameter_checks)
                    total = len(event.parameter_checks)
                    self.status_var.set(f"参数检查成功 · {passed}/{total} 项通过")
                    messagebox.showinfo(
                        "参数检查成功",
                        self._format_parameter_checks(event.parameter_checks),
                        parent=self,
                    )
                elif event.kind == "parameter_test_error":
                    accepted = self.connection_gate.complete_parameter_failure(
                        event.connection_test_id
                    )
                    if not accepted:
                        continue
                    self._update_parameter_button_state()
                    self._update_start_button_state()
                    self.status_var.set(f"参数检查失败 · {event.message}")
                    messagebox.showerror("参数写入/检查失败", event.message, parent=self)
                elif event.kind == "connected":
                    self.current_session_dir = event.session_dir
                    self.status_var.set(f"已连接 · 等待首个有效读数 · {event.message}")
                elif event.kind == "started":
                    self.current_session_dir = event.session_dir
                    self.status_var.set(f"采集中 · {event.message}")
                elif event.kind == "progress":
                    self.current_segment = event.segment_index
                    self.count_var.set(f"{event.sample_count:,}")
                    self.elapsed_var.set(str(timedelta(seconds=int(event.elapsed_s))))
                    self.latest_var.set(f"{event.latest_hz:.12g}")
                    redraw_frequency = True
                elif event.kind == "segment":
                    self.current_segment = event.segment_index
                    self.latest_result = None
                    self.allan_line.set_data([], [])
                    self.hadamard_line.set_data([], [])
                    self.canvas.draw_idle()
                    self.status_var.set(
                        f"采集中 · 已进入连续段 {event.segment_index + 1}，段间边界不参与偏差计算"
                    )
                elif event.kind == "analysis" and event.result is not None:
                    if event.result.continuity_segment != self.current_segment:
                        continue
                    self.latest_result = event.result
                    self.reference_display_var.set(f"{event.result.reference_hz:.12g}")
                    self._refresh_deviation_plot()
                elif event.kind == "analysis_error":
                    self.status_var.set(f"采集中 · 分析暂不可用：{event.message}")
                elif event.kind == "error":
                    self.connection_gate.invalidate()
                    self._update_start_button_state()
                    self.status_var.set(f"采集错误 · {event.message}")
                    messagebox.showerror("采集错误", event.message, parent=self)
                elif event.kind == "stopped":
                    self.count_var.set(f"{event.sample_count:,}")
                    self.elapsed_var.set(str(timedelta(seconds=int(event.elapsed_s))))
                    self.status_var.set(
                        f"已停止 · 数据已保存到 {event.session_dir}"
                        if not event.message
                        else f"已停止 · {event.message}"
                    )
                    self._set_config_enabled(True)
                    self.test_connection_button.configure(state=tk.NORMAL)
                    self._update_parameter_button_state()
                    if event.message:
                        self.connection_gate.invalidate()
                    self._update_start_button_state()
                    self.stop_button.configure(state=tk.DISABLED)
        except queue.Empty:
            pass
        if redraw_frequency:
            self._refresh_frequency_plot()
        self.after(100, self._poll_events)

    def _refresh_frequency_plot(self) -> None:
        if self.session is None:
            return
        values, _, _, _ = self.session.buffer.snapshot(last_points=5000)
        if not values.size:
            return
        gate = self.session.actual_gate_time_s
        time_axis = (np.arange(values.size) - values.size + 1) * gate
        self.frequency_line.set_data(time_axis, values)
        self.frequency_axis.relim()
        self.frequency_axis.autoscale_view()
        self.frequency_axis.ticklabel_format(axis="y", style="plain", useOffset=True)
        self.canvas.draw_idle()

    def _refresh_deviation_plot(self) -> None:
        result = self.latest_result
        if result is None:
            return
        self.allan_line.set_data(
            result.tau_seconds if self.show_allan_var.get() else [],
            result.allan_deviation if self.show_allan_var.get() else [],
        )
        self.hadamard_line.set_data(
            result.tau_seconds if self.show_hadamard_var.get() else [],
            result.hadamard_deviation if self.show_hadamard_var.get() else [],
        )
        self.deviation_axis.relim()
        self.deviation_axis.autoscale_view()
        self.deviation_axis.legend(loc="best", fontsize=8)
        self.canvas.draw_idle()

    @staticmethod
    def _format_identity(raw: str) -> str:
        fields = [item.strip() for item in raw.split(",")]
        labels = ("厂商", "型号", "序列号", "固件")
        return "\n".join(
            f"{label}: {value}" for label, value in zip(labels, fields, strict=False)
        )

    @staticmethod
    def _format_parameter_checks(checks: tuple[ParameterCheck, ...]) -> str:
        if not checks:
            return "模拟模式参数检查通过。"
        lines = []
        for item in checks:
            marker = "通过" if item.passed else "失败"
            lines.append(f"{marker} · {item.name}: {item.actual}（期望 {item.expected}）")
        return "\n".join(lines)

    def _on_close(self) -> None:
        if self.session is not None and self.session.is_running:
            self.session.stop()
            self.status_var.set("正在停止并保存剩余数据…")
            self._close_deadline = time.monotonic() + 5.0
            self.after(100, self._close_when_finished)
        else:
            self.destroy()

    def _close_when_finished(self) -> None:
        if self.session is not None and self.session.is_running and (
            self._close_deadline is None or time.monotonic() < self._close_deadline
        ):
            self.after(100, self._close_when_finished)
        elif self.session is not None and self.session.is_running:
            self.status_var.set("仪器未响应，已超时关闭窗口；后台会话将自行结束")
            self.session.stop()
            self.destroy()
        else:
            self.destroy()


def main() -> None:
    app = StabilityAnalyzerApp()
    app.mainloop()
