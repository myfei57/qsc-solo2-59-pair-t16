"""限额放电编排。

顺序固定为「并网限额钳位 → 爬坡 → 电量累计 → 配额记账」。钳位必须在爬坡
之前完成：带限功率参数的放电指令如果先爬坡再钳位，并网点会在一瞬间越限。

放电安排前必须按当前唯一一份 SOC 做过度放电核对：现场校准把真实电量
（如 30%）与调度手里的旧值（如 45%）拉开、偏差超过容差时，本次放电按
违约中止并明确告警，避免调度照着假电量把电池放过放。
"""

from __future__ import annotations

from ..errors import StoreError


def discharge_rack(
    pcs,
    grid,
    quota,
    soc,
    rack,
    station: str,
    *,
    power_kw: float,
    dt: float,
    now: float,
) -> dict:
    """执行一次放电编排，返回带步骤与违约项的报告。"""

    flow: list[dict] = []
    violations: list[str] = []

    if pcs.cut.is_tripped():
        return {"station": station, "ok": False, "steps": flow, "violations": ["pcs_tripped"]}

    deviation = soc.deviation_state()
    flow.append({"component": "soc", "action": "dispatch_check", "detail": dict(deviation)})
    if deviation["exceeded"]:
        violations.append("soc_deviation_exceeded")
        return {"station": station, "ok": False, "steps": flow, "violations": violations}

    if not pcs.is_tied():
        pcs.transfer("off_grid", "discharge_request")
        pcs.transfer("grid_tie", "discharge_request")
    clamped = grid.clamp_for_ramp(abs(float(power_kw)))
    flow.append({"component": "grid", "action": "clamp_before_ramp", "detail": clamped})
    derate = pcs.apply_derate(grid=clamped["ratio"], temperature=1.0)
    point = pcs.set_point(clamped["allowed_kw"], dt, derate)
    flow.append({"component": "pcs", "action": "ramp", "detail": point})
    if point["violations"]:
        violations.extend(point["violations"])

    energy_kwh = abs(point["ramped_kw"]) * dt / 3600.0
    arc = grid.record_flow(point["ramped_kw"], dt)
    flow.append({"component": "grid", "action": "energy", "detail": arc})
    try:
        quota.record_discharge(energy_kwh, now)
    except StoreError as error:
        violations.append("quota_exceeded")
        flow.append({"component": "quota", "action": "discharge_rejected", "detail": error.as_record()})
        return {"station": station, "ok": False, "steps": flow, "violations": violations}

    delta_ah = soc.accumulate(-abs(point["ramped_kw"]) * 1000.0 / max(rack.voltage_v(), 1e-9), dt, now)
    published = soc.publish(now)
    rack.set_soc(published.soc_pct)
    # 调度本次按发布值执行，采用值同步，下一轮偏差以新基准重新累积。
    soc.report(published.soc_pct)
    flow.append(
        {
            "component": "soc",
            "action": "publish",
            "detail": {**published.as_dict(), "delta_ah": round(delta_ah, 6), "revision": soc.revision()},
        }
    )
    return {"station": station, "ok": not violations, "steps": flow, "violations": violations}
