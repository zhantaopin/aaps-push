# -*- coding: utf-8 -*-
"""规则引擎：任务时间/星期/多条件判断 + 中文变量模板渲染 + 任务执行。

与 v03 行为兼容：条件类型、与/或/非组合、时间条件(定点/时间段)、星期限制、
repeat/once 执行类型。任务运行状态(last/executed)改存 SQLite 而非配置文件。
"""
import logging
import re
import time
from datetime import datetime

from . import push_channels

# 中文变量名 -> 英文变量名（模板渲染前替换）
VAR_MAP = {
    "当前血糖": "BG", "趋势": "TREND",
    "5分钟增量": "DELTA", "15分钟平均增量": "SHORT_DELTA", "30分钟平均增量": "LONG_DELTA",
    "总IOB": "IOB", "大剂量IOB": "BOLUS_IOB", "基础率IOB": "BASAL_IOB",
    "剩余COB": "COB", "时间": "TIME",
    "决策来源": "FINAL_SOURCE", "最终预测血糖": "FINAL_VALUE",
    "手表电量": "WATCH_BATTERY", "手表离线": "WATCH_OFFLINE",
    "手表掉电": "WATCH_DROP", "充电状态": "CHARGING",
    "基础率": "BASAL_RATE", "剩余药量": "RESERVOIR",
    "泵状态": "PUMP_STATUS", "血糖延迟": "BG_DELAY", "泵延迟": "PUMP_OFFLINE",
}

# 条件类型 -> 快照字段（快照值已经是显示单位）
COND_FIELDS = {
    "血糖绝对值": "bg",
    "delta(5min)": "delta", "增量": "delta",
    "shortAvgDelta(15min)": "short_delta", "短平均增量": "short_delta",
    "longAvgDelta(30min)": "long_delta", "长平均增量": "long_delta",
    "总IOB": "iob", "大剂量IOB": "bolus_iob", "基础率IOB": "basal_iob",
    "COB": "cob", "最终预测BG": "final_value",
    "手表电量": "watch_battery", "手表离线(分钟)": "watch_offline_min",
    "手表掉电(30分钟)": "watch_drop_30m", "充电状态": "watch_charging_flag",
    "血糖延迟(分钟)": "bg_delay_min", "泵延迟(分钟)": "pump_offline_min",
    "泵状态异常": "pump_abnormal_flag", "泵药量(单位)": "reservoir",
}

OPS = {
    ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b, "<=": lambda a, b: a <= b,
    "<": lambda a, b: a < b,
}


def render_message(template, snap):
    """渲染消息模板。变量缺失时回退原模板并告警（沿用 v03 行为）。"""
    try:
        message = template or ""
        for cn, en in VAR_MAP.items():
            message = re.sub(r"\{%s(:[^}]+)?\}" % cn, r"{%s\1}" % en, message)
        return message.format(**snap["ctx"])
    except (KeyError, IndexError, ValueError) as e:
        logging.warning("消息模板格式化错误(缺失变量 %s)，按原样发送: %s", e, template)
        return template or ""


def build_context(snap, config):
    """由数据快照构造模板上下文与条件判断取值。"""
    return {
        "ctx": snap["ctx"],
        "fields": {
            "bg": snap["ctx"]["BG"],
            "delta": snap["ctx"]["DELTA"],
            "short_delta": snap["ctx"]["SHORT_DELTA"],
            "long_delta": snap["ctx"]["LONG_DELTA"],
            "iob": snap["ctx"]["IOB"],
            "bolus_iob": snap["ctx"]["BOLUS_IOB"],
            "basal_iob": snap["ctx"]["BASAL_IOB"],
            "cob": snap["ctx"]["COB"],
            "final_value": snap["ctx"]["FINAL_VALUE"],
            "watch_battery": snap["ctx"].get("WATCH_BATTERY"),
            "watch_offline_min": snap["ctx"].get("WATCH_OFFLINE"),
            "watch_drop_30m": snap["ctx"].get("WATCH_DROP"),
            "watch_charging_flag": snap["ctx"].get("CHARGING_FLAG"),
            "bg_delay_min": snap["ctx"].get("BG_DELAY"),
            "pump_offline_min": snap["ctx"].get("PUMP_OFFLINE"),
            "pump_abnormal_flag": 0 if snap["ctx"].get("PUMP_STATUS") in ("normal", "未知", "") else 1,
            "reservoir": _to_num(snap["ctx"].get("RESERVOIR")),
        },
    }


