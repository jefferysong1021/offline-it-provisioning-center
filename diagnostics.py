"""Read-only Windows performance and layered network probes."""

from __future__ import annotations

import ctypes
import json
import os
import platform
import re
import shutil
import socket
import statistics
import subprocess
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path


NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _powershell(script: str, timeout: int = 8) -> str:
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, creationflags=NO_WINDOW, check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "PowerShell 采样失败")
    return result.stdout.strip()


def _windows_cpu_times() -> tuple[int, int]:
    class FILETIME(ctypes.Structure):
        _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
    if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        raise OSError("GetSystemTimes 失败")

    def value(item: FILETIME) -> int:
        return item.low | (item.high << 32)

    return value(idle), value(kernel) + value(user)


def _windows_memory() -> tuple[int, int]:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_uint32), ("memory_load", ctypes.c_uint32),
            ("total_physical", ctypes.c_uint64), ("available_physical", ctypes.c_uint64),
            ("total_page_file", ctypes.c_uint64), ("available_page_file", ctypes.c_uint64),
            ("total_virtual", ctypes.c_uint64), ("available_virtual", ctypes.c_uint64),
            ("available_extended", ctypes.c_uint64),
        ]
    info = MEMORYSTATUSEX()
    info.length = ctypes.sizeof(info)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(info)):
        raise OSError("GlobalMemoryStatusEx 失败")
    return info.total_physical, info.available_physical


def _gb(value: int) -> float:
    return round(value / 1024**3, 2)


class PerformanceSampler:
    def __init__(self) -> None:
        self.previous = None

    def sample(self) -> dict:
        cpu = None
        memory = {"usage_percent": None, "total_gb": None, "available_gb": None}
        if os.name == "nt":
            idle, total = _windows_cpu_times()
            if self.previous:
                idle_delta, total_delta = idle - self.previous[0], total - self.previous[1]
                if total_delta > 0:
                    cpu = round(max(0, min(100, 100 * (1 - idle_delta / total_delta))), 1)
            self.previous = (idle, total)
            total_mem, available_mem = _windows_memory()
            memory = {
                "usage_percent": round(100 * (1 - available_mem / total_mem), 1),
                "total_gb": _gb(total_mem), "available_gb": _gb(available_mem),
            }
        system_drive = Path(os.environ.get("SystemDrive", "C:") + "\\") if os.name == "nt" else Path("/")
        disk = shutil.disk_usage(system_drive)
        return {
            "collected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "os": platform.platform(),
            "cpu": {"usage_percent": cpu, "logical_cores": os.cpu_count()},
            "memory": memory,
            "system_disk": {
                "usage_percent": round(100 * disk.used / disk.total, 1),
                "free_gb": _gb(disk.free), "total_gb": _gb(disk.total),
                "drive": str(system_drive),
            },
        }


def top_processes() -> list[dict]:
    if os.name != "nt":
        return []
    script = (
        "$cores=[Environment]::ProcessorCount; $before=@{}; "
        "Get-Process | ForEach-Object { try { $before[$_.Id]=@($_.CPU,$_.IOReadBytes,$_.IOWriteBytes) } catch {} }; "
        "Start-Sleep -Milliseconds 350; "
        "$rows=Get-Process | ForEach-Object { try { $old=$before[$_.Id]; if($null -eq $old){ return }; "
        "[pscustomobject]@{ Name=$_.ProcessName; Id=$_.Id; MemoryMB=[math]::Round($_.WorkingSet64/1MB,1); "
        "CpuPercent=[math]::Round([math]::Max(0,($_.CPU-$old[0])/0.35/$cores*100),1); "
        "ReadMBps=[math]::Round([math]::Max(0,($_.IOReadBytes-$old[1])/0.35/1MB),2); "
        "WriteMBps=[math]::Round([math]::Max(0,($_.IOWriteBytes-$old[2])/0.35/1MB),2) } "
        "} catch {} }; $rows | Sort-Object CpuPercent,MemoryMB -Descending | Select-Object -First 8 | ConvertTo-Json -Compress"
    )
    data = json.loads(_powershell(script, timeout=10))
    entries = data if isinstance(data, list) else [data]
    return [{"name": row.get("Name"), "pid": row.get("Id"), "memory_mb": row.get("MemoryMB"),
             "cpu_percent": row.get("CpuPercent"), "read_mbps": row.get("ReadMBps"),
             "write_mbps": row.get("WriteMBps")} for row in entries]


