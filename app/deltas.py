# -*- coding: utf-8 -*-
"""官方 AAPS 增量算法（mg/dL 口径），对照 AndroidAPS DeltaCalculator 源码实现。

官方口径（DeltaCalculator.kt）：
  对每个周期 P（delta=5 / shortAvg=15 / longAvg=30 分钟）：
  - 取时间距今在 (2.5, P+2.5] 分钟内的历史读数
  - 每个读数与最新读数比较：normalized = (最新值 - 该读数) / (时间差分钟 / 5)
    即统一折算为"每5分钟变化速率"
  - 对窗口内所有 normalized 取算术平均
  - 空窗口回退：shortAvg 无数据 → 用 delta；longAvg 无数据 → 用 shortAvg
只使用有效(sgv>39)且非填充(filled=False)的数据点。
"""
HISTORY_LENGTH = 30

# 单位换算：mg/dL -> mmol/L
MGDL_TO_MMOL = 18.0


def convert_mgdl(value_mgdl, unit):
    """内部统一 mg/dL，输出时按配置单位换算。"""
    if unit == "mg/dL":
        return round(float(value_mgdl), 1)
    return round(float(value_mgdl) / MGDL_TO_MMOL, 2)


def convert_delta_mgdl(value_mgdl, unit):
    if unit == "mg/dL":
        return round(float(value_mgdl), 1)
    return round(float(value_mgdl) / MGDL_TO_MMOL, 2)


class DeltaCalculator:
    def __init__(self):
        self.history = []  # [(ts_ms, sgv, recalc, filled)] 最新在前
        self.delta = 0.0          # mg/dL
        self.short_avg_delta = 0.0
        self.long_avg_delta = 0.0

    def update(self, sgv, ts_ms, recalc=None, filled=False):
        recalc = float(sgv if recalc is None else recalc)
        sgv = float(sgv)
        if self.history:
            last_ts, last_val, _, _ = self.history[0]
            if last_ts == ts_ms and last_val == sgv:
                return  # 重复数据不处理

        self.history.insert(0, (ts_ms, sgv, recalc, filled))
        if len(self.history) > HISTORY_LENGTH:
            self.history.pop()

        if len(self.history) < 2:
            self.delta = self.short_avg_delta = self.long_avg_delta = 0.0
            return

        def rnd(v):
            return round(v * 10) / 10  # 官方口径：保留1位小数

        def window_avg(period_min):
            """官方算法：窗口内每个读数 vs 最新读数，按5分钟速率归一化后平均。"""
            ref_ts, ref_val = self.history[0][0], self.history[0][2]
            vals = []
            for ts, sgv, recalc, filled in self.history[1:]:
                if sgv <= 39 or filled:
                    continue
                minutes = (ref_ts - ts) / 60000.0
                if 2.5 < minutes <= period_min + 2.5:
                    vals.append((ref_val - recalc) / (minutes / 5.0))
            return sum(vals) / len(vals) if vals else None

        d = window_avg(5)
        s = window_avg(15)
        l = window_avg(30)
        # 官方回退链：long 缺 → short；short 缺 → delta
        if d is None:
            d = 0.0
        if s is None:
            s = d
        if l is None:
            l = s
        self.delta = rnd(d)
        self.short_avg_delta = rnd(s)
        self.long_avg_delta = rnd(l)