def _to_num(val):
    """宽松转数值（'未知'等非数值按 0 处理）。"""
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def evaluate_conditions(task, snap):
    """多条件判断，返回 (是否满足, 原因说明)。"""
    conditions = task.get("conditions") or []
    if not conditions:
        return True, "无条件(总是触发)"
    fields = build_context(snap, None)["fields"]
    final_ok = True
    reasons = []
    for cond in conditions:
        cond_type = cond.get("type", "血糖绝对值")
        op = cond.get("op", ">")
        try:
            threshold = float(cond.get("value", 0))
        except (TypeError, ValueError):
            return False, "条件阈值非法: %r" % cond.get("value")
        current_raw = fields.get(COND_FIELDS.get(cond_type, ""), 0.0)
        try:
            current = float(current_raw)
        except (TypeError, ValueError):
            current = 0.0
        ok = OPS.get(op, lambda a, b: False)(current, threshold)
        reasons.append("%s %.2f %s %.2f => %s" % (cond_type, current, op, threshold, ok))
        logic = cond.get("logic", "与")
        if cond is conditions[0]:
            final_ok = ok
        elif logic == "与":
            final_ok = final_ok and ok
        elif logic == "或":
            final_ok = final_ok or ok
        elif logic == "非":
            final_ok = final_ok and not ok
    return final_ok, "; ".join(reasons)


def evaluate_time(task, now):
    """时间与星期判断，返回 (是否满足, 原因)。定点触发会更新 last_trigger_date。"""
    time_type = task.get("time_type", "none")
    time_val = str(task.get("time_value", "")).replace(" ", "")
    current_time = now.strftime("%H:%M")
    current_date = now.strftime("%Y-%m-%d")

    time_ok = True
    if time_type == "point":
        st = task.get("_st", {"last_trigger_date": ""})
        if current_time == time_val and current_date != st.get("last_trigger_date", ""):
            task["_st"]["last_trigger_date"] = current_date  # 标记今日已触发
            time_ok = True
        else:
            time_ok = False
    elif time_type == "range":
        if "-" in time_val:
            try:
                s, e = [x.strip() for x in time_val.split("-", 1)]
                time_ok = s <= current_time <= e
            except ValueError:
                time_ok = False
        else:
            time_ok = False

    weekdays = task.get("weekdays") or [False] * 7
    weekday_ok = True
    if any(weekdays):
        weekday_ok = weekdays[now.weekday()]

    return time_ok and weekday_ok, "时间=%s(%s) 星期OK=%s" % (
        time_type, time_val or "不限制", weekday_ok)


def check_and_execute(task, snap, config, db, now):
    """单个任务判定与执行。返回 (是否触发, 说明)。"""
    task_id = task.get("id")
    state = db.get_task_state(task_id)
    task["_st"] = state

    if not task.get("enabled", True):
        return False, "任务已禁用"
    if task.get("execution_type", "repeat") == "once" and state["executed"]:
        return False, "单次任务已执行过"
    interval = max(int(task.get("repeat_interval", 180)), 10)
    if snap["now_ts"] - state["last"] < interval:
        return False, "未到重复间隔"

    time_ok, time_reason = evaluate_time(task, now)
    if not time_ok:
        return False, "时间不满足: %s" % time_reason
    cond_ok, cond_reason = evaluate_conditions(task, snap)
    if not cond_ok:
        return False, "条件不满足: %s" % cond_reason

    # 触发：更新状态并推送
    db.set_task_state(task_id, last=snap["now_ts"],
                      last_trigger_date=state.get("last_trigger_date", ""),
                      executed=(task.get("execution_type", "repeat") == "once"))
    message = render_message(task.get("msg", ""), snap)
    results = push_channels.dispatch(task, message, config, db, task_name=task.get("name", "未命名任务"))
    failed = [r for r in results if not r["ok"]]
    db.add_event("INFO" if not failed else "WARN", task.get("name", "未命名任务"),
                 ",".join(sorted({r["channel"] for r in results}) or "-"),
                 "触发: %s | %s" % (cond_reason, "全部成功" if not failed
                                    else "失败: " + "; ".join(r["detail"] for r in failed)))
    logging.info("任务[%s]触发并推送完成", task.get("name"))
    return True, "触发推送: %s" % cond_reason


