from __future__ import annotations

from clockout.session import is_interactive_desktop


class FakeUser32:
    def __init__(self, *, desktop: int = 1, switch_result: int = 1) -> None:
        self.desktop = desktop
        self.switch_result = switch_result
        self.closed = False

    def OpenInputDesktop(self, flags: int, inherit: bool, access: int) -> int:
        return self.desktop

    def SwitchDesktop(self, desktop: int) -> int:
        return self.switch_result

    def CloseDesktop(self, desktop: int) -> int:
        self.closed = True
        return 1


def test_interactive_desktop_requires_switchable_input_desktop() -> None:
    api = FakeUser32(switch_result=1)

    assert is_interactive_desktop(api)
    assert api.closed


def test_locked_desktop_is_rejected() -> None:
    api = FakeUser32(switch_result=0)

    assert not is_interactive_desktop(api)
    assert api.closed


def test_missing_input_desktop_is_rejected() -> None:
    api = FakeUser32(desktop=0)

    assert not is_interactive_desktop(api)
    assert not api.closed
