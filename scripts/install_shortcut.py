"""在桌面放一个 quant-desk 的快捷方式。

    python scripts/install_shortcut.py              # 放到桌面
    python scripts/install_shortcut.py --remove     # 拿掉

指向 ``pythonw.exe`` 而不是 ``panel.bat``：bat 会闪一个黑框，而这是个窗口程序。
快捷方式的起始位置必须是项目根，否则 ``data/external.sqlite`` 会按当前目录去解析。
"""

from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "quant-desk"
ICON = ROOT / "scripts" / "panel_ui" / "panel.ico"
SCRIPT = ROOT / "scripts" / "panel.pyw"
PYTHONW = ROOT / ".venv" / "Scripts" / "pythonw.exe"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="创建/删除桌面快捷方式")
    parser.add_argument("--remove", action="store_true")
    args = parser.parse_args(argv)

    try:
        import win32com.client
    except ImportError:
        print("[error] 需要 pywin32（pip install pywin32）")
        return 2

    shell = win32com.client.Dispatch("WScript.Shell")
    desktop = Path(shell.SpecialFolders("Desktop"))
    link = desktop / f"{NAME}.lnk"

    if args.remove:
        link.unlink(missing_ok=True)
        print(f"[ok] 已删除 {link}")
        return 0

    if not PYTHONW.exists():
        print(f"[error] 找不到解释器：{PYTHONW}")
        return 2
    if not SCRIPT.exists():
        print(f"[error] 找不到面板脚本：{SCRIPT}")
        return 2

    shortcut = shell.CreateShortCut(str(link))
    shortcut.TargetPath = str(PYTHONW)
    shortcut.Arguments = f'"{SCRIPT}"'
    shortcut.WorkingDirectory = str(ROOT)
    shortcut.IconLocation = str(ICON) if ICON.exists() else f"{PYTHONW},0"
    shortcut.Description = "quant-desk 实盘面板（只读记账，不下单）"
    shortcut.WindowStyle = 1
    shortcut.Save()
    print(f"[ok] {link}")
    print(f"     目标 {PYTHONW} {SCRIPT.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
