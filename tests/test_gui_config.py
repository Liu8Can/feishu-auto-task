from __future__ import annotations

from clockout.config import AppConfig
from clockout.gui import ClockoutDemoApp
from clockout.storage import JsonStateStore


def test_gui_passes_configured_weekdays_to_engine(tmp_path: object) -> None:
    app = object.__new__(ClockoutDemoApp)
    app.config = AppConfig(weekdays=(0, 2))
    app.adapter = object()
    app.store = JsonStateStore(tmp_path / "state.json")  # type: ignore[operator]

    engine = app._engine()

    assert engine.config.weekdays == frozenset({0, 2})
