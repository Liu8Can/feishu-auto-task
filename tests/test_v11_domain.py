from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta

import pytest

from clockout.config import AppConfig, load_config, save_config
from clockout.core import PunchAction
from clockout.storage import JsonStateStore, StateStoreError


def test_old_config_window_is_migrated_to_checkout_and_saved_canonically(
    tmp_path: object,
) -> None:
    path = tmp_path / "config.json"  # type: ignore[operator]
    path.write_text(
        json.dumps(
            {
                "mode": "dry_run",
                "check_start_time": "16:10",
                "check_end_time": "22:40",
            }
        ),
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.schema_version == 2
    assert not config.auto_check_in_enabled
    assert config.auto_check_out_enabled
    assert config.check_out_window_start == "16:10"
    assert config.check_out_window_end == "22:40"
    assert config.start_time.hour == 16
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["schema_version"] == 2
    assert "check_start_time" not in saved
    assert "check_end_time" not in saved


@pytest.mark.parametrize("schema_version", [True, 0, 3, "2"])
def test_invalid_config_schema_version_is_rejected(
    tmp_path: object, schema_version: object
) -> None:
    path = tmp_path / "config.json"  # type: ignore[operator]
    path.write_text(json.dumps({"schema_version": schema_version}), encoding="utf-8")

    with pytest.raises(ValueError, match="版本"):
        load_config(path)


def test_canonical_config_round_trip_does_not_write_legacy_window_fields(
    tmp_path: object,
) -> None:
    path = tmp_path / "config.json"  # type: ignore[operator]
    config = AppConfig(
        auto_check_in_enabled=True,
        check_in_window_start="08:00",
        check_in_window_end="10:00",
        check_out_window_start="17:00",
        check_out_window_end="21:00",
    )

    save_config(path, config)

    assert load_config(path) == config
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["check_in_window_start"] == "08:00"
    assert saved["check_out_window_end"] == "21:00"
    assert "check_start_time" not in saved


def test_legacy_constructor_window_is_preserved_when_saved(tmp_path: object) -> None:
    path = tmp_path / "config.json"  # type: ignore[operator]

    save_config(
        path,
        AppConfig(check_start_time="16:20", check_end_time="22:10"),
    )

    loaded = load_config(path)
    assert loaded.check_out_window_start == "16:20"
    assert loaded.check_out_window_end == "22:10"


def test_v1_state_migrates_only_to_checkout_and_preserves_uncertain_lock(
    tmp_path: object,
) -> None:
    path = tmp_path / "state.json"  # type: ignore[operator]
    path.write_text(
        json.dumps(
            {
                "date": "2026-09-15",
                "check_in_time": "09:00",
                "eligible_time": "2026-09-15T19:05",
                "clock_out_attempted": True,
                "clock_out_success": False,
                "attempted_at": "2026-09-15T19:05:00",
                "outcome": "unknown",
                "attempt_count": 1,
                "physical_click_count": 1,
                "next_retry_at": "",
                "reset_count": 0,
                "history": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store = JsonStateStore(path)

    state = store.load(date(2026, 9, 15))

    assert state is not None
    assert state.schema_version == 2
    assert state.action(PunchAction.CHECK_IN) is None
    checkout = state.action(PunchAction.CHECK_OUT)
    assert checkout is not None
    assert checkout.attempted
    assert checkout.outcome == "unknown"
    assert not store.claim_action(
        date(2026, 9, 15),
        PunchAction.CHECK_OUT,
        check_in_time="09:00",
        eligible_time=datetime(2026, 9, 15, 19, 5),
        attempted_at=datetime(2026, 9, 15, 19, 10),
    )
    migrated = json.loads(path.read_text(encoding="utf-8"))
    assert migrated["schema_version"] == 2
    assert set(migrated["actions"]) == {"check_out"}
    assert "clock_out_attempted" not in migrated


def test_v2_missing_action_means_that_action_has_not_been_attempted(
    tmp_path: object,
) -> None:
    path = tmp_path / "state.json"  # type: ignore[operator]
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "date": "2026-09-15",
                "actions": {},
            }
        ),
        encoding="utf-8",
    )
    store = JsonStateStore(path)

    state = store.load(date(2026, 9, 15))

    assert state is not None
    assert state.action(PunchAction.CHECK_IN) is None
    assert state.action(PunchAction.CHECK_OUT) is None
    assert not state.clock_out_attempted


def test_inconsistent_v1_uncertain_outcome_is_migrated_as_attempted(
    tmp_path: object,
) -> None:
    path = tmp_path / "state.json"  # type: ignore[operator]
    path.write_text(
        json.dumps(
            {
                "date": "2026-09-15",
                "check_in_time": "09:00",
                "eligible_time": "2026-09-15T19:05",
                "clock_out_attempted": False,
                "clock_out_success": False,
                "attempted_at": "2026-09-15T19:05:00",
                "outcome": "unknown",
            }
        ),
        encoding="utf-8",
    )

    state = JsonStateStore(path).load(date(2026, 9, 15))

    assert state is not None
    assert state.clock_out_attempted
    assert state.outcome == "unknown"


def test_v1_migration_replace_failure_leaves_original_file_unchanged(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"  # type: ignore[operator]
    legacy = {
        "date": "2026-09-15",
        "check_in_time": "09:00",
        "eligible_time": "2026-09-15T19:05",
        "clock_out_attempted": True,
        "clock_out_success": False,
        "attempted_at": "2026-09-15T19:05:00",
        "outcome": "attempted",
    }
    path.write_text(json.dumps(legacy), encoding="utf-8")
    original = path.read_bytes()

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(Exception, match="损坏或无法读取"):
        JsonStateStore(path).load(date(2026, 9, 15))

    assert path.read_bytes() == original


def test_actions_claim_and_update_independently(tmp_path: object) -> None:
    store = JsonStateStore(tmp_path / "state.json")  # type: ignore[operator]
    day = date(2026, 9, 15)
    morning = datetime(2026, 9, 15, 8, 50)
    evening = datetime(2026, 9, 15, 19, 5)

    check_in_token = store.claim_action(
        day,
        PunchAction.CHECK_IN,
        check_in_time="",
        eligible_time=morning,
        attempted_at=morning,
    )
    check_out_token = store.claim_action(
        day,
        PunchAction.CHECK_OUT,
        check_in_time="08:50",
        eligible_time=evening,
        attempted_at=evening,
    )

    assert check_in_token
    assert check_out_token
    assert check_in_token != check_out_token
    store.record_action_outcome(
        day,
        PunchAction.CHECK_IN,
        check_in_token,
        "success",
        success=True,
        invocation_started=True,
    )
    state = store.load(day)
    assert state is not None
    assert state.action(PunchAction.CHECK_IN).success  # type: ignore[union-attr]
    assert not state.action(PunchAction.CHECK_OUT).success  # type: ignore[union-attr]


def test_claim_token_cannot_be_used_for_another_action_or_stale_attempt(
    tmp_path: object,
) -> None:
    store = JsonStateStore(tmp_path / "state.json")  # type: ignore[operator]
    day = date(2026, 9, 15)
    morning = datetime(2026, 9, 15, 8, 50)
    evening = datetime(2026, 9, 15, 19, 5)
    check_in_token = store.claim_action(
        day,
        PunchAction.CHECK_IN,
        check_in_time="",
        eligible_time=morning,
        attempted_at=morning,
    )
    check_out_token = store.claim_action(
        day,
        PunchAction.CHECK_OUT,
        check_in_time="08:50",
        eligible_time=evening,
        attempted_at=evening,
    )
    assert check_in_token and check_out_token

    with pytest.raises(StateStoreError, match="令牌"):
        store.record_action_outcome(
            day,
            PunchAction.CHECK_OUT,
            check_in_token,
            "success",
            success=True,
        )

    store.record_action_outcome(
        day,
        PunchAction.CHECK_IN,
        check_in_token,
        "aborted_before_click",
        success=False,
    )
    store.reset_action_for_retry(
        day,
        PunchAction.CHECK_IN,
        reset_at=morning + timedelta(minutes=1),
        reason="页面未变化",
    )
    replacement_token = store.claim_action(
        day,
        PunchAction.CHECK_IN,
        check_in_time="",
        eligible_time=morning,
        attempted_at=morning + timedelta(minutes=2),
        max_attempts=3,
    )
    assert replacement_token and replacement_token != check_in_token
    with pytest.raises(StateStoreError, match="令牌"):
        store.record_action_outcome(
            day,
            PunchAction.CHECK_IN,
            check_in_token,
            "success",
            success=True,
        )


def test_checkout_compatibility_properties_delegate_to_checkout_action(
    tmp_path: object,
) -> None:
    store = JsonStateStore(tmp_path / "state.json")  # type: ignore[operator]
    day = date(2026, 9, 15)
    moment = datetime(2026, 9, 15, 19, 5)

    assert store.claim_attempt(
        day,
        check_in_time="09:00",
        eligible_time=moment,
        attempted_at=moment,
    )
    claimed = store.load(day)
    assert claimed is not None
    checkout = claimed.action(PunchAction.CHECK_OUT)
    assert checkout is not None
    store.record_action_outcome(
        day,
        PunchAction.CHECK_OUT,
        checkout.claim_token,
        "success",
        success=True,
        invocation_started=True,
    )
    state = store.load(day)

    assert state is not None
    assert state.clock_out_attempted
    assert state.clock_out_success
    assert state.outcome == "success"
    assert state.check_in_time == "09:00"


def test_late_previous_day_claim_cannot_replace_new_day_state(tmp_path: object) -> None:
    store = JsonStateStore(tmp_path / "state.json")  # type: ignore[operator]
    current_day = date(2026, 9, 16)
    previous_day = date(2026, 9, 15)
    current_moment = datetime(2026, 9, 16, 8, 50)

    current_token = store.claim_action(
        current_day,
        PunchAction.CHECK_IN,
        check_in_time="",
        eligible_time=current_moment,
        attempted_at=current_moment,
    )
    late_token = store.claim_action(
        previous_day,
        PunchAction.CHECK_OUT,
        check_in_time="09:00",
        eligible_time=datetime(2026, 9, 15, 19, 5),
        attempted_at=datetime(2026, 9, 15, 19, 5),
    )

    assert current_token
    assert late_token is None
    state = store.load(current_day)
    assert state is not None
    assert state.action(PunchAction.CHECK_IN) is not None


@pytest.mark.parametrize(
    "schema_version",
    [True, 1.0, "1", False, 2.0],
)
def test_state_schema_version_requires_a_real_integer(
    tmp_path: object, schema_version: object
) -> None:
    path = tmp_path / "state.json"  # type: ignore[operator]
    path.write_text(
        json.dumps({"schema_version": schema_version, "date": "2026-09-15", "actions": {}}),
        encoding="utf-8",
    )

    with pytest.raises(Exception, match="损坏或无法读取"):
        JsonStateStore(path).load(date(2026, 9, 15))


@pytest.mark.parametrize(
    "changes",
    [
        {"success": True, "outcome": "success"},
        {"physical_click_count": 1},
        {"outcome": "unknown"},
    ],
)
def test_inconsistent_v2_action_state_fails_closed(
    tmp_path: object, changes: dict[str, object]
) -> None:
    path = tmp_path / "state.json"  # type: ignore[operator]
    action = {
        "check_in_time": "09:00",
        "eligible_time": "2026-09-15T19:05",
        "attempted": False,
        "success": False,
        "attempted_at": "2026-09-15T19:05:00",
        "outcome": "manual_reset",
        "attempt_count": 1,
        "physical_click_count": 0,
        "next_retry_at": "",
        "reset_count": 0,
        "history": [],
        "claim_token": "",
    }
    action.update(changes)
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "date": "2026-09-15",
                "actions": {"check_out": action},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(Exception, match="损坏或无法读取"):
        JsonStateStore(path).load(date(2026, 9, 15))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda state: state.pop("success"),
        lambda state: state.update(outcome="invented"),
        lambda state: state.update(attempt_count=0),
        lambda state: state.update(attempt_count=1, physical_click_count=2),
        lambda state: state.update(extra_field=True),
    ],
)
def test_v2_action_requires_complete_canonical_state(
    tmp_path: object, mutation
) -> None:
    path = tmp_path / "state.json"  # type: ignore[operator]
    action = {
        "check_in_time": "09:00",
        "eligible_time": "2026-09-15T19:05",
        "attempted": True,
        "success": False,
        "attempted_at": "2026-09-15T19:05:00",
        "outcome": "attempted",
        "attempt_count": 1,
        "physical_click_count": 0,
        "next_retry_at": "",
        "reset_count": 0,
        "history": [],
        "claim_token": "token",
    }
    mutation(action)
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "date": "2026-09-15",
                "actions": {"check_out": action},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(Exception, match="损坏或无法读取"):
        JsonStateStore(path).load(date(2026, 9, 15))