def build_snapshot_ctx(state, unit, source_name=None):
    """把调度器采集状态转成显示单位快照（供条件判断与模板渲染）。

    source_name 指定时，血糖/增量/趋势/IOB/COB/最终预测/血糖延迟取该源独立数值
    （任务绑定的默认数据源）；无该源数据时血糖延迟按9999（触发断线报警）。
    """
    from .deltas import convert_delta_mgdl
    unit = unit or "mmol/L"
    view = state
    if source_name and (state.get("sources") or {}).get(source_name):
        view = state["sources"][source_name]
    final_value_raw = view.get("final_value", 0.0)  # mg/dL 口径
    view_bg = view.get("bg", 0.0)
    view_ts = view.get("last_entry_ts", 0)
    ctx = {
        "BG": round(view_bg / 18, 1) if (unit != "mg/dL" and view_bg) else round(view_bg, 1),
        "TREND": view.get("trend", "➡️ 平稳"),
        "DELTA": convert_delta_mgdl(view.get("delta", 0.0), unit),
        "SHORT_DELTA": convert_delta_mgdl(view.get("short_delta", 0.0), unit),
        "LONG_DELTA": convert_delta_mgdl(view.get("long_delta", 0.0), unit),
        "IOB": "%.2f" % view.get("iob", 0.0),
        "BOLUS_IOB": "%.2f" % view.get("bolus_iob", 0.0),
        "BASAL_IOB": "%.2f" % view.get("basal_iob", 0.0),
        "COB": "%.0f" % view.get("cob", 0.0),
        "TIME": state["time_str"],
        "FINAL_SOURCE": view.get("final_source", "无"),
        "FINAL_VALUE": round(final_value_raw / 18, 1) if unit != "mg/dL" else round(final_value_raw, 1),
        "DEV": view.get("params", {}).get("Dev", 0.0),
        "BGI": view.get("params", {}).get("BGI", 0.0),
        "ISF": view.get("params", {}).get("ISF", 0.0),
        "CR": view.get("params", {}).get("CR", 0.0),
        "TARGET": view.get("params", {}).get("Target", 0.0),
        "MINPREDBG": view.get("params", {}).get("minPredBG", 0.0),
        "MINGUARDBG": view.get("params", {}).get("minGuardBG", 0.0),
        "EVENTUALBG": view.get("params", {}).get("EventualBG", 0.0),
        "INSULINREQ": view.get("params", {}).get("insulinReq", 0.0),
        # 手表电量与离线分钟数（无数据时离线按9999处理，避免误判为"未离线"）
        "WATCH_BATTERY": (int(round(state["watch_battery"]))
                          if state.get("watch_battery") is not None else 0),
        "WATCH_OFFLINE": (round((time.time() - state["watch_ts"] / 1000) / 60)
                          if state.get("watch_ts") else 9999),
        # 30分钟掉电幅度（正值=掉了多少%，历史不足时按0不误报）
        "WATCH_DROP": (round(state["watch_drop_30m"], 1)
                       if state.get("watch_drop_30m") is not None else 0),
        # 充电状态：判定用数值标志 + 模板显示文本
        "CHARGING_FLAG": (1 if state.get("watch_charging") else 0),
        "CHARGING": ("充电中" if state.get("watch_charging")
                     else "未知" if state.get("watch_charging") is None else "未充电"),
        # 泵数据（无数据时显示"未知"，延迟按9999避免误判为"未断线"）
        "BASAL_RATE": ("%.2f" % state["basal_rate"]
                       if state.get("basal_rate") is not None else "未知"),
        "RESERVOIR": ("%.0f" % state["reservoir"]
                      if state.get("reservoir") is not None else "未知"),
        "PUMP_STATUS": state.get("pump_status") or "未知",
        "PUMP_OFFLINE": (round((time.time() - state["pump_ts"] / 1000) / 60)
                         if state.get("pump_ts") else 9999),
        # 血糖延迟（分钟）：按任务绑定源计算；无数据按9999
        "BG_DELAY": (round((time.time() - view_ts / 1000) / 60) if view_ts else 9999),
    }
    return ctx
