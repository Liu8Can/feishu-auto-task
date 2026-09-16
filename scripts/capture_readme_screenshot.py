from __future__ import annotations

import os
import tempfile
from pathlib import Path

from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication

from clockout.config import AppConfig, save_config
from clockout.qt_app import AppController
from clockout.runtime import RuntimePaths


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "images" / "app-overview.png"


def main() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    with tempfile.TemporaryDirectory(prefix="feishu-readme-") as temporary_dir:
        base = Path(temporary_dir)
        paths = RuntimePaths(
            base_dir=base,
            config_path=base / "config.json",
            state_path=base / "state.json",
            log_dir=base / "logs",
        )
        save_config(
            paths.config_path,
            AppConfig(
                monitor_enabled=False,
                start_with_windows=False,
                trusted_container_fingerprint="",
                theme_mode="light",
            ),
        )
        app = QApplication([])
        QFontDatabase.addApplicationFont(r"C:\Windows\Fonts\msyh.ttc")
        controller = AppController(app, paths, background=False)
        try:
            app.processEvents()
            OUTPUT.parent.mkdir(parents=True, exist_ok=True)
            if not controller.window.grab().save(str(OUTPUT)):
                raise RuntimeError("README 截图保存失败")
        finally:
            controller.quit()
            for handler in tuple(controller.logger.handlers):
                handler.close()
                controller.logger.removeHandler(handler)


if __name__ == "__main__":
    main()