def test_in_progress_attempt_cannot_be_reset(tmp_path: object) -> None:
    store = JsonStateStore(tmp_path / "state.json")  # type: ignore[operator]
    day = date(2026, 9, 15)
    moment = datetime(2026, 9, 15, 8, 50)
    token = store.claim_action(
        day,
        PunchAction.CHECK_IN,
        check_in_time="",
        eligible_time=moment,
        attempted_at=moment,
    )
    assert token

    with pytest.raises(StateStoreError, match="进行"):
        store.reset_action_for_retry(
            day,
            PunchAction.CHECK_IN,
            reset_at=moment + timedelta(minutes=1),
            reason="不应重置进行中的操作",
        )


def test_manual_reset_cannot_bypass_retry_limit(tmp_path: object) -> None:
    store = JsonStateStore(tmp_path / "state.json")  # type: ignore[operator]
    day = date(2026, 9, 15)
    moment = datetime(2026, 9, 15, 8, 50)
    token = store.claim_action(
        day,
        PunchAction.CHECK_IN,
        check_in_time="",
        eligible_time=moment,
        attempted_at=moment,
    )
    assert token
    store.record_action_outcome(
        day,
        PunchAction.CHECK_IN,
        token,
        "aborted_before_click",
        success=False,
    )
    store.reset_action_for_retry(
        day,
        PunchAction.CHECK_IN,
        reset_at=moment + timedelta(minutes=1),
        reason="页面检查失败",
    )

    assert store.claim_action(
        day,
        PunchAction.CHECK_IN,
        check_in_time="",
        eligible_time=moment,
        attempted_at=moment + timedelta(minutes=2),
        max_attempts=1,
    ) is None


def test_claim_token_cannot_rewrite_a_terminal_success(tmp_path: object) -> None:
    store = JsonStateStore(tmp_path / "state.json")  # type: ignore[operator]
    day = date(2026, 9, 15)
    moment = datetime(2026, 9, 15, 8, 50)
    token = store.claim_action(
        day,
        PunchAction.CHECK_IN,
        check_in_time="",
        eligible_time=moment,
        attempted_at=moment,
    )
    assert token
    store.record_action_outcome(
        day,
        PunchAction.CHECK_IN,
        token,
        "success",
        success=True,
        invocation_started=True,
    )

    with pytest.raises(StateStoreError, match="迁移"):
        store.record_action_outcome(
            day,
            PunchAction.CHECK_IN,
            token,
            "aborted_before_click",
            success=False,
        )
    state = store.load(day)
    assert state is not None
    action = state.action(PunchAction.CHECK_IN)
    assert action is not None and action.success