def _probe_tcp(host: str, port: int, count: int = 3) -> dict:
    attempts = []
    for _ in range(count):
        started = time.monotonic()
        try:
            with socket.create_connection((host, port), timeout=1.5):
                attempts.append(round((time.monotonic() - started) * 1000, 1))
        except OSError:
            attempts.append(None)
    valid = [item for item in attempts if item is not None]
    return {
        "host": host, "port": port, "method": "TCP connect",
        "attempts_ms": attempts, "failure_percent": round(100 * (count - len(valid)) / count, 1),
        "latency_ms": round(statistics.mean(valid), 1) if valid else None,
        "jitter_ms": round(statistics.mean(abs(b - a) for a, b in zip(valid, valid[1:])), 1) if len(valid) > 1 else None,
        "status": "ok" if valid else "failed",
    }


def _ping(host: str) -> dict:
    if not host:
        return {"status": "not-configured", "latency_ms": None, "method": "ICMP"}
    started = time.monotonic()
    try:
        result = subprocess.run(["ping", "-n", "1", "-w", "1500", host], capture_output=True,
                                timeout=3, creationflags=NO_WINDOW, check=False)
        ok = result.returncode == 0
        return {"status": "ok" if ok else "no-reply", "latency_ms": round((time.monotonic() - started) * 1000, 1) if ok else None,
                "method": "ICMP command elapsed", "note": "耗时含进程启动；无应答可能是 ICMP 被禁。"}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "error", "latency_ms": None, "method": "ICMP", "error": str(exc)}


def _validate_host(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 253 or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for ch in value):
        raise ValueError("域名或主机地址格式无效")
    return value


def _network_config() -> dict:
    if os.name != "nt":
        return {"status": "unsupported", "interface": None, "ipv4": None, "gateway": None, "dns_servers": []}
    script = (
        "$c=Get-NetIPConfiguration | Where-Object {$_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq 'Up'} "
        "| Select-Object -First 1; "
        "if ($c) { [pscustomobject]@{Interface=$c.InterfaceAlias; IPv4=@($c.IPv4Address.IPAddress); "
        "Gateway=@($c.IPv4DefaultGateway.NextHop); DnsServers=@($c.DNSServer.ServerAddresses)} "
        "| ConvertTo-Json -Compress }"
    )
    try:
        raw = _powershell(script, timeout=6)
        if not raw:
            raise RuntimeError("Get-NetIPConfiguration 未返回默认路由")
        data = json.loads(raw)
        return {"status": "ok", "interface": data.get("Interface"), "ipv4": (data.get("IPv4") or [None])[0],
                "gateway": (data.get("Gateway") or [None])[0], "dns_servers": data.get("DnsServers") or []}
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        # Non-admin or sandboxed Windows sessions can deny NetTCPIP cmdlets.
        fallback = _ipconfig_fallback()
        fallback["error"] = str(exc)
        return fallback


def _ipconfig_fallback() -> dict:
    gateway, ipv4 = None, None
    try:
        route = subprocess.run(["route.exe", "print", "0.0.0.0"], capture_output=True, timeout=5,
                               creationflags=NO_WINDOW, check=False)
        route_text = route.stdout.decode("ascii", errors="ignore")
        routes = re.findall(r"(?m)^\s*0\.0\.0\.0\s+0\.0\.0\.0\s+(\d+\.\d+\.\d+\.\d+)\s+(\d+\.\d+\.\d+\.\d+)\s+(\d+)\s*$", route_text)
        if routes:
            gateway, ipv4, _metric = min(routes, key=lambda row: int(row[2]))
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        result = subprocess.run(["ipconfig.exe", "/all"], capture_output=True, timeout=6,
                                creationflags=NO_WINDOW, check=False)
        text = result.stdout.decode("utf-8", errors="replace")
        # ipconfig output is localized; use this as evidence text, not a guessed gateway.
        return {"status": "partial" if gateway else "manual-review", "interface": None, "ipv4": ipv4, "gateway": gateway,
                "dns_servers": [], "ipconfig_excerpt": text[:4000]}
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "partial" if gateway else "error", "interface": None, "ipv4": ipv4, "gateway": gateway, "dns_servers": []}


def _resolve_dns(domain: str) -> dict:
    started = time.monotonic()
    try:
        # PowerShell child process is bounded so a stalled resolver cannot hang the UI worker forever.
        quoted = "'" + domain.replace("'", "''") + "'"
        output = _powershell(f"Resolve-DnsName -Name {quoted} -Type A -ErrorAction Stop | Where-Object IPAddress | Select-Object -First 1 -ExpandProperty IPAddress", timeout=6)
        return {"status": "ok" if output else "failed", "domain": domain, "address": output or None,
                "latency_ms": round((time.monotonic() - started) * 1000, 1) if output else None,
                "method": "Resolve-DnsName command elapsed"}
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return {"status": "failed", "domain": domain, "address": None, "latency_ms": None, "error": str(exc)}


