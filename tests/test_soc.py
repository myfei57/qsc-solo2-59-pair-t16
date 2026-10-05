"""SOC 库仑累计、校准代际、分段对账、来源裁决与单一发布值。"""

from __future__ import annotations

import threading

import pytest

from batterystore.soc.coulomb import CoulombCounter
from batterystore.soc.estimate import estimate_soc
from batterystore.soc.segments import SegmentLedger
from batterystore.soc.sources import CALIBRATION_SOURCE, COULOMB_SOURCE, SourceLedger


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
    assert counter.reset()["net_ah"] == pytest.approx(-50.0)
    assert counter.net_ah() == 0.0
    with pytest.raises(ValueError):
        CoulombCounter(0.0)
    with pytest.raises(ValueError):
        counter.through(1.0, 0.0)


def test_estimate_uses_latest_calibration_epoch(platform):
    limits = platform.config.soc
    stale = {"epoch": 1, "soc_pct": 45.0, "expired": True}
    estimate = estimate_soc(stale, 0.0, limits, anchor_pct=45.0)
    assert estimate.soc_pct == 45.0
    assert estimate.calibration_epoch == 1
    assert estimate.stale is True
    assert estimate.as_dict()["drift_pct"] == 0.0
    assert estimate.drift_exceeded is False


def test_estimate_flags_drift_beyond_tolerance(platform):
    limits = platform.config.soc
    calibration = {"epoch": 1, "soc_pct": 45.0, "expired": False}
    within = estimate_soc(calibration, 0.0, limits, anchor_pct=45.0 + limits.drift_tolerance_pct)
    beyond = estimate_soc(calibration, 0.0, limits, anchor_pct=45.0 + limits.drift_tolerance_pct + 0.01)
    assert within.drift_exceeded is False
    assert beyond.drift_exceeded is True
    # 带符号：账面虚高时漂移为负，正是过放风险方向
    risky = estimate_soc(calibration, 0.0, limits, anchor_pct=60.0)
    assert risky.drift_pct == pytest.approx(-15.0)
    assert risky.drift_exceeded is True


def test_soc_follows_new_calibration(platform):
    platform.calibrate_soc(30.0, "field")
    estimate = platform.soc.current(platform.clock.now())
    assert estimate.calibration_epoch == 2
    assert estimate.soc_pct == pytest.approx(30.0)
    assert platform.racks["rk-01"].soc_pct() == pytest.approx(30.0)
    assert platform.racks["rk-02"].soc_pct() == pytest.approx(30.0)


def test_calibration_reevaluates_immediately_not_with_old_offset(platform):
    # 校准前账面已被累计拉低：旧口径会继续沿用旧偏移，新口径必须立刻换基准
    platform.discharge(power_kw=200.0, dt=600.0)
    before = platform.soc.current(platform.clock.now()).soc_pct
    result = platform.calibrate_soc(30.0, "field")
    detail = result["steps"][1]["detail"]
    assert detail["calibration_epoch"] == 2
    assert detail["soc_pct"] == pytest.approx(30.0)
    assert detail["base_soc_pct"] == pytest.approx(30.0)
    assert detail["delta_pct"] == pytest.approx(0.0)
    # 校准修正量 = 新基准 - 校准前推算，记录在案
    assert detail["correction_pct"] == pytest.approx(30.0 - before)
    assert platform.soc.current(platform.clock.now()).soc_pct == pytest.approx(30.0)


def test_calibration_correction_beyond_tolerance_raises_alarm(platform):
    platform.discharge(power_kw=50.0, dt=1800.0)
    before = platform.soc.current(platform.clock.now()).soc_pct
    platform.calibrate_soc(before - platform.config.soc.drift_tolerance_pct - 5.0, "field")
    alarms = platform.audit.alarms()
    correction = [entry for entry in alarms if entry["action"] == "correction_exceeded"]
    assert correction
    payload = correction[-1]["payload"]
    assert payload["calibration_epoch"] == 2
    assert abs(payload["deviation_pct"]) > platform.config.soc.drift_tolerance_pct


def test_soc_accumulates_after_calibration(platform):
    platform.calibrate_soc(50.0, "field")
    platform.soc.accumulate(140.0, 3600.0, platform.clock.now())
    estimate = platform.soc.current(platform.clock.now())
    assert estimate.delta_pct > 0
    assert estimate.soc_pct > 50.0
    assert estimate.calibration_epoch == 2
    assert platform.soc.drift_pct() >= 0.0


def test_segment_ledger_seals_and_reopens_per_calibration(platform):
    limits = platform.config.soc
    platform.soc.accumulate(140.0, 3600.0, platform.clock.now())
    platform.calibrate_soc(60.0, "field")
    ledger = platform.soc.segments
    sealed = ledger.segments()
    assert len(sealed) == 1
    assert sealed[0]["epoch"] == 1
    assert sealed[0]["base_soc_pct"] == pytest.approx(limits.initial_soc_pct)
    # 口径一：新校准后当前开段从零点重新起算
    assert ledger.net_ah() == pytest.approx(0.0)
    reconciliation = platform.soc.reconcile_segments()
    assert [row["epoch"] for row in reconciliation] == [1, 2]
    assert reconciliation[0]["end_soc_pct"] == pytest.approx(60.0)
    assert reconciliation[1]["base_soc_pct"] == pytest.approx(60.0)


