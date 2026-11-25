# -*- coding: utf-8 -*-
"""Nightscout 客户端：entries/devicestatus 拉取、决策参数解析、心跳上报（用于双端选主）。"""
import logging
import re
from datetime import datetime

import requests

# 从 openaps.suggested.reason 文本中提取的决策参数（单位 mg/dL）
PARAM_PATTERNS = {
    "Dev": r"Dev[:\s]+([-\d.]+)", "BGI": r"BGI[:\s]+([-\d.]+)",
    "ISF": r"ISF[:\s]+([\d.]+)", "CR": r"CR[:\s]+([\d.]+)",
    "Target": r"Target[:\s]+([\d.]+)", "minPredBG": r"minPredBG[:\s]+([-\d.]+)",
    "minGuardBG": r"minGuardBG[:\s]+([-\d.]+)", "IOBpredBG": r"IOBpredBG[:\s]+([-\d.]+)",
    "COBpredBG": r"COBpredBG[:\s]+([-\d.]+)", "UAMpredBG": r"UAMpredBG[:\s]+([-\d.]+)",
    "EventualBG": r"Eventual BG[:\s]+([-\d.]+)", "insulinReq": r"insulinReq[:\s]+([\d.]+)",
    "maxBolus": r"maxBolus[:\s]+([\d.]+)", "maxSafeBasal": r"maxSafeBasal[:\s]+([\d.]+)",
}

TREND_MAP = {
    "DoubleDown": "⬇️⬇️ 快速下降", "SingleDown": "⬇️ 下降",
    "FortyFiveDown": "↘️ 缓慢下降", "Flat": "➡️ 平稳",
    "FortyFiveUp": "↗️ 缓慢上升", "SingleUp": "⬆️ 上升",
    "DoubleUp": "⬆️⬆️ 快速上升",
}


def trend_text(direction):
    return TREND_MAP.get(direction, TREND_MAP.get(str(direction).lower(), "➡️ 平稳"))


def parse_date_ms(value):
    """NS 的 date 字段可能是毫秒数字或 ISO 字符串，统一转毫秒。"""
    if isinstance(value, (int, float)):
        return int(value)
    try:
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return int(dt.timestamp() * 1000)
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


class NightscoutClient:
    def __init__(self, base_url, secret, timeout=12):
        self.base_url = (base_url or "").rstrip("/")
        self.secret = secret or ""
        self.timeout = timeout
        self.session = requests.Session()

    def _headers(self, extra=None):
        h = {"api-secret": self.secret}
        if extra:
            h.update(extra)
        return h

    def fetch_entries(self, count=30):
        """血糖记录：sgv(mg/dL)、direction、filledGap、date(ms)。"""
        try:
            r = self.session.get(
                f"{self.base_url}/api/v1/entries.json",
                params={"count": count}, headers=self._headers(), timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logging.error("获取entries数据失败: %s", e)
            return None

    def fetch_devicestatus(self, count=20):
        try:
            r = self.session.get(
                f"{self.base_url}/api/v1/devicestatus.json",
                params={"count": count}, headers=self._headers(), timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logging.error("获取devicestatus数据失败: %s", e)
            return None

    def fetch_last_bolus(self):
        """最新一次大剂量（eventType 含 Bolus 且 insulin>0）的时间戳 ms；无记录或失败返回 None。
        注意：AAPS 上传的 type 字段是 NORMAL 等归一化值，须用 eventType 判断。"""
        try:
            r = self.session.get(
                f"{self.base_url}/api/v1/treatments.json",
                params={"count": 100}, headers=self._headers(), timeout=self.timeout)
            r.raise_for_status()
            best = 0
            for t in (r.json() or []):
                try:
                    if "bolus" not in (t.get("eventType") or "").lower():
                        continue
                    if float(t.get("insulin") or 0) <= 0:
                        continue
                    ts = parse_date_ms(t.get("created_at") or t.get("date") or 0)
                    if ts > best:
                        best = ts
                except (TypeError, ValueError):
                    continue
            return best or None
        except Exception as e:
            logging.error("获取大剂量记录失败: %s", e)
            return None

    def upload_heartbeat(self, device_name, instance_id, now_ms):
        """上报心跳 devicestatus，作为跨机器（云端+手机）选主的共享标记。
        需要 NS 写权限；失败时调度器按单实例模式运行。"""
        try:
            r = self.session.post(
                f"{self.base_url}/api/v1/devicestatus.json",
                headers=self._headers({"Content-Type": "application/json"}),
                json={"device": device_name, "date": now_ms,
                      "aapsPushHeartbeat": {"instance": instance_id}},
                timeout=self.timeout)
            return r.status_code == 200
        except Exception as e:
            logging.warning("心跳上报失败: %s", e)
            return False


def parse_devicestatus(item):
    """解析一条 devicestatus：IOB 明细、COB、决策参数、时间戳。"""
    cob_val = 0.0
    openaps = item.get("openaps") or {}
    try:
        cob_val = float((openaps.get("cob") or {}).get("cob", 0) or 0)
    except (TypeError, ValueError):
        cob_val = 0.0
    reason = ""
    if openaps:
        reason = (openaps.get("suggested") or {}).get("reason", "") or ""
        if cob_val == 0:
            m = re.search(r"COB[:\s]+([\d.]+)", reason, re.IGNORECASE)
            if m:
                try:
                    cob_val = float(m.group(1))
                except ValueError:
                    pass

    params = {}
    for key, pattern in PARAM_PATTERNS.items():
        m = re.search(pattern, reason, re.IGNORECASE)
        if m:
            try:
                params[key] = float(m.group(1))
            except ValueError:
                params[key] = 0.0
        else:
            params[key] = 0.0

    iob_data = openaps.get("iob") or {}
    try:
        iob = round(float(iob_data.get("iob", 0) or 0), 2)
        basaliob = round(float(iob_data.get("basaliob", 0) or 0), 2)
    except (TypeError, ValueError):
        iob, basaliob = 0.0, 0.0

    return {
        "ts": parse_date_ms(item.get("date", 0)),
        "iob": iob,
        "basaliob": basaliob,
        "bolusiob": round(iob - basaliob, 2),
        "cob": cob_val,
        "params": params,
        "reason": reason,
        "device": item.get("device", "") or "",
        # 手表/手机电量（AAPS 上传的 uploaderBattery）
        "uploader_battery": _safe_float(item.get("uploaderBattery")),
        "is_charging": (None if item.get("isCharging") is None
                        else bool(item.get("isCharging"))),
        # 泵数据（pump.extended.BaseBasalRate=基础率, reservoir=药量U, status=泵状态）
        "basal_rate": _safe_float(((item.get("pump") or {}).get("extended") or {}).get("BaseBasalRate")),
        "reservoir": _safe_float((item.get("pump") or {}).get("reservoir")),
        "pump_status": (((item.get("pump") or {}).get("status") or {}).get("status")) or "",
        "pump_ts": parse_date_ms(((item.get("pump") or {}).get("status") or {}).get("timestamp")
                                 or item.get("date", 0)),
    }


def _safe_float(val):
    """宽松转 float，失败返回 None。"""
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
