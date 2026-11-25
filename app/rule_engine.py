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
    "上次大剂量": "BOLUS_AGO", "组名": "GROUP_NAME",
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
    "上次大剂量(分钟)": "bolus_ago_min",
}

OPS = {
    ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b, "<=": lambda a, b: a <= b,
    "<": lambda a, b: a < b,
}

# 定点触发容差：仅定点时刻起 N 分钟内条件成立才发（错过当天不再补发）
POINT_TOLERANCE_MIN = 2


def _hm(s):
    """'H:MM'/'HH:MM' -> 分钟数；非法返回 None。"""
    try:
        h, m = s.split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def render_message(template, snap, group_name=None):
    """渲染消息模板。变量缺失时回退原模板并告警（沿用 v03 行为）。
    group_name 非 None 时提供 {%组名%} 变量（命中分支组的名字）。"""
    try:
        message = template or ""
        for cn, en in VAR_MAP.items():
            # 兼容两种写法：{%中文变量%} 与 {中文变量}（UI 默认后者）-> {EN}
            pat = r"\{%?\s*" + re.escape(cn) + r"\s*(:[^}]+)?\s*%?\}"
            message = re.sub(pat, lambda m, en=en: "{%s%s}" % (en, m.group(1) or ""), message)
        ctx = dict(snap["ctx"])
        if group_name is not None:
            ctx["GROUP_NAME"] = group_name
        return message.format(**ctx)
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
            "bolus_ago_min": snap["ctx"].get("BOLUS_AGO"),
        },
    }


def _to_num(val):
    """宽松转数值（'未知'等非数值按 0 处理）。"""
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _time_cond(tt, tv, wd):
    """构造时间条件（AAPS 式触发器节点：none=不设/仅周几，range=时间段，point=定点）。"""
    return {"type": "时间", "op": "==", "value": 0, "logic": "与",
            "tg_type": tt or "none", "tg_value": str(tv or ""),
            "weekdays": [bool(x) for x in (wd or [])]}


def _is_time_filter(tt, tv, wd):
    """该组/任务级时间配置是否构成过滤（none 且无周几=无过滤）。"""
    return (tt or "none") != "none" or bool(str(tv or "").strip()) or any(wd or [])


def effective_task(task):
    """运行时任务视图（浅拷贝）：先用原任务完成两级时间迁移（嵌入组内时间条件），
    再清空任务级 time_* 使 evaluate_time 放行；_st 等引用保持共享。
    迁移幂等：视图再进 effective_groups 不会重复迁移。"""
    t = dict(task)
    t["condition_groups"] = effective_groups(task)
    t.pop("conditions", None)
    t["time_type"] = "none"
    t["time_value"] = ""
    t["weekdays"] = []
    return t


def _chain_logic(g, idx, task):
    """组连接词：组自带 logic 优先；旧 group_logic 迁移（'且'→非首组全'且'，其余→'或'）。"""
    if g.get("logic") in ("或", "且", "非"):
        return g["logic"]
    gl = task.get("group_logic") or "或"
    return gl if (idx > 0 and gl == "且") else "或"


def effective_groups(task):
    """分支组视图：condition_groups 优先；否则由旧 conditions 按"或"切组兼容。
    每组带 logic 连接词（或=并列/且=挂链/非=整体取反挂链），旧 group_logic 迁移。
    时间归一（AAPS 式）：组级 tg_type/tg_value/weekdays 与任务级 time_type/time_value/
    weekdays 迁移为组内首部"时间"条件（任务级在组级之后，AND 语义与旧版一致，
    双时间并存的死区如实保留）；迁移后组 tg 字段清空。返回 [{name, logic, conds, ...}]。"""
    cgs = task.get("condition_groups")
    groups = []
    if cgs:
        for k, g in enumerate(cgs):
            conds = g.get("conds") or []
            if not conds:
                continue
            name = (g.get("name") or "").strip() or "组%d" % (k + 1)
            if g.get("tg_type") in ("none", "range", "point"):
                tg_type, tg_value = g["tg_type"], str(g.get("tg_value", "") or "")
            else:  # 旧结构迁移
                tv = str(g.get("time_value", "") or "")
                tg_type, tg_value = ("range", tv) if tv else ("none", "")
            weekdays = [bool(x) for x in (g.get("weekdays") or [])]
            if _is_time_filter(tg_type, tg_value, weekdays):
                conds = [_time_cond(tg_type, tg_value, weekdays)] + list(conds)
            groups.append({
                "name": name,
                "logic": _chain_logic(g, k, task),
                "tg_type": "none", "tg_value": "",  # 已迁为时间条件
                "weekdays": [], "msg": g.get("msg") or "",
                "conds": conds,
            })
        if groups:
            return _apply_task_time(task, groups)
    conditions = task.get("conditions") or []
    if not conditions:
        return []
    chunks, cur = [], []
    for i, cond in enumerate(conditions):
        if i > 0 and cond.get("logic", "与") == "或":
            if cur:
                chunks.append(cur)
            cur = []
        cur.append(cond)
    if cur:
        chunks.append(cur)
    groups = [{"name": "组%d" % (i + 1), "logic": "或", "tg_type": "none", "tg_value": "",
               "weekdays": [], "msg": "", "conds": cs} for i, cs in enumerate(chunks)]
    return _apply_task_time(task, groups)


