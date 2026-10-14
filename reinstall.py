"""Windows Setup launcher for an ISO stored outside the application directory."""

from __future__ import annotations

import json
import os
import platform
import subprocess
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ReinstallPlan:
    iso: Path
    mode: str
    driver_dir: Path | None
    driver_count: int
    size_gb: float
    computer: str
    architecture: str


def validate_plan(iso_name: str, mode: str, driver_name: str = "", application_dir: Path | None = None) -> ReinstallPlan:
    if os.name != "nt":
        raise RuntimeError("系统重装功能仅支持 Windows。")
    if not iso_name.strip():
        raise ValueError("请先选择 Windows ISO 镜像。")
    iso = Path(iso_name).expanduser().resolve()
    if application_dir and iso.is_relative_to(application_dir.resolve()):
        raise ValueError("请把系统镜像放在程序目录之外。")
    if iso.suffix.lower() != ".iso" or not iso.is_file():
        raise ValueError("请选择存在的 Windows ISO 镜像文件。")
    if iso.stat().st_size < 500 * 1024 * 1024:
        raise ValueError("ISO 小于 500 MB，可能不是完整的 Windows 安装镜像。")
    if mode not in ("upgrade", "clean"):
        raise ValueError("安装模式无效。")
    driver_dir = Path(driver_name).expanduser().resolve() if driver_name.strip() else None
    if driver_dir and not driver_dir.is_dir():
        raise ValueError("驱动目录不存在。")
    driver_count = sum(1 for _ in driver_dir.rglob("*.inf")) if driver_dir else 0
    if driver_dir and not driver_count:
        raise ValueError("驱动目录中没有 INF 文件。")
    return ReinstallPlan(
        iso=iso,
        mode=mode,
        driver_dir=driver_dir,
        driver_count=driver_count,
        size_gb=round(iso.stat().st_size / 1024**3, 2),
        computer=platform.node(),
        architecture=platform.machine(),
    )


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def mount_iso(iso: Path) -> Path:
    """Mount and return the drive root. Existing mounts are reused."""
    if iso.suffix.lower() != ".iso" or not iso.is_file():
        raise ValueError("ISO 文件不存在。")
    script = (
        f"$image=Get-DiskImage -ImagePath {_powershell_quote(str(iso))} -ErrorAction Stop; "
        "if (-not $image.Attached) { $image=Mount-DiskImage -ImagePath $image.ImagePath -PassThru -ErrorAction Stop }; "
        "$volume=$image | Get-Volume -ErrorAction Stop | Select-Object -First 1; "
        "if (-not $volume.DriveLetter) { throw '镜像已挂载但未获得盘符' }; "
        "Write-Output ($volume.DriveLetter + ':\\')"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
    )
    if completed.returncode:
        raise RuntimeError(f"挂载 ISO 失败：{completed.stderr.strip() or completed.stdout.strip()}")
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("未获取到 ISO 的盘符。")
    root = Path(lines[-1])
    if not root.drive or not root.is_dir():
        raise RuntimeError("ISO 挂载盘符无效。")
    if not (root / "setup.exe").is_file() or not any((root / "sources" / name).is_file() for name in ("install.wim", "install.esd", "install.swm")):
        raise ValueError("镜像中未找到 Windows setup.exe 和 install.wim/esd/swm。")
    return root


def setup_command(plan: ReinstallPlan, mounted_root: Path) -> list[str]:
    if plan.mode != "upgrade":
        raise ValueError("全新安装必须从启动介质进入 Windows Setup，不能从当前桌面直接执行。")
    setup = mounted_root / "setup.exe"
    if not mounted_root.drive or not mounted_root.is_dir():
        raise ValueError("Windows 安装介质盘符无效。")
    if not setup.is_file():
        raise FileNotFoundError(f"未找到 Windows Setup：{setup}")
    # Leave Setup interactive so the operator can review its keep-apps-and-files choice.
    command = [str(setup), "/DynamicUpdate", "Disable"]
    # Drivers are installed in a separate post-install step; interactive Setup stays under operator control.
    return command


def launch_setup(plan: ReinstallPlan, log_dir: Path) -> tuple[list[str], Path]:
    root = mount_iso(plan.iso)
    command = setup_command(plan, root)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"windows-setup-launch-{datetime.now():%Y%m%d-%H%M%S}.json"
    log_path.write_text(json.dumps({
        "iso": str(plan.iso), "mode": plan.mode, "driver_dir": str(plan.driver_dir) if plan.driver_dir else None,
        "driver_inf_count": plan.driver_count, "command": command, "note": "Setup 已启动；安装结果请以 Windows Setup 界面和日志为准。",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    subprocess.Popen(command, cwd=str(root), shell=False)
    return command, log_path


def prepare_clean_install(plan: ReinstallPlan, log_dir: Path) -> Path:
    if plan.mode != "clean":
        raise ValueError("请选择全新安装模式。")
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"clean-install-plan-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps({
        "image": str(plan.iso), "image_size_gb": plan.size_gb,
        "driver_folder": str(plan.driver_dir) if plan.driver_dir else None,
        "driver_inf_count": plan.driver_count,
        "computer": plan.computer, "architecture": plan.architecture,
        "image_validation": "extension, existence and size only; installation media contents not verified",
        "steps": [
            "备份并验证个人文件、浏览器数据和应用配置。",
            "核对镜像来源及发布者提供的 SHA-256；使用已验证的工具将 ISO 写入空 U 盘，此操作会清空 U 盘。",
            "将驱动目录复制到另一个分区或 U 盘；不要只保存在即将清空的系统盘。",
            "从安装 U 盘启动 Windows Setup，在界面中核对目标磁盘和分区。",
            "安装完成后，运行本工具并点击系统重装页的‘重装后安装匹配驱动’。",
        ],
        "status": "preparation-only",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def install_matching_drivers(driver_name: str, log_dir: Path) -> tuple[int, Path]:
    """Stage every INF; PnPUtil installs only packages matching current hardware."""
    directory = Path(driver_name).expanduser().resolve()
    if not directory.is_dir() or not any(directory.rglob("*.inf")):
        raise ValueError("请选择包含 INF 文件的驱动目录。")
    if os.name != "nt":
        raise RuntimeError("驱动安装仅支持 Windows。")
    command = ["pnputil.exe", "/add-driver", "*.inf", "/subdirs", "/install"]
    completed = subprocess.run(command, cwd=str(directory), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=1800, shell=False,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"driver-install-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps({"driver_dir": str(directory), "command": command,
                                "return_code": completed.returncode,
                                "stdout_tail": completed.stdout[-10000:], "stderr_tail": completed.stderr[-10000:]},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    return completed.returncode, path
