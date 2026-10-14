"""Tkinter read-only monitoring panel."""

from __future__ import annotations

import json
import threading
import tkinter as tk
from collections import deque
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from diagnostics import PerformanceSampler, findings, sample_network, top_processes


class MonitorPanel(ttk.Frame):
    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.sampler = PerformanceSampler()
        self.history = deque(maxlen=60)
        self.network_history = deque(maxlen=60)
        self.last_network = None
        self.last_processes = []
        self.running = False
        self.busy = False
        self._generation = 0
        self.domain = tk.StringVar(value="www.microsoft.com")
        self.tcp_host = tk.StringVar(value="www.microsoft.com")
        self.tcp_port = tk.StringVar(value="443")
        self.interval = tk.StringVar(value="3")
        self.network_each_sample = tk.BooleanVar(value=False)
        self.status_text = tk.StringVar(value="点击开始监测；网络探测可手动执行。")
        self.values = {key: tk.StringVar(value="--") for key in ("cpu", "memory", "disk", "gateway", "dns", "tcp", "tcp_443", "https")}
        self._build()

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        bar = ttk.Frame(self)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(bar, text="采样间隔(秒)").pack(side="left")
        ttk.Combobox(bar, textvariable=self.interval, values=("2", "3", "5", "10"), width=4, state="readonly").pack(side="left", padx=5)
        ttk.Checkbutton(bar, text="每次同时探测网络", variable=self.network_each_sample).pack(side="left", padx=5)
        ttk.Button(bar, text="开始监测", command=self.start).pack(side="left", padx=5)
        ttk.Button(bar, text="停止", command=self.stop).pack(side="left")
        ttk.Button(bar, text="立即测网络", command=self.test_network).pack(side="left", padx=5)
        ttk.Button(bar, text="导出 JSON", command=self.export_json).pack(side="right")

        target = ttk.Frame(self)
        target.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        for label, var, width in (("DNS/HTTPS 域名", self.domain, 28), ("TCP 主机", self.tcp_host, 28), ("端口", self.tcp_port, 6)):
            ttk.Label(target, text=label).pack(side="left", padx=(0, 4))
            ttk.Entry(target, textvariable=var, width=width).pack(side="left", padx=(0, 12))
        ttk.Label(target, textvariable=self.status_text).pack(side="right")

        cards = ttk.Frame(self)
        cards.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        for key, label in (("cpu", "CPU"), ("memory", "内存"), ("disk", "系统盘"), ("gateway", "网关"), ("dns", "DNS"), ("tcp", "TCP 目标"), ("tcp_443", "TCP 443"), ("https", "HTTPS")):
            card = ttk.Frame(cards, style="Card.TFrame")
            card.pack(side="left", fill="x", expand=True, padx=3)
            ttk.Label(card, text=label, style="Card.TLabel").pack(anchor="w", padx=8, pady=(7, 1))
            ttk.Label(card, textvariable=self.values[key], style="Card.TLabel", font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=8, pady=(0, 8))

        content = ttk.Frame(self)
        content.grid(row=3, column=0, sticky="nsew")
        content.columnconfigure(0, weight=3)
        content.columnconfigure(1, weight=2)
        content.rowconfigure(0, weight=1)
        self.graph = tk.Canvas(content, background="#111827", highlightthickness=0)
        self.graph.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.graph.bind("<Configure>", lambda _event: self._draw())
        self.details = tk.Text(content, background="#111827", foreground="#d1d5db", wrap="word", relief="flat")
        self.details.grid(row=0, column=1, sticky="nsew")
        self.details.insert("end", "监测依据：CPU ≥85%、内存 ≥85%、系统盘 ≥90% 时提示关注。\n\n网络显示 TCP 建连失败率，不等同于 ICMP 丢包率。")

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._generation += 1
        self.status_text.set("监测中")
        self._schedule(self._generation)

    def stop(self) -> None:
        self.running = False
        self._generation += 1
        self.status_text.set("已停止；历史样本仍可导出")

    def _schedule(self, generation: int) -> None:
        if generation != self._generation or not self.running:
            return
        if not self.busy:
            self._sample(include_network=self.network_each_sample.get())
        self.after(int(self.interval.get()) * 1000, lambda: self._schedule(generation))

    def test_network(self) -> None:
        self._sample(include_network=True)

    def _sample(self, include_network: bool) -> None:
        if self.busy:
            return
        try:
            port = int(self.tcp_port.get())
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            messagebox.showerror("端口无效", "请输入 1 到 65535 之间的端口。")
            return
        domain, host = self.domain.get().strip(), self.tcp_host.get().strip()
        if include_network and (not domain or not host):
            messagebox.showerror("目标不能为空", "请填写 DNS/HTTPS 域名和 TCP 主机。")
            return
        self.busy = True
        self.status_text.set("正在采样…")

        def worker() -> None:
            try:
                system = self.sampler.sample()
                network = sample_network(domain, host, port) if include_network else None
                try:
                    processes = top_processes()
                except Exception:
                    processes = []
                self.after(0, lambda: self._update(system, network, processes))
            except Exception as exc:
                self.after(0, lambda msg=str(exc): self._error(msg))

        threading.Thread(target=worker, daemon=True).start()

    def _error(self, message: str) -> None:
        self.busy = False
        self.status_text.set(f"采样失败：{message[:100]}")

    def _update(self, system: dict, network: dict | None, processes: list[dict]) -> None:
        self.busy = False
        self.history.append(system)
        self.last_processes = processes
        if network:
            self.last_network = network
            self.network_history.append(network)
        self.values["cpu"].set(self._percent(system["cpu"]["usage_percent"]))
        self.values["memory"].set(self._percent(system["memory"]["usage_percent"]))
        self.values["disk"].set(self._percent(system["system_disk"]["usage_percent"]))
        if network:
            self.values["gateway"].set(network["gateway"]["status"])
            self.values["dns"].set(f"{network['dns']['latency_ms']} ms" if network["dns"]["latency_ms"] is not None else "失败")
            self.values["tcp"].set(self._latency(network["tcp_target"]))
            self.values["tcp_443"].set(self._latency(network["tcp_443"]))
            self.values["https"].set(self._latency(network["https"]))
        self.status_text.set(f"最近采样：{system['collected_at'][11:19]} · {len(self.history)}/60")
        detail_lines = ["初步判断 / Findings", *["• " + item for item in findings(system, self.last_network)], "", "高内存进程 / Top Processes"]
        detail_lines.extend(
            f"{item['name']}  PID {item['pid']}  CPU {item['cpu_percent']}%  内存 {item['memory_mb']} MB  读/写 {item['read_mbps']}/{item['write_mbps']} MB/s"
            for item in processes
        )
        if self.last_network:
            config = self.last_network["configuration"]
            dns_servers = ", ".join(str(server) for server in (config.get("dns_servers") or [])) or "未获取"
            detail_lines.extend(["", "网络分层 / Network Layers", f"网卡: {config.get('interface')}  IPv4: {config.get('ipv4')}",
                                 f"网关: {config.get('gateway')}  DNS服务器: {dns_servers}",
                                 f"回环: {self.last_network['loopback']['status']}  网关: {self.last_network['gateway']['status']}",
                                 f"DNS: {self.last_network['dns']['status']} → {self.last_network['dns']['address']}",
                                 f"TCP: {self.last_network['tcp_target']['failure_percent']}% 建连失败；TCP 443: {self.last_network['tcp_443']['failure_percent']}% 建连失败",
                                 f"HTTPS: {self.last_network['https']['status']}  HTTP {self.last_network['https'].get('http_status')}"])
            if config.get("status") != "ok":
                detail_lines.append(f"网卡信息读取状态：{config.get('status')}；可导出 JSON 查看原始采集内容。")
        self.details.delete("1.0", "end")
        self.details.insert("end", "\n".join(detail_lines))
        self._draw()

    @staticmethod
    def _percent(value) -> str:
        return "--" if value is None else f"{value}%"

    @staticmethod
    def _latency(item: dict) -> str:
        return "失败" if item["latency_ms"] is None else f"{item['latency_ms']} ms"

    def _draw(self) -> None:
        canvas = self.graph
        canvas.delete("all")
        width, height = max(canvas.winfo_width(), 200), max(canvas.winfo_height(), 200)
        divider = height * 0.56
        canvas.create_text(16, 18, text="最近 60 次采样 / CPU · 内存 · 系统盘", anchor="w", fill="#d1d5db", font=("Segoe UI", 11, "bold"))
        top, bottom = 48, divider - 18
        for level in (0, 25, 50, 75, 100):
            y = bottom - (bottom - top) * level / 100
            canvas.create_line(45, y, width - 12, y, fill="#263244")
            canvas.create_text(34, y, text=str(level), anchor="e", fill="#94a3b8")
        if len(self.history) >= 2:
            for key, color in (("cpu", "#6dd695"), ("memory", "#82b8d8"), ("system_disk", "#d5a85d")):
                points = []
                for index, entry in enumerate(self.history):
                    value = entry[key].get("usage_percent")
                    if value is None:
                        continue
                    x = 48 + (width - 64) * index / max(1, len(self.history) - 1)
                    y = bottom - (bottom - top) * value / 100
                    points.extend((x, y))
                if len(points) >= 4:
                    canvas.create_line(*points, fill=color, width=2, smooth=True)
        network_top, network_bottom = divider + 35, height - 20
        canvas.create_text(16, divider + 10, text="TCP 目标建连延迟 / ms", anchor="w", fill="#d1d5db", font=("Segoe UI", 11, "bold"))
        for level in (0, 150, 300):
            y = network_bottom - (network_bottom - network_top) * level / 300
            canvas.create_line(45, y, width - 12, y, fill="#263244")
            canvas.create_text(34, y, text=str(level), anchor="e", fill="#94a3b8")
        if len(self.network_history) >= 2:
            points = []
            for index, entry in enumerate(self.network_history):
                value = entry["tcp_target"].get("latency_ms")
                if value is None:
                    if len(points) >= 4:
                        canvas.create_line(*points, fill="#c58de4", width=2)
                    points = []
                    continue
                x = 48 + (width - 64) * index / max(1, len(self.network_history) - 1)
                y = network_bottom - (network_bottom - network_top) * min(value, 300) / 300
                points.extend((x, y))
            if len(points) >= 4:
                canvas.create_line(*points, fill="#c58de4", width=2)

    def export_json(self) -> None:
        if not self.history:
            messagebox.showinfo("暂无样本", "请先执行一次监测。")
            return
        filename = f"pc-diagnosis-{datetime.now():%Y%m%d-%H%M%S}.json"
        chosen = filedialog.asksaveasfilename(defaultextension=".json", initialfile=filename, filetypes=[("JSON", "*.json")])
        if chosen:
            Path(chosen).write_text(json.dumps({"schema_version": "1.0", "system_samples": list(self.history),
                                               "network": self.last_network, "network_samples": list(self.network_history),
                                               "top_processes": self.last_processes,
                                               "findings": findings(self.history[-1], self.last_network)},
                                              ensure_ascii=False, indent=2), encoding="utf-8")
            self.status_text.set(f"报告已导出：{Path(chosen).name}")
