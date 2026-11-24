"""示例特征：主刻蚀步骤气体流量偏差的均值。"""

from eifeature import Skip, gde, ops


def fetch(run_id):
    """只在这里取数：实际流量和设定流量，截取步骤 210、212。"""
    actual = gde.trace(run_id, "GasFlow_Ar", unit="sccm").steps(210, 212)
    setpoint = gde.trace(run_id, "GasFlow_Ar_SP", unit="sccm").steps(210, 212)
    return {"actual": {"t": actual.t, "v": actual.v}, "setpoint": {"t": setpoint.t, "v": setpoint.v}}


def compute(data):
    """纯计算：两路信号重采样到 1 Hz 后逐点相减取均值。"""
    actual = ops.resample(ops.dedup(data["actual"]), hz=1)
    setpoint = ops.resample(ops.dedup(data["setpoint"]), hz=1)
    n = min(len(actual), len(setpoint))
    if n < 20:
        raise Skip("LOW_COVERAGE")  # 点太少，结果不可信
    return sum(a - b for a, b in zip(actual.v[:n], setpoint.v[:n])) / n