def _https_request(domain: str) -> dict:
    url = f"https://{domain}/"
    started = time.monotonic()
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "IT-Provisioning-Center/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            status = response.status
        return {"status": "ok" if status < 500 else "http-error", "url": url, "http_status": status,
                "latency_ms": round((time.monotonic() - started) * 1000, 1), "method": "HTTPS HEAD"}
    except urllib.error.HTTPError as exc:
        # HTTP 4xx still proves that TLS and the HTTP server responded.
        return {"status": "ok" if exc.code < 500 else "http-error", "url": url, "http_status": exc.code,
                "latency_ms": round((time.monotonic() - started) * 1000, 1), "method": "HTTPS HEAD"}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"status": "failed", "url": url, "http_status": None, "latency_ms": None,
                "method": "HTTPS HEAD", "error": str(exc)}


def sample_network(domain: str, tcp_host: str, tcp_port: int) -> dict:
    domain, tcp_host = _validate_host(domain), _validate_host(tcp_host)
    if not 1 <= tcp_port <= 65535:
        raise ValueError("TCP 端口必须在 1 到 65535 之间")
    with ThreadPoolExecutor(max_workers=5) as pool:
        config_future = pool.submit(_network_config)
        dns_future = pool.submit(_resolve_dns, domain)
        tcp_future = pool.submit(_probe_tcp, tcp_host, tcp_port)
        tcp_443_future = pool.submit(_probe_tcp, domain, 443)
        https_future = pool.submit(_https_request, domain)
        config = config_future.result()
        gateway_future = pool.submit(_ping, config.get("gateway"))
        dns = dns_future.result()
        tcp = tcp_future.result()
        tcp_443 = tcp_443_future.result()
        https = https_future.result()
        gateway = gateway_future.result()
    return {
        "collected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "loopback": _ping("127.0.0.1"), "configuration": config,
        "gateway": {"address": config.get("gateway"), **gateway},
        "dns": dns, "tcp_target": tcp, "tcp_443": tcp_443, "https": https,
        "notes": ["TCP 连接失败率不是 ICMP 丢包率；某目标 443 失败不能单独证明本机 443 被占用。",
                  "网关 ICMP 无应答时，请结合 DNS 和 TCP 结果判断是否禁 Ping。"],
    }


def findings(system: dict | None, network: dict | None) -> list[str]:
    result = []
    if system:
        for key, label, threshold in (("cpu", "CPU", 85), ("memory", "内存", 85), ("system_disk", "系统盘空间", 90)):
            metric = system[key]
            value = metric.get("usage_percent")
            if value is not None and value >= threshold:
                result.append(f"{label}使用率 {value}% ≥ {threshold}%；持续出现时排查高占用进程或可用空间。")
    if network:
        if not network["configuration"].get("gateway"):
            result.append("未读取到有效的 IPv4 默认路由；检查网卡、IP 地址和网关配置。")
        elif network["gateway"]["status"] != "ok":
            result.append("网关 ICMP 无应答；检查链路，同时确认网关是否禁 Ping。")
        if network["dns"]["status"] != "ok":
            result.append("DNS 解析失败；检查 DNS 服务器、VPN 和内网 DNS 策略。")
        if network["tcp_443"]["status"] != "ok":
            result.append("目标域名 TCP 443 建连失败；检查路由、代理、防火墙和服务端。")
        if network["https"]["status"] != "ok":
            result.append("HTTPS 请求未正常完成；检查证书、代理、HTTP 服务和网络策略。")
        for key in ("tcp_target", "tcp_443"):
            item = network[key]
            if item["failure_percent"] >= 5 and item["status"] == "ok":
                result.append(f"{item['host']}:{item['port']} 的 TCP 建连失败率 {item['failure_percent']}%，建议重复采样并对比其他目标。")
            if item["latency_ms"] is not None and item["latency_ms"] >= 150:
                result.append(f"{item['host']}:{item['port']} 平均 TCP 建连耗时 {item['latency_ms']} ms ≥150 ms；对比网关和其他目标。")
            if item["jitter_ms"] is not None and item["jitter_ms"] >= 50:
                result.append(f"{item['host']}:{item['port']} 建连耗时波动 {item['jitter_ms']} ms ≥50 ms；检查 Wi-Fi 信号及后台流量。")
    return result or ["本次采样未触发阈值；如卡顿仍存在，请继续观察趋势和具体进程。"]
