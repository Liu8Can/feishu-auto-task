from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .theme import SemanticStatus


def _status_value(status: SemanticStatus | str) -> str:
    try:
        return SemanticStatus(status).value
    except ValueError as exc:
        choices = ", ".join(item.value for item in SemanticStatus)
        raise ValueError(f"未知语义状态 {status!r}，可选值：{choices}") from exc


def _set_dynamic_property(widget: QWidget, name: str, value: str) -> None:
    widget.setProperty(name, value)
    style = widget.style()
    if style is not None:
        style.unpolish(widget)
        style.polish(widget)
    widget.update()


class StatusPill(QLabel):
    def __init__(
        self,
        text: str = "",
        status: SemanticStatus | str = SemanticStatus.NEUTRAL,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(text, parent)
        self.setProperty("uiRole", "statusPill")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.set_status(status)

    @property
    def status(self) -> str:
        return str(self.property("status"))

    def set_status(self, status: SemanticStatus | str) -> None:
        _set_dynamic_property(self, "status", _status_value(status))


class FeedbackBar(QFrame):
    def __init__(
        self,
        title: str = "",
        message: str = "",
        status: SemanticStatus | str = SemanticStatus.INFO,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("uiRole", "feedback")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(3)
        self.title_label = QLabel(title)
        self.title_label.setProperty("uiRole", "feedbackTitle")
        self.message_label = QLabel(message)
        self.message_label.setProperty("uiMuted", True)
        self.message_label.setWordWrap(True)
        layout.addWidget(self.title_label)
        layout.addWidget(self.message_label)
        self.set_feedback(title, message, status)

    @property
    def status(self) -> str:
        return str(self.property("status"))

    def set_feedback(
        self,
        title: str,
        message: str,
        status: SemanticStatus | str = SemanticStatus.INFO,
    ) -> None:
        self.title_label.setText(title)
        self.message_label.setText(message)
        self.title_label.setVisible(bool(title))
        self.message_label.setVisible(bool(message))
        _set_dynamic_property(self, "status", _status_value(status))


class DiagnosticRow(QFrame):
    def __init__(
        self,
        label: str,
        value: str = "",
        *,
        status_text: str = "待检查",
        status: SemanticStatus | str = SemanticStatus.NEUTRAL,
        detail: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("uiRole", "diagnosticRow")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 10, 0, 10)
        layout.setSpacing(12)
        text_layout = QVBoxLayout()
        text_layout.setSpacing(2)
        self.label = QLabel(label)
        self.value_label = QLabel(value)
        self.value_label.setProperty("uiMuted", True)
        self.value_label.setWordWrap(True)
        text_layout.addWidget(self.label)
        text_layout.addWidget(self.value_label)
        layout.addLayout(text_layout, 1)
        self.status_pill = StatusPill(status_text, status)
        self.status_pill.setToolTip(detail)
        layout.addWidget(self.status_pill, 0, Qt.AlignmentFlag.AlignVCenter)

    def set_result(
        self,
        value: str,
        status_text: str,
        status: SemanticStatus | str,
        *,
        detail: str = "",
    ) -> None:
        self.value_label.setText(value)
        self.status_pill.setText(status_text)
        self.status_pill.set_status(status)
        self.status_pill.setToolTip(detail)


class TimelineRow(QFrame):
    def __init__(
        self,
        time_text: str,
        title: str,
        detail: str = "",
        *,
        status: SemanticStatus | str = SemanticStatus.NEUTRAL,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("uiRole", "timelineRow")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 10, 0, 10)
        layout.setSpacing(12)
        self.marker = QFrame()
        self.marker.setProperty("uiRole", "timelineMarker")
        layout.addWidget(self.marker, 0, Qt.AlignmentFlag.AlignTop)
        self.time_label = QLabel(time_text)
        self.time_label.setProperty("uiMuted", True)
        self.time_label.setFixedWidth(58)
        layout.addWidget(self.time_label, 0, Qt.AlignmentFlag.AlignTop)
        text_layout = QVBoxLayout()
        text_layout.setSpacing(2)
        self.title_label = QLabel(title)
        self.detail_label = QLabel(detail)
        self.detail_label.setProperty("uiMuted", True)
        self.detail_label.setWordWrap(True)
        text_layout.addWidget(self.title_label)
        text_layout.addWidget(self.detail_label)
        layout.addLayout(text_layout, 1)
        self.set_entry(time_text, title, detail, status=status)

    @property
    def status(self) -> str:
        return str(self.marker.property("status"))

    def set_entry(
        self,
        time_text: str,
        title: str,
        detail: str = "",
        *,
        status: SemanticStatus | str = SemanticStatus.NEUTRAL,
    ) -> None:
        self.time_label.setText(time_text)
        self.title_label.setText(title)
        self.detail_label.setText(detail)
        self.detail_label.setVisible(bool(detail))
        _set_dynamic_property(self.marker, "status", _status_value(status))
