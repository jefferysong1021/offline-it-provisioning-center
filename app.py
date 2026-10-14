from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from monitor_panel import MonitorPanel
from reinstall import install_matching_drivers, launch_setup, prepare_clean_install, validate_plan


APP_NAME = "IT Provisioning Center"


def application_root() -> Path:
    """Use the executable directory when frozen by PyInstaller."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = application_root()
PACKAGE_DIR = ROOT / "packages"
DRIVER_DIR = ROOT / "drivers"
LOG_DIR = ROOT / "logs"
MANIFEST_PATH = ROOT / "package_manifest.json"
INSTALLER_EXTENSIONS = {".exe", ".msi", ".msix", ".msixbundle", ".appx", ".inf", ".cab", ".ps1", ".bat", ".cmd"}

PROVIDERS = {
    "QQ 邮箱 / QQ Mail": {
        "domain": "qq.com",
        "pop_host": "pop.qq.com",
        "pop_port": "995",
        "pop_security": "SSL/TLS",
        "smtp_host": "smtp.qq.com",
        "smtp_port": "465",
        "smtp_security": "SSL/TLS",
        "imap_host": "imap.qq.com",
        "imap_port": "993",
        "imap_security": "SSL/TLS",
        "password_note": "QQ 邮箱通常需要授权码，不是网页登录密码。",
    },
    "163 邮箱 / 163 Mail": {
        "domain": "163.com",
        "pop_host": "pop.163.com",
        "pop_port": "995",
        "pop_security": "SSL/TLS",
        "smtp_host": "smtp.163.com",
        "smtp_port": "465",
        "smtp_security": "SSL/TLS",
        "imap_host": "imap.163.com",
        "imap_port": "993",
        "imap_security": "SSL/TLS",
        "password_note": "163 邮箱通常需要客户端授权密码。",
    },
    "Gmail": {
        "domain": "gmail.com",
        "pop_host": "pop.gmail.com",
        "pop_port": "995",
        "pop_security": "SSL/TLS",
        "smtp_host": "smtp.gmail.com",
        "smtp_port": "465",
        "smtp_security": "SSL/TLS",
        "imap_host": "imap.gmail.com",
        "imap_port": "993",
        "imap_security": "SSL/TLS",
        "password_note": "建议使用应用专用密码或 OAuth，不要使用主密码。",
    },
    "自定义 / Custom": {
        "domain": "",
        "pop_host": "",
        "pop_port": "995",
        "pop_security": "SSL/TLS",
        "smtp_host": "",
        "smtp_port": "465",
        "smtp_security": "SSL/TLS",
        "imap_host": "",
        "imap_port": "993",
        "imap_security": "SSL/TLS",
        "password_note": "请使用服务商提供的客户端密码或授权码。",
    },
}


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "item"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_admin() -> bool:
    if os.name != "nt":
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def load_manifest() -> list[dict]:
    configured: list[dict] = []
    configured_files: set[str] = set()
    if MANIFEST_PATH.exists():
        try:
            data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict) or not item.get("file"):
                        continue
                    entry = dict(item)
                    entry.setdefault("id", safe_name(Path(entry["file"]).stem))
                    entry.setdefault("name", entry["id"])
                    entry.setdefault("category", "driver" if "driver" in str(entry["file"]).lower() else "package")
                    entry.setdefault("type", Path(entry["file"]).suffix.removeprefix(".").lower())
                    entry.setdefault("silent_args", [])
                    entry.setdefault("requires_admin", entry["type"] in ("msi", "msix", "msixbundle", "appx", "inf", "cab"))
                    entry.setdefault("sha256", "")
                    configured.append(entry)
                    configured_files.add(str(Path(entry["file"])).replace("/", "\\").lower())
        except json.JSONDecodeError:
            pass
    discovered: list[dict] = []
    used_ids = {str(item.get("id")) for item in configured}
    scan_roots = ((PACKAGE_DIR, "package"), (DRIVER_DIR, "driver"))
    for folder, category in scan_roots:
        if not folder.exists():
            continue
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in INSTALLER_EXTENSIONS:
                continue
            relative = str(path.relative_to(ROOT)).replace("/", "\\")
            if relative.lower() in configured_files:
                continue
            base_id = safe_name(path.stem)
            item_id = base_id
            counter = 2
            while item_id in used_ids:
                item_id = f"{base_id}-{counter}"
                counter += 1
            used_ids.add(item_id)
            discovered.append({
                "id": item_id,
                "name": path.stem,
                "category": category,
                "type": path.suffix.removeprefix(".").lower(),
                "file": relative,
                "silent_args": [],
                "requires_admin": category == "driver" or path.suffix.lower() in (".msi", ".msix", ".msixbundle", ".appx", ".inf", ".cab"),
                "sha256": "",
            })

    # Also support the user's original layout: put installers beside the EXE.
    excluded_root_files = {
        "app.py", "build_portable.ps1", "package_manifest.json", "package_manifest.example.json",
        "readme.md", "start.bat", Path(sys.executable).name.lower(),
    }
    for path in sorted(ROOT.iterdir()):
        if not path.is_file() or path.name.lower() in excluded_root_files or path.suffix.lower() not in INSTALLER_EXTENSIONS - {".ps1", ".bat", ".cmd"}:
            continue
        relative = path.name
        if relative.lower() in configured_files:
            continue
        base_id = safe_name(path.stem)
        item_id = base_id
        counter = 2
        while item_id in used_ids:
            item_id = f"{base_id}-{counter}"
            counter += 1
        used_ids.add(item_id)
        discovered.append({
            "id": item_id,
            "name": path.stem,
            "category": "package",
            "type": path.suffix.removeprefix(".").lower(),
            "file": relative,
            "silent_args": [],
            "requires_admin": path.suffix.lower() in (".msi", ".msix", ".msixbundle", ".appx", ".inf", ".cab"),
            "sha256": "",
        })
    return configured + discovered


def validate_package_path(relative_path: str) -> Path:
    candidate = (ROOT / relative_path).resolve()
    if not candidate.is_file():
        raise FileNotFoundError(f"文件不存在：{candidate}")
    try:
        candidate.relative_to(ROOT)
    except ValueError as exc:
        raise PermissionError("安装包路径必须位于程序目录内") from exc
    return candidate


def build_install_command(item: dict, path: Path) -> list[str]:
    kind = str(item.get("type", path.suffix.removeprefix("."))).lower()
    args = [str(value) for value in item.get("silent_args", [])]
    if kind == "msi":
        return ["msiexec.exe", "/i", str(path), *args]
    if kind == "inf":
        return ["pnputil.exe", "/add-driver", str(path), "/install", *args]
    if kind in ("msix", "msixbundle", "appx"):
        escaped = str(path).replace("'", "''")
        return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "RemoteSigned", "-Command", f"Add-AppxPackage -Path '{escaped}'", *args]
    if kind == "cab":
        return ["dism.exe", "/Online", "/Add-Package", f"/PackagePath:{path}", "/NoRestart", *args]
    if kind == "ps1":
        return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "RemoteSigned", "-File", str(path), *args]
    if kind in ("bat", "cmd"):
        return ["cmd.exe", "/c", str(path), *args]
    return [str(path), *args]


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1180x780")
        self.minsize(980, 680)
        self.configure(background="#0b1220")
        PACKAGE_DIR.mkdir(exist_ok=True)
        DRIVER_DIR.mkdir(exist_ok=True)
        LOG_DIR.mkdir(exist_ok=True)
        self._setup_style()
        self.email_vars: dict[str, tk.StringVar] = {}
        self.package_items: list[dict] = []
        self.package_selected: set[str] = set()
        self._build_ui()
        self.apply_provider_preset()
        self.refresh_packages()

    def _setup_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#0b1220")
        style.configure("Card.TFrame", background="#111827")
        style.configure("TLabel", background="#0b1220", foreground="#d1d5db")
        style.configure("Card.TLabel", background="#111827", foreground="#d1d5db")
        style.configure("Title.TLabel", background="#0b1220", foreground="#f9fafb", font=("Segoe UI", 18, "bold"))
        style.configure("Muted.TLabel", background="#0b1220", foreground="#94a3b8")
        style.configure("TButton", padding=(10, 6))
        style.configure("Treeview", background="#111827", fieldbackground="#111827", foreground="#d1d5db")

    def _build_ui(self) -> None:
        header = ttk.Frame(self)
        header.pack(fill="x", padx=18, pady=(16, 8))
        ttk.Label(header, text=APP_NAME, style="Title.TLabel").pack(side="left")
        self.status = tk.StringVar(value="离线模式 · 不会自动联网")
        ttk.Label(header, textvariable=self.status, style="Muted.TLabel").pack(side="left", padx=18)

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=18, pady=(0, 18))
        email_frame = ttk.Frame(notebook)
        packages_frame = ttk.Frame(notebook)
        reinstall_frame = ttk.Frame(notebook)
        monitor_frame = ttk.Frame(notebook)
        notebook.add(email_frame, text="邮箱配置 / Mail Profile")
        notebook.add(packages_frame, text="软件与驱动 / Packages")
        notebook.add(reinstall_frame, text="系统重装 / Windows Setup")
        notebook.add(monitor_frame, text="实时诊断 / Diagnostics")
        self._build_email_panel(email_frame)
        self._build_package_panel(packages_frame)
        self._build_reinstall_panel(reinstall_frame)
        MonitorPanel(monitor_frame).pack(fill="both", expand=True)

    def _build_reinstall_panel(self, parent) -> None:
        self.iso_path = tk.StringVar()
        self.reinstall_driver_path = tk.StringVar()
        self.reinstall_mode = tk.StringVar(value="upgrade")
        parent.columnconfigure(0, weight=1)
        form = ttk.Frame(parent, style="Card.TFrame")
        form.grid(row=0, column=0, sticky="ew", padx=12, pady=12)
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text="外部 Windows ISO", style="Card.TLabel").grid(row=0, column=0, sticky="w", padx=12, pady=12)
        ttk.Entry(form, textvariable=self.iso_path).grid(row=0, column=1, sticky="ew")
        ttk.Button(form, text="选择 ISO", command=self._choose_iso).grid(row=0, column=2, padx=12)
        ttk.Label(form, text="INF 驱动目录", style="Card.TLabel").grid(row=1, column=0, sticky="w", padx=12, pady=12)
        ttk.Entry(form, textvariable=self.reinstall_driver_path).grid(row=1, column=1, sticky="ew")
        ttk.Button(form, text="选择目录", command=self._choose_driver_dir).grid(row=1, column=2, padx=12)
        ttk.Radiobutton(form, text="原位重装：尽量保留应用与文件", variable=self.reinstall_mode, value="upgrade").grid(row=2, column=0, columnspan=3, sticky="w", padx=12, pady=4)
        ttk.Radiobutton(form, text="全新安装：生成操作计划，另行制作启动盘并在 Setup 中选分区", variable=self.reinstall_mode, value="clean").grid(row=3, column=0, columnspan=3, sticky="w", padx=12, pady=4)
        ttk.Button(form, text="检查镜像与驱动", command=self._check_reinstall).grid(row=4, column=0, padx=12, pady=14, sticky="w")
        ttk.Button(form, text="执行所选流程", command=self._execute_reinstall).grid(row=4, column=1, pady=14, sticky="w")
        ttk.Button(form, text="重装后安装匹配驱动", command=self._install_reinstall_drivers).grid(row=5, column=0, columnspan=3, padx=12, pady=(0, 12), sticky="w")
        self.reinstall_preview = tk.Text(parent, height=17, background="#111827", foreground="#d1d5db", relief="flat", wrap="word")
        self.reinstall_preview.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        parent.rowconfigure(1, weight=1)
        self.reinstall_preview.insert("end", "请选择外部文件夹中的 Windows ISO。原位重装启动 ISO 内的 Windows Setup；全新安装会生成操作计划并要求从安装 U 盘启动。\n\n驱动目录可放在程序目录之外，但全新安装前应复制到不会被清空的介质。")

    def _choose_iso(self) -> None:
        chosen = filedialog.askopenfilename(title="选择 Windows ISO", filetypes=[("ISO 镜像", "*.iso")])
        if chosen:
            self.iso_path.set(chosen)
            self._check_reinstall()

    def _choose_driver_dir(self) -> None:
        chosen = filedialog.askdirectory(title="选择 INF 驱动目录")
        if chosen:
            self.reinstall_driver_path.set(chosen)
            self._check_reinstall()

    def _check_reinstall(self):
        try:
            plan = validate_plan(self.iso_path.get(), self.reinstall_mode.get(), self.reinstall_driver_path.get(), ROOT)
            lines = ["镜像与驱动预检 / Preflight", "=" * 45,
                     f"设备：{plan.computer}  架构：{plan.architecture}", f"镜像：{plan.iso}",
                     f"大小：{plan.size_gb} GB", f"模式：{'原位重装' if plan.mode == 'upgrade' else '全新安装'}",
                     f"驱动目录：{plan.driver_dir or '未指定'}", f"INF 数量：{plan.driver_count}", "",
                     "请在继续前备份并验证重要数据。重装可能改变应用、设置或分区。",
                     "原位重装由 Windows Setup 决定是否能保留应用和文件；语言、版本及架构不兼容时可能不可用。",
                     "全新安装只生成计划并挂载验证镜像；需另行制作启动盘，进入 Windows Setup 选择分区。"]
            self.reinstall_preview.delete("1.0", "end")
            self.reinstall_preview.insert("end", "\n".join(lines))
            return plan
        except Exception as exc:
            messagebox.showerror("预检失败", str(exc))
            return None

    def _execute_reinstall(self) -> None:
        plan = self._check_reinstall()
        if not plan:
            return
        if plan.mode == "upgrade" and not is_admin():
            messagebox.showwarning("需要管理员权限", "请以管理员身份启动本程序，再运行 Windows Setup。")
            return
        if not messagebox.askyesno("确认备份", "你是否已备份并验证重要文件、浏览器数据及应用配置？\nWindows Setup 可能要求重启，并可能改变应用或文件。"):
            return
        if plan.mode == "upgrade":
            if not messagebox.askyesno("启动 Windows Setup", "将挂载所选 ISO 并启动 Windows Setup 原位安装。\n请在 Setup 页面再次核对保留选项。继续？"):
                return
        try:
            if plan.mode == "upgrade":
                _command, path = launch_setup(plan, LOG_DIR)
                messagebox.showinfo("已启动", f"Windows Setup 已打开。请在安装界面确认保留选项。\n记录：{path}")
            else:
                path = prepare_clean_install(plan, LOG_DIR)
                messagebox.showinfo("全新安装准备完成", f"已检查镜像路径和大小，操作计划：\n{path}\n\n请另行核对镜像 SHA-256 并制作安装 U 盘；程序没有清空任何磁盘。")
            self.status.set(f"重装流程：{path.name}")
        except Exception as exc:
            messagebox.showerror("无法继续", str(exc))

    def _install_reinstall_drivers(self) -> None:
        if not is_admin():
            messagebox.showwarning("需要管理员权限", "请以管理员身份启动本程序后安装驱动。")
            return
        directory = self.reinstall_driver_path.get().strip()
        if not directory:
            messagebox.showwarning("缺少目录", "请先选择 INF 驱动目录。")
            return
        if not messagebox.askyesno("安装驱动", f"将扫描以下目录中的 INF，交给 Windows PnPUtil 安装与当前硬件匹配的驱动：\n{directory}\n\n请确认驱动来源可信。继续？"):
            return
        self.status.set("正在安装匹配驱动，请等待…")

        def worker() -> None:
            try:
                code, path = install_matching_drivers(directory, LOG_DIR)
                self.after(0, lambda: self._driver_result(code, path))
            except Exception as exc:
                self.after(0, lambda msg=str(exc): messagebox.showerror("驱动安装失败", msg))

        threading.Thread(target=worker, daemon=True).start()

    def _driver_result(self, code: int, path: Path) -> None:
        self.status.set(f"驱动安装退出码 {code} · {path.name}")
        messagebox.showinfo("驱动安装结束", f"PnPUtil 退出码：{code}\n详情：{path}")

    def _email_field(self, parent, key: str, label: str, show: str | None = None) -> None:
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill="x", padx=16, pady=5)
        ttk.Label(row, text=label, style="Card.TLabel", width=25).pack(side="left")
        value = tk.StringVar()
        self.email_vars[key] = value
        ttk.Entry(row, textvariable=value, show=show or "").pack(side="left", fill="x", expand=True)

    def _build_email_panel(self, parent) -> None:
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=1)
        parent.rowconfigure(1, weight=1)
        left = ttk.Frame(parent, style="Card.TFrame")
        left.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=(0, 10), pady=0)
        right = ttk.Frame(parent, style="Card.TFrame")
        right.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=(10, 0), pady=0)

        ttk.Label(left, text="账号信息 / Account", style="Card.TLabel", font=("Segoe UI", 13, "bold")).pack(anchor="w", padx=16, pady=(16, 8))
        provider_var = tk.StringVar(value="QQ 邮箱 / QQ Mail")
        self.email_vars["provider"] = provider_var
        row = ttk.Frame(left, style="Card.TFrame")
        row.pack(fill="x", padx=16, pady=5)
        ttk.Label(row, text="服务商 / Provider", style="Card.TLabel", width=25).pack(side="left")
        combo = ttk.Combobox(row, textvariable=provider_var, values=list(PROVIDERS), state="readonly")
        combo.pack(side="left", fill="x", expand=True)
        combo.bind("<<ComboboxSelected>>", lambda _event: self.apply_provider_preset())
        self._email_field(left, "email", "邮箱地址 / Email")
        self._email_field(left, "password", "授权码 / App Password", show="*")
        ttk.Label(left, text="密码只保留在内存，不写入配置文件、日志或报告。QQ 邮箱请使用授权码。", style="Card.TLabel", wraplength=420).pack(anchor="w", padx=16, pady=(4, 12))

        ttk.Label(left, text="接收服务器 / Incoming", style="Card.TLabel", font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=16, pady=(4, 6))
        self._email_field(left, "pop_host", "POP 服务器 / POP Host")
        self._email_field(left, "pop_port", "POP 端口 / POP Port")
        self._email_field(left, "pop_security", "安全连接 / Security")
        self._email_field(left, "imap_host", "IMAP 服务器 / IMAP Host")
        self._email_field(left, "imap_port", "IMAP 端口 / IMAP Port")

        ttk.Label(right, text="发送服务器 / Outgoing", style="Card.TLabel", font=("Segoe UI", 13, "bold")).pack(anchor="w", padx=16, pady=(16, 8))
        self._email_field(right, "smtp_host", "SMTP 服务器 / SMTP Host")
        self._email_field(right, "smtp_port", "SMTP 端口 / SMTP Port")
        self._email_field(right, "smtp_security", "安全连接 / Security")
        self._email_field(right, "username", "SMTP 用户名 / SMTP Username")
        self._email_field(right, "display_name", "显示名称 / Display Name")
        self.email_preview = tk.Text(right, height=14, background="#0f172a", foreground="#d1d5db", insertbackground="#fff", relief="flat")
        self.email_preview.pack(fill="both", expand=True, padx=16, pady=(12, 8))
        button_row = ttk.Frame(right, style="Card.TFrame")
        button_row.pack(fill="x", padx=16, pady=(0, 16))
        ttk.Button(button_row, text="生成配置预览", command=self.preview_email).pack(side="left")
        ttk.Button(button_row, text="生成 Thunderbird XML", command=self.export_thunderbird).pack(side="left", padx=8)
        ttk.Button(button_row, text="清空授权码", command=lambda: self.email_vars["password"].set("")).pack(side="right")

    def apply_provider_preset(self) -> None:
        provider = self.email_vars.get("provider", tk.StringVar()).get()
        preset = PROVIDERS.get(provider, PROVIDERS["自定义 / Custom"])
        for key in ("pop_host", "pop_port", "pop_security", "smtp_host", "smtp_port", "smtp_security", "imap_host", "imap_port", "imap_security"):
            if key in preset and key in self.email_vars:
                self.email_vars[key].set(preset[key])
        self.email_vars.setdefault("username", tk.StringVar()).set(self.email_vars.get("email", tk.StringVar()).get())

    def email_data(self) -> dict:
        data = {key: value.get().strip() for key, value in self.email_vars.items()}
        data["password_present"] = bool(data.get("password"))
        data["password"] = ""
        data["generated_at"] = now()
        return data

    def preview_email(self) -> None:
        data = self.email_data()
        self.email_preview.delete("1.0", "end")
        lines = [
            "邮箱配置预览 / Mail Profile Preview",
            "=" * 42,
            f"账号 / Account: {data.get('email', '')}",
            f"POP: {data.get('pop_host', '')}:{data.get('pop_port', '')} ({data.get('pop_security', '')})",
            f"IMAP: {data.get('imap_host', '')}:{data.get('imap_port', '')}",
            f"SMTP: {data.get('smtp_host', '')}:{data.get('smtp_port', '')} ({data.get('smtp_security', '')})",
            f"密码是否已输入 / Password present: {data['password_present']}",
            "",
            "安全说明 / Security:",
            "- 密码不会写入配置文件或安装日志。",
            "- QQ 邮箱通常需要授权码，而不是网页登录密码。",
            "- 实际客户端可能仍要求首次启动时输入授权码。",
        ]
        self.email_preview.insert("end", "\n".join(lines))
        self.status.set("已生成邮箱配置预览 · 未连接外部服务")

    def export_thunderbird(self) -> None:
        data = self.email_data()
        if not data.get("email") or not data.get("pop_host") or not data.get("smtp_host"):
            messagebox.showwarning("信息不完整", "请先填写邮箱、POP 和 SMTP 服务器。")
            return
        domain = data["email"].split("@")[-1]
        xml = f'''<?xml version="1.0"?>
<clientConfig version="1.1">
  <emailProvider id="{domain}">
    <domain>{domain}</domain>
    <displayName>{domain} Mail</displayName>
    <incomingServer type="pop3">
      <hostname>{data['pop_host']}</hostname>
      <port>{data['pop_port']}</port>
      <socketType>{data['pop_security']}</socketType>
      <username>%EMAILADDRESS%</username>
      <authentication>password-cleartext</authentication>
    </incomingServer>
    <outgoingServer type="smtp">
      <hostname>{data['smtp_host']}</hostname>
      <port>{data['smtp_port']}</port>
      <socketType>{data['smtp_security']}</socketType>
      <username>%EMAILADDRESS%</username>
      <authentication>password-cleartext</authentication>
    </outgoingServer>
  </emailProvider>
</clientConfig>
'''
        path = filedialog.asksaveasfilename(defaultextension=".xml", filetypes=[("XML 文件", "*.xml")], initialfile=f"autoconfig-{safe_name(domain)}.xml")
        if not path:
            return
        Path(path).write_text(xml, encoding="utf-8")
        self.status.set(f"已生成 Thunderbird 配置：{Path(path).name}")
        messagebox.showinfo("生成完成", "配置文件已生成。密码未写入文件，请在客户端首次使用时输入授权码。")

    def _build_package_panel(self, parent) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        toolbar = ttk.Frame(parent)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(toolbar, text="将安装包放进程序目录的 packages/，驱动放进 drivers/；建议使用 package_manifest.json 管理静默参数。", style="Muted.TLabel").pack(side="left")
        ttk.Button(toolbar, text="刷新清单", command=self.refresh_packages).pack(side="right")
        ttk.Button(toolbar, text="全选", command=self.select_all).pack(side="right", padx=6)
        ttk.Button(toolbar, text="取消选择", command=self.clear_selection).pack(side="right", padx=6)
        ttk.Button(toolbar, text="安装选中项", command=self.install_selected).pack(side="right", padx=6)

        table_frame = ttk.Frame(parent)
        table_frame.grid(row=1, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        self.package_tree = ttk.Treeview(table_frame, columns=("selected", "name", "category", "type", "file", "admin", "status"), show="headings")
        columns = (("selected", "选择", 60), ("name", "名称 / Name", 180), ("category", "类别", 90), ("type", "类型", 70), ("file", "文件", 370), ("admin", "管理员权限", 90), ("status", "状态", 120))
        for key, title, width in columns:
            self.package_tree.heading(key, text=title)
            self.package_tree.column(key, width=width, anchor="w")
        self.package_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.package_tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.package_tree.configure(yscrollcommand=scrollbar.set)
        self.package_tree.bind("<Double-1>", self.toggle_package)
        ttk.Label(parent, text="双击行选择/取消。安装前会校验路径和 SHA-256；驱动或 MSI 通常需要管理员权限。", style="Muted.TLabel").grid(row=2, column=0, sticky="w", pady=(8, 0))

    def refresh_packages(self) -> None:
        self.package_items = load_manifest()
        self.package_selected.clear()
        for item in self.package_tree.get_children():
            self.package_tree.delete(item)
        for item in self.package_items:
            path = ROOT / item.get("file", "")
            self.package_tree.insert("", "end", iid=item["id"], values=("☐", item.get("name", item["id"]), item.get("category", "package"), item.get("type", path.suffix), item.get("file", ""), "是" if item.get("requires_admin") else "否", "ready" if path.is_file() else "missing"))
        self.status.set(f"已扫描 {len(self.package_items)} 个安装项 · 目录：{ROOT}")

    def toggle_package(self, event=None) -> None:
        item_id = self.package_tree.identify_row(event.y) if event else None
        if not item_id:
            return
        if item_id in self.package_selected:
            self.package_selected.remove(item_id)
            selected = "☐"
        else:
            self.package_selected.add(item_id)
            selected = "☑"
        values = list(self.package_tree.item(item_id, "values"))
        values[0] = selected
        self.package_tree.item(item_id, values=values)

    def select_all(self) -> None:
        self.package_selected = {item["id"] for item in self.package_items}
        for item in self.package_items:
            values = list(self.package_tree.item(item["id"], "values"))
            values[0] = "☑"
            self.package_tree.item(item["id"], values=values)

    def clear_selection(self) -> None:
        self.package_selected.clear()
        for item in self.package_items:
            values = list(self.package_tree.item(item["id"], "values"))
            values[0] = "☐"
            self.package_tree.item(item["id"], values=values)

    def install_selected(self) -> None:
        selected = [item for item in self.package_items if item["id"] in self.package_selected]
        if not selected:
            messagebox.showinfo("没有选择", "请先双击选择要安装的软件或驱动。")
            return
        if not messagebox.askyesno("确认安装", f"将按清单安装 {len(selected)} 项。\n请确认安装包来源可信，并接受可能出现的管理员权限提示。"):
            return
        if any(item.get("requires_admin") for item in selected) and not is_admin():
            messagebox.showwarning("需要管理员权限", "选中项包含需要管理员权限的软件或驱动，请使用“以管理员身份运行”重新启动本程序。")
            return
        LOG_DIR.mkdir(exist_ok=True)
        log_path = LOG_DIR / f"install-{datetime.now():%Y%m%d-%H%M%S}.json"
        results = []
        for item in selected:
            result = {"id": item["id"], "name": item.get("name", item["id"]), "started_at": now(), "status": "pending"}
            try:
                path = validate_package_path(item["file"])
                expected = str(item.get("sha256", "")).lower().strip()
                actual = sha256(path)
                result["sha256"] = actual
                if expected and expected != actual.lower():
                    raise ValueError("SHA-256 校验失败，已阻止安装")
                command = build_install_command(item, path)
                result["command"] = command
                completed = subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=int(item.get("timeout_seconds", 1800)), shell=False)
                result["return_code"] = completed.returncode
                result["status"] = "success" if completed.returncode == 0 else "failed"
                result["stdout_tail"] = completed.stdout[-2000:]
                result["stderr_tail"] = completed.stderr[-2000:]
            except Exception as exc:
                result["status"] = "blocked_or_failed"
                result["error"] = str(exc)
            result["finished_at"] = now()
            results.append(result)
            self.package_tree.set(item["id"], "status", result["status"])
        log_path.write_text(json.dumps({"application": APP_NAME, "generated_at": now(), "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
        self.status.set(f"安装流程完成 · 日志：{log_path.name}")
        messagebox.showinfo("安装完成", f"流程已完成，详情见：\n{log_path}")


def self_test() -> None:
    print(json.dumps({
        "application": APP_NAME,
        "platform": platform.platform(),
        "root": str(ROOT),
        "package_dir": str(PACKAGE_DIR),
        "driver_dir": str(DRIVER_DIR),
        "manifest_items": len(load_manifest()),
        "provider_presets": list(PROVIDERS),
        "admin": is_admin(),
        "mode": "offline",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--self-test", action="store_true", help="验证目录、邮箱预设和安装包清单")
    args = parser.parse_args()
    if args.self_test:
        self_test()
    else:
        App().mainloop()
