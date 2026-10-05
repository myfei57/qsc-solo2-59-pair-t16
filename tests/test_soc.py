"""SOC 库仑累计、校准代际、对账、偏差越限与来源冲突。"""

from __future__ import annotations

import pytest

from batterystore.soc.coulomb import CoulombCounter
from batterystore.soc.estimate import estimate_soc


def test_coulomb_counter_tracks_both_directions():
    counter = CoulombCounter(0.5)
    charged = counter.through(100.0, 3600.0)
    discharged = counter.through(-100.0, 3600.0)
    assert charged == pytest.approx(50.0)
    assert discharged == pytest.approx(-100.0)
    assert counter.charged_ah() == pytest.approx(50.0)
    assert counter.discharged_ah() == pytest.approx(100.0)
    assert counter.net_ah() == pytest.approx(-50.0)
    assert counter.intervals() == 2
    assert counter.efficiency == 0.5
    # 连续累计口径：累计器没有清零入口，跨校准段的吞吐量必须一直保留。
    assert not hasattr(counter, "reset")
    with pytest.raises(ValueError):
        CoulombCounter(0.0)
    with pytest.raises(ValueError):
        counter.through(1.0, 0.0)


def test_estimate_uses_latest_calibration_epoch(platform):
    limits = platform.config.soc
    stale = {"epoch": 1, "soc_pct": 45.0, "expired": True}
    estimate = estimate_soc(stale, 0.0, limits, reported_pct=45.0)
    assert estimate.soc_pct == 45.0
    assert estimate.calibration_epoch == 1
    assert estimate.stale is True
    assert estimate.as_dict()["drift_pct"] == 0.0
    assert estimate.deviation_exceeded is False


def test_estimate_flags_deviation_beyond_tolerance(platform):
    limits = platform.config.soc
    estimate = estimate_soc({"epoch": 2, "soc_pct": 30.0}, 0.0, limits, reported_pct=45.0)
    assert estimate.soc_pct == pytest.approx(30.0)
    assert estimate.drift_pct == pytest.approx(-15.0)
    assert estimate.deviation_exceeded is True
    within = estimate_soc({"epoch": 2, "soc_pct": 47.0}, 0.0, limits, reported_pct=45.0)
    assert within.deviation_exceeded is False


def test_soc_follows_new_calibration(platform):
    platform.calibrate_soc(30.0, "field")
    estimate = platform.soc.value(platform.clock.now())
    assert estimate.calibration_epoch == 2
    assert estimate.soc_pct == pytest.approx(30.0)
    # 唯一对外值同步到每一簇，页面/调度拿到的是同一修订号。
    assert platform.racks["rk-01"].soc_pct() == pytest.approx(30.0)
    assert platform.racks["rk-02"].soc_pct() == pytest.approx(30.0)
    assert platform.soc.current()["revision"] == platform.soc_snapshot()["current"]["revision"]


def test_soc_accumulates_after_calibration(platform):
    platform.calibrate_soc(50.0, "field")
    platform.soc.accumulate(140.0, 3600.0, platform.clock.now())
    estimate = platform.soc.value(platform.clock.now())
    assert estimate.delta_pct > 0
    assert estimate.soc_pct > 50.0
    assert platform.soc.drift_pct() >= 0.0


def test_calibration_keeps_counter_and_records_segment(platform):
    platform.calibrate_soc(50.0, "field")
    net_before = platform.soc.counter.net_ah()
    platform.soc.accumulate(140.0, 3600.0, platform.clock.now())
    charged_before = platform.soc.counter.charged_ah()
    assert charged_before > 0.0
    platform.calibrate_soc(40.0, "field")
    # 校准不清零：校准前后累计器读数一致。
    assert platform.soc.counter.charged_ah() == pytest.approx(charged_before)
    recon = platform.soc.reconciliation()
    assert recon["policy"] == "continuous_counter_with_correction"
    closed = [seg for seg in recon["segments"] if seg["next_epoch"] == 3]
    assert len(closed) == 1
    segment = closed[0]
    assert segment["segment_net_ah"] == pytest.approx(charged_before)
    assert segment["exceeded_tolerance"] is True
    # 校准后新段从新基准起算，旧累计仍留在连续账上。
    assert platform.soc.reconciliation()["lifetime_net_ah"] == pytest.approx(charged_before)


def test_deviation_beyond_tolerance_alarm_and_discharge_block(platform):
    # 调度采用 45%，现场校准真实只剩 30%。
    platform.calibrate_soc(45.0, "field")
    platform.soc.report(45.0)
    result = platform.calibrate_soc(30.0, "field")
    assert result["violations"] == ["soc_deviation_exceeded"]
    state = platform.soc.deviation_state()
    assert state["exceeded"] is True
    assert state["soc_pct"] == pytest.approx(30.0)
    assert state["reported_pct"] == pytest.approx(45.0)
    actions = [event.action for event in platform.bus.history(topic="soc")]
    assert "soc_deviation_exceeded" in actions
    # 调度照假电量放电必须被拦下，且不产生新的库仑累计。
    net = platform.soc.counter.net_ah()
    blocked = platform.discharge(power_kw=200.0, dt=60.0)
    assert blocked["ok"] is False
    assert "soc_deviation_exceeded" in blocked["violations"]
    assert platform.soc.counter.net_ah() == pytest.approx(net)
    # 调度确认新值后放行。
    platform.soc.report(30.0)
    assert platform.discharge(power_kw=200.0, dt=60.0)["ok"] is True


def test_consumers_read_one_value(platform):
    platform.calibrate_soc(42.0, "field")
    first = platform.soc.current()
    page = platform.soc_snapshot()["current"]
    rack = platform.racks["rk-01"].soc_pct()
    second = platform.soc.current()
    assert first == second == page
    assert rack == pytest.approx(first["soc_pct"])
    # 没有新的发布动作，修订号不变。
    assert platform.soc.revision() == first["revision"]


def test_late_accumulation_after_calibration_is_rejected(platform):
    now = platform.clock.now()
    platform.calibrate_soc(50.0, "field")
    net = platform.soc.counter.net_ah()
    # 校准之后才到达一段"校准之前"的累计（乱序晚到）。
    delta = platform.soc.accumulate(100.0, 60.0, now - 1.0)
    assert delta == 0.0
    assert platform.soc.counter.net_ah() == pytest.approx(net)
    conflicts = platform.soc.drain_conflicts()
    assert conflicts and conflicts[0]["kind"] == "accumulation"
    assert conflicts[0]["winner"] == "calibration"


def test_soc_history_and_snapshot(platform):
    platform.calibrate_soc(60.0, "field")
    platform.soc.value(platform.clock.now())
    snapshot = platform.soc_snapshot()
    assert snapshot["samples"] >= 1
    assert snapshot["coulomb"]["intervals"] >= 0
    assert snapshot["calibration"]["epoch"] == 2
    assert snapshot["current"]["calibration_epoch"] == 2
    assert platform.soc.history.series()[-1]["epoch"] == 2
    assert "reconciliation" in snapshot
    assert "conflicts" in snapshot
    # 快照读取不制造新采样、不改修订号。
    revision = platform.soc.revision()
    platform.soc.snapshot(platform.clock.now())
    assert platform.soc.revision() == revision