def _apply_task_time(task, groups):
    """任务级时间迁移：time_type/time_value/weekdays 非空时给每组追加一条任务级
    时间条件（排在组级之后），运行时视图清空任务级 time_*。"""
    tt = task.get("time_type") or "none"
    tv = str(task.get("time_value", "") or "").replace(" ", "")
    wd = [bool(x) for x in (task.get("weekdays") or [])]
    if _is_time_filter(tt, tv, wd):
        for g in groups:  # 组级时间条件在前，任务级在后（每组独立副本）
            g["conds"] = g["conds"] + [dict(_time_cond(tt, tv, wd))]
    return groups


def _eval_cond(cond, fields, now=None):
    """单条件求值，返回 (结果, 原因说明)；"非"=该项取反。
    type=='时间' 为时间条件：tg_type(none/range/point)+tg_value+weekdays 三态窗口。"""
    if cond.get("type") == "时间":
        tt = cond.get("tg_type") or "none"
        tv = str(cond.get("tg_value") or "").replace(" ", "")
        wd = cond.get("weekdays") or []
        tag = {"none": "不设" + ("(仅周几)" if any(wd) else ""),
               "range": "范围" + tv, "point": "定点" + tv}.get(tt, tt)
        if now is None:
            return True, "时间[%s]OK(无now按真)" % tag
        ok = evaluate_window(tt, tv, wd, now)
        return ok, "时间[%s]%s" % (tag, "OK" if ok else "不满足")
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
    reason = "%s %.2f %s %.2f => %s" % (cond_type, current, op, threshold, ok)
    if cond.get("logic", "与") == "非":
        ok = not ok
        reason = "非(%s)" % reason
    return ok, reason


def evaluate_conditions(task, snap, now=None):
    """顺序链条判断（从上到下）：每组 logic（或/且/非）表示与「前面累计结果」的组合——
    或=任一分支命中（并列），且=与累计结果同真才触发，非=该组条件全不满足才算命中
    （组整体取反）。旧 group_logic 已由 effective_groups 迁到各组 logic。
    返回 (是否满足, 原因说明, 有效命中组名列表[按组顺序，含非组取反命中])。"""
    fields = build_context(snap, None)["fields"]
    groups = effective_groups(task)
    if not groups:
        return True, "无分支(总是触发)", []
    toks, hit_names = [], []
    result = None
    for gi, g in enumerate(groups):
        parts = []
        cond_ok = True
        for cond in g["conds"]:
            ok, reason = _eval_cond(cond, fields, now)
            cond_ok = cond_ok and ok
            parts.append(reason)
        logic = g.get("logic") or "或"
        eff = (not cond_ok) if logic == "非" else cond_ok
        if result is None:
            result = eff
        elif logic == "或":
            result = result or eff
        else:  # 且 / 非 挂链
            result = result and eff
        if eff:
            hit_names.append(g["name"])
        tag = "" if gi == 0 else "[%s]" % logic
        toks.append("%s[%s] %s" % (tag, g["name"], " & ".join(parts)))
    head = "组间链条 " if len(groups) > 1 else ""
    return bool(result), head + " ‖ ".join(toks), hit_names


def evaluate_window(tg_type, tg_value, weekdays, now):
    """组级时间窗三态：none=全部(仅周几过滤)；range=时间段(支持 22:00-7:00 跨夜)；
    point=定点(仅定点时刻起 POINT_TOLERANCE_MIN 分钟内为真，错过当天不再补发，
    每日一次由 group_dates 状态保证)。"""
    if weekdays and any(weekdays):
        if not weekdays[now.weekday()]:
            return False
    t = tg_type or "none"
    tv = str(tg_value or "").replace(" ", "")
    if t == "none" or not tv:
        return True
    curm = now.hour * 60 + now.minute
    if t == "point":
        sm = _hm(tv)
        return sm is not None and sm <= curm <= sm + POINT_TOLERANCE_MIN
    # range
    if "-" not in tv:
        return False
    s, e = [x.strip() for x in tv.split("-", 1)]
    sm, em = _hm(s), _hm(e)
    if sm is None or em is None:
        return False
    return (sm <= curm <= em) if sm <= em else (curm >= sm or curm <= em)