def test_segment_ledger_reconciliation_closes_chain(platform):
    platform.soc.accumulate(140.0, 3600.0, platform.clock.now())
    platform.calibrate_soc(60.0, "field")
    platform.soc.accumulate(-140.0, 3600.0, platform.clock.now())
    rows = platform.soc.reconcile_segments()
    first, second = rows
    # 段内闭合：起点 + 段内增量 + 校准修正量 = 下段起点
    assert first["end_soc_pct"] == pytest.approx(second["base_soc_pct"])
    assert second["delta_pct"] < 0
    assert second["end_soc_pct"] == pytest.approx(platform.soc.current(platform.clock.now()).soc_pct)


def test_source_ledger_last_writer_wins_conflicts():
    ledger = SourceLedger()
    ledger.commit(CALIBRATION_SOURCE, 100.0, {"epoch": 1})
    ledger.commit(COULOMB_SOURCE, 110.0)
    same_source = ledger.verdict(COULOMB_SOURCE, 105.0)
    # 同源乱序判负但不算跨来源冲突
    assert same_source["stale"] is True
    assert same_source["conflict"] is False
    assert ledger.conflicts() == []
    cross = ledger.verdict(CALIBRATION_SOURCE, 108.0)
    assert cross["stale"] is True
    assert cross["conflict"] is True
    rejected = ledger.record_rejection(CALIBRATION_SOURCE, 108.0, {"epoch": 2})
    assert rejected["winner"] == COULOMB_SOURCE
    assert rejected["rejected_source"] == CALIBRATION_SOURCE
    conflict = ledger.conflicts()[-1]
    assert conflict["held_source"] == COULOMB_SOURCE
    assert conflict["lag_s"] == pytest.approx(2.0)
    # 判负未提交，最后生效来源仍是累计
    assert ledger.last_source() == COULOMB_SOURCE


def test_late_coulomb_sample_is_rejected_and_recorded(platform):
    stamped = platform.clock.now()
    platform.calibrate_soc(40.0, "field")
    platform.clock.advance(60)
    held = platform.soc.current(platform.clock.now()).soc_pct
    # 迟到的累计样本：采样时刻早于校准时刻，尽管它更晚才送到
    returned = platform.soc.accumulate(-100.0, 3600.0, stamped - 10)
    assert returned == 0.0
    assert platform.soc.current(platform.clock.now()).soc_pct == pytest.approx(held)
    conflicts = platform.soc.sources.conflicts()
    assert conflicts
    assert conflicts[-1]["rejected_source"] == COULOMB_SOURCE
    assert conflicts[-1]["winner"] == CALIBRATION_SOURCE
    # 同一时刻或更晚的累计照常入账
    accepted = platform.soc.accumulate(10.0, 60.0, platform.clock.now())
    assert accepted > 0.0


def test_late_calibration_is_rejected_before_bms_epoch_advances(platform):
    platform.calibrate_soc(40.0, "field")
    platform.clock.advance(60)
    # 一次时刻更晚的累计先生效
    platform.soc.accumulate(10.0, 60.0, platform.clock.now())
    bms_epoch = platform.bms.calibration.epoch
    held = platform.soc.current(platform.clock.now()).soc_pct
    # 时刻更早的校准必须在登记进 BMS 之前被挡住：代际不前进、基准不回退
    rejection = platform.soc.check_calibration(12.0, platform.clock.now() - 5)
    assert rejection is not None
    assert rejection["rejected_source"] == CALIBRATION_SOURCE
    assert rejection["winner"] == COULOMB_SOURCE
    assert platform.bms.calibration.epoch == bms_epoch
    assert platform.soc.current(platform.clock.now()).soc_pct == pytest.approx(held)
    # 时刻不早于已生效写入的校准正常登记
    accepted = platform.calibrate_soc(55.0, "field")
    assert accepted["ok"] is True
    assert platform.bms.calibration.epoch == bms_epoch + 1
    assert platform.soc.current(platform.clock.now()).calibration_epoch == bms_epoch + 1


def test_soc_history_and_snapshot(platform):
    platform.calibrate_soc(60.0, "field")
    platform.soc.value(platform.clock.now())
    snapshot = platform.soc_snapshot()
    assert snapshot["samples"] >= 1
    assert snapshot["coulomb"]["intervals"] >= 0
    assert snapshot["calibration"]["epoch"] == 2
    assert snapshot["estimate"]["calibration_epoch"] == 2
    series_epochs = {item["epoch"] for item in platform.soc.history.series()}
    assert 2 in series_epochs
    assert platform.soc.snapshot(platform.clock.now()).calibration["soc_pct"] == pytest.approx(60.0)
    assert platform.soc.reset()["net_ah"] == pytest.approx(0.0)
    assert snapshot["published"]["calibration_epoch"] == 2
    assert "segments" in snapshot and "conflicts" in snapshot and "reconciliation" in snapshot


def test_published_soc_is_single_value_for_all_consumers(platform):
    platform.soc.accumulate(100.0, 600.0, platform.clock.now())
    page = platform.soc_snapshot()["published"]
    scheduler = platform.soc.current(platform.clock.now())
    assert page["soc_pct"] == scheduler.soc_pct
    assert page["calibration_epoch"] == scheduler.calibration_epoch
    # 纯读不再产生新估算：连续两次读取值与时间戳一致
    again = platform.soc.published()
    assert again["published_at"] == page["published_at"]
    assert again["soc_pct"] == page["soc_pct"]


def test_concurrent_consumers_read_one_soc_value(platform):
    probe = platform.concurrency_soc_probe(readers=8, rounds=64)
    assert probe["samples"] == 8 * 64
    assert probe["mismatches"] == []
    assert probe["ok"] is True