def evaluate_time(task, now):
    """时间与星期判断，返回 (是否满足, 原因)。定点触发会更新 last_trigger_date。"""
    time_type = task.get("time_type", "none")
    time_val = str(task.get("time_value", "")).replace(" ", "")
    current_time = now.strftime("%H:%M")
    current_date = now.strftime("%Y-%m-%d")

    time_ok = True
    if time_type == "point":
        # 定点触发：当日到达定点时刻(含之后)的第一个采集周期即触发，
        # 避免采集周期恰好跳过该分钟导致定点任务整日漏触发
        st = task.get("_st", {"last_trigger_date": ""})
        want = _hm(time_val)
        nowm = now.hour * 60 + now.minute
        if (want is not None and nowm >= want
                and current_date != st.get("last_trigger_date", "")):
            task["_st"]["last_trigger_date"] = current_date  # 标记今日已触发
            time_ok = True
        else:
            time_ok = False
    elif time_type == "range":
        if "-" in time_val:
            s, e = [x.strip() for x in time_val.split("-", 1)]
            sm, em = _hm(s), _hm(e)
            curm = now.hour * 60 + now.minute
            if sm is None or em is None:
                time_ok = False
            elif sm <= em:
                time_ok = sm <= curm <= em
            else:
                time_ok = curm >= sm or curm <= em  # 跨夜时间段，如 22:00-7:00
        else:
            time_ok = False

    weekdays = task.get("weekdays") or [False] * 7
    weekday_ok = True
    if any(weekdays):
        weekday_ok = weekdays[now.weekday()]

    return time_ok and weekday_ok, "时间=%s(%s) 星期OK=%s" % (
        time_type, time_val or "不限制", weekday_ok)


def _group_is_point(group):
    """组内是否含"定点"时间条件（每日一次由 group_dates 保证）。"""
    return any(c.get("type") == "时间" and c.get("tg_type") == "point"
               for c in group["conds"])


def check_and_execute(task, snap, config, db, now):
    """单个任务判定与执行。返回 (是否触发, 说明)。"""
    task_id = task.get("id")
    state = db.get_task_state(task_id)
    task["_st"] = state
    task = effective_task(task)  # 运行时视图：两级时间已迁为组内时间条件
    today = now.strftime("%Y-%m-%d")

    if not task.get("enabled", True):
        return False, "任务已禁用"
    interval = max(int(task.get("repeat_interval", 180)), 10)
    if snap["now_ts"] - state["last"] < interval:
        return False, "未到重复间隔"

    # once 语义 = 每日仅触发一次（保存任务会重置状态，便于修改后立即重测）
    executed_today = state.get("last_trigger_date", "") == today

    time_ok, time_reason = evaluate_time(task, now)
    if not time_ok:
        return False, "时间不满足: %s" % time_reason
    if task.get("execution_type", "repeat") == "once" and executed_today:
        if not db.event_exists_today(task.get("name", "未命名任务"), "今日已执行"):
            db.add_event("INFO", task.get("name", "未命名任务"), "-",
                         "单次任务今日已执行过，跳过（每日仅触发一次）")
        return False, "单次任务今日已执行过"

    cond_ok, cond_reason, hit_names = evaluate_conditions(task, snap, now)
    if not cond_ok:
        return False, "条件不满足: %s" % cond_reason

    # 触发：命中组里选第一个非"定点且今日已触发"的组；消息用该组文案，{%组名%}=命中组名
    today = now.strftime("%Y-%m-%d")
    gdates = dict(state.get("group_dates") or {})
    final_group = None
    for g in effective_groups(task):
        if g["name"] in hit_names:
            if _group_is_point(g) and gdates.get(g["name"]) == today:
                continue  # 定点组每日一次：当日已触发则跳过
            final_group = g
            break
    if final_group is None:
        return False, "命中组均为定点组且今日已触发"
    if _group_is_point(final_group):
        gdates[final_group["name"]] = today
    group_name = final_group["name"]
    executed_once = task.get("execution_type", "repeat") == "once"
    db.set_task_state(task_id, last=snap["now_ts"],
                      last_trigger_date=today if executed_once
                      else state.get("last_trigger_date", ""),
                      executed=executed_once,
                      group_dates=gdates)
    message = render_message(
        (final_group.get("msg") if final_group else "") or task.get("msg", ""),
        snap, group_name=group_name)
    results = push_channels.dispatch(task, message, config, db, task_name=task.get("name", "未命名任务"))
    failed = [r for r in results if not r["ok"]]
    db.add_event("INFO" if not failed else "WARN", task.get("name", "未命名任务"),
                 ",".join(sorted({r["channel"] for r in results}) or "-"),
                 "触发[组:%s]: %s | %s" % (group_name or "-", cond_reason,
                                          "全部成功" if not failed
                                          else "失败: " + "; ".join(r["detail"] for r in failed)))
    logging.info("任务[%s]触发(组:%s)并推送完成", task.get("name"), group_name or "-")
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
        # 上次大剂量距现在分钟数：无记录按0（视为刚打过，">N分钟"类条件不触发，宁漏报不误报）
        "BOLUS_AGO": (round((time.time() - view.get("last_bolus_ts", 0) / 1000) / 60)
                      if view.get("last_bolus_ts") else 0),
    }
    return ctx
