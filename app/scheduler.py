# -*- coding: utf-8 -*-
"""调度器：轮询采集 → 心跳选主（双端去重）→ 规则引擎。独立线程运行。"""
import logging
import threading
import time
import uuid
from collections import deque
from datetime import datetime

from .deltas import DeltaCalculator
from .ns_client import NightscoutClient, parse_devicestatus, trend_text
from . import rule_engine


class Scheduler:
    def __init__(self, config_store, db):
        self.config_store = config_store
        self.db = db
        self.stop_event = threading.Event()

        # 实例标识（持久化，重启不变）
        self.instance_id = db.get_kv("instance_id") or uuid.uuid4().hex[:10]
        db.set_kv("instance_id", self.instance_id)
        self.device_name = "aaps-push:%s" % self.instance_id

        self.deltas = DeltaCalculator()
        self._lock = threading.RLock()
        self._thread = None
        # 手表电量历史：(时间戳ms, 电量%)，约6小时窗口，用于30分钟掉电检测
        self._watch_hist = deque(maxlen=360)
        # 每个数据源独立状态（增量/IOB/预测），供任务按源绑定
        self._sources_state = {}

        # 采集状态（内部统一 mg/dL 口径）
        self._state = {
            "bg": 0.0,            # mg/dL
            "trend": "➡️ 平稳",
            "delta": 0.0, "short_delta": 0.0, "long_delta": 0.0,  # mg/dL
            "iob": 0.0, "bolus_iob": 0.0, "basal_iob": 0.0,
            "cob": 0.0,
            "final_source": "无", "final_value": 0.0,  # mg/dL
            "params": {},
            "last_entry_ts": 0,
            "time_str": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            # 手表电量（来自 AAPS 上传的 devicestatus.uploaderBattery）
            "watch_battery": None,
            "watch_charging": None,
            "watch_device": "",
            "watch_ts": 0,
            "watch_drop_30m": None,  # 30分钟内掉了多少%（正值=掉了）
            # 泵数据（来自 devicestatus.pump）
            "basal_rate": None,     # 当前基础率 U/h
            "reservoir": None,      # 药量 U
            "pump_status": "",      # normal/suspended/...
            "pump_ts": 0,
            # 多数据源
            "bg_source": "",        # 最新血糖来源源名
            "source_delays": {},    # {源名: 延迟分钟}
            "sources": {},          # {源名: 该源独立采集状态}
        }
        # 运行状态
        self._run_state = {
            "is_leader": True,
            "leader_reason": "启动中",
            "last_cycle_ok": False,
            "last_cycle_time": "",
            "cycles": 0,
            "errors": 0,
        }

    # ---------- 生命周期 ----------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self.stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="scheduler")
        self._thread.start()

    def stop(self):
        self.stop_event.set()

    def _run_loop(self):
        logging.info("调度器启动，实例ID: %s", self.instance_id)
        while not self.stop_event.is_set():
            cfg = self.config_store.get()
            interval = max(int(cfg.get("interval", 60)), 15)
            try:
                self.cycle(cfg)
            except Exception as e:
                self._run_state["errors"] += 1
                logging.exception("采集周期异常: %s", e)
            self.stop_event.wait(interval)

    # ---------- 单个采集周期 ----------
    def _ns_sources(self, cfg):
        """数据源列表：ns_sources 优先，兼容旧单 url/secret。"""
        srcs = [s for s in (cfg.get("ns_sources") or []) if s.get("url")]
        if not srcs and cfg.get("url"):
            srcs = [{"name": "默认", "url": cfg.get("url"),
                     "secret": cfg.get("secret"), "group": ""}]
        return srcs

    def cycle(self, cfg):
        now = datetime.now()
        srcs = self._ns_sources(cfg)
        hb_client = None
        all_statuses = []     # 合并后的 devicestatus
        first_client = None

        # 1. 逐源独立采集与计算
        for s in srcs:
            client = NightscoutClient(s.get("url"), s.get("secret"))
            if first_client is None:
                first_client = client
            sname = s.get("name") or s.get("url", "")
            with self._lock:
                sst = self._sources_state.setdefault(sname, {
                    "deltas": DeltaCalculator(),
                    "bg": 0.0, "delta": 0.0, "short_delta": 0.0, "long_delta": 0.0,
                    "trend": "➡️ 平稳", "last_entry_ts": 0,
                    "iob": 0.0, "bolus_iob": 0.0, "basal_iob": 0.0, "cob": 0.0,
                    "params": {}, "final_source": "无", "final_value": 0.0,
                    "last_bolus_ts": 0,
                })

            entries = client.fetch_entries(count=30)
            if entries:
                entries.sort(key=lambda x: int(x.get("date", 0) or 0), reverse=True)
                latest = entries[0]
                ts = int(latest.get("date", 0) or 0)
                with self._lock:
                    if ts > sst["last_entry_ts"]:
                        sst["last_entry_ts"] = ts
                        sgv = float(latest.get("sgv", 0) or 0)
                        sst["bg"] = sgv
                        sst["deltas"].update(sgv, ts, sgv, latest.get("filledGap", False))
                        sst["delta"] = sst["deltas"].delta
                        sst["short_delta"] = sst["deltas"].short_avg_delta
                        sst["long_delta"] = sst["deltas"].long_avg_delta
                        sst["trend"] = trend_text(latest.get("direction", "Flat"))

            statuses = client.fetch_devicestatus(count=60)
            if statuses:
                all_statuses.extend(statuses)
                cutoff = sst["last_entry_ts"] - 5 * 60 * 1000
                for item in reversed(statuses):
                    data = parse_devicestatus(item)
                    if data["ts"] > cutoff:
                        with self._lock:
                            sst["iob"] = data["iob"]
                            sst["basal_iob"] = data["basaliob"]
                            sst["bolus_iob"] = data["bolusiob"]
                            sst["cob"] = data["cob"]
                            sst["params"] = data["params"]
                            self._calc_final_decision(data["params"], sst)
                        break

            # 最近一次大剂量时间（treatments API，供"上次大剂量(分钟)"条件用；失败保留旧值）
            bolus_ts = client.fetch_last_bolus()
            if bolus_ts:
                with self._lock:
                    if bolus_ts > sst.get("last_bolus_ts", 0):
                        sst["last_bolus_ts"] = bolus_ts

        # 2. 全局合并视图：取所有源中最新的一条（用于状态卡与兼容）
        with self._lock:
            newest_name = None
            for name, sst in self._sources_state.items():
                if newest_name is None or sst["last_entry_ts"] > self._sources_state[newest_name]["last_entry_ts"]:
                    newest_name = name
            if newest_name:
                sst = self._sources_state[newest_name]
                self._state.update({
                    "bg": sst["bg"], "trend": sst["trend"],
                    "delta": sst["delta"], "short_delta": sst["short_delta"],
                    "long_delta": sst["long_delta"],
                    "iob": sst["iob"], "bolus_iob": sst["bolus_iob"],
                    "basal_iob": sst["basal_iob"], "cob": sst["cob"],
                    "params": sst["params"],
                    "final_source": sst["final_source"], "final_value": sst["final_value"],
                    "last_entry_ts": sst["last_entry_ts"],
                    "bg_source": newest_name,
                    "sources": {n: dict(v) for n, v in self._sources_state.items()},
                })
            # 各源延迟（分钟）
            now_ms = int(time.time() * 1000)
            delays = {}
            for name, sst in self._sources_state.items():
                delays[name] = (round((now_ms - sst["last_entry_ts"]) / 60000.0)
                                if sst["last_entry_ts"] else 9999)
            self._state["source_delays"] = delays

        client = first_client  # 心跳/选主用第一个源
        had_new_data = self._state["bg"] > 0

        # 3. 手表电量 / 泵数据（设备属性，取所有源中最新，与任务绑定源无关）
        newest_watch = None
        newest_pump = None
        for item in all_statuses:
            d = parse_devicestatus(item)
            if (d["uploader_battery"] is not None
                    and not d["device"].startswith("aaps-push:")
                    and (newest_watch is None or d["ts"] > newest_watch["ts"])):
                newest_watch = d
            if d["pump_ts"] and (newest_pump is None or d["pump_ts"] > newest_pump["pump_ts"]):
                newest_pump = d
        if newest_watch:
            with self._lock:
                self._state["watch_battery"] = newest_watch["uploader_battery"]
                self._state["watch_charging"] = newest_watch["is_charging"]
                self._state["watch_device"] = newest_watch["device"]
                self._state["watch_ts"] = newest_watch["ts"]
            wts = newest_watch["ts"]
            wb = newest_watch["uploader_battery"]
            if not self._watch_hist or self._watch_hist[-1][0] != wts:
                self._watch_hist.append((wts, wb))
            cutoff30 = wts - 30 * 60 * 1000
            older = [(t, b) for (t, b) in self._watch_hist if t <= cutoff30]
            drop = (older[-1][1] - wb) if older else None
            with self._lock:
                self._state["watch_drop_30m"] = drop
        if newest_pump:
            with self._lock:
                self._state["basal_rate"] = newest_pump["basal_rate"]
                self._state["reservoir"] = newest_pump["reservoir"]
                self._state["pump_status"] = newest_pump["pump_status"]
                self._state["pump_ts"] = newest_pump["pump_ts"]

        # 4. 心跳上报与选主（双端去重的关键）
        is_leader, reason = self._elect_leader(cfg, client)

        # 5. 只有主实例执行规则引擎（防止云端+手机重复推送）
        if is_leader:
            self._run_rules(cfg, now)

        with self._lock:
            self._state["time_str"] = now.strftime("%Y-%m-%d %H:%M:%S")
            self._run_state.update({
                "is_leader": is_leader,
                "leader_reason": reason,
                "last_cycle_ok": True,
                "last_cycle_time": now.strftime("%Y-%m-%d %H:%M:%S"),
                "cycles": self._run_state["cycles"] + 1,
            })

    def _calc_final_decision(self, params, sst=None):
        """与 v03 一致：各预测BG取最小，若低于 minGuardBG 则用 minGuardBG 兜底。
        sst=None 时写入全局状态（兼容），否则写入对应数据源状态。"""
        target = sst if sst is not None else self._state
        preds = [(k, params[k]) for k in ("IOBpredBG", "COBpredBG", "UAMpredBG", "minPredBG")
                 if params.get(k, 0) > 0]
        if not preds:
            target["final_source"] = "无有效预测"
            target["final_value"] = 0.0
            return
        source, value = min(preds, key=lambda kv: kv[1])
        guard = params.get("minGuardBG", 0)
        if guard > 0 and value < guard:
            source, value = "minGuardBG", guard
        target["final_source"] = source
        target["final_value"] = value
        params["EventualBG"] = value

    def _elect_leader(self, cfg, client):
        """选主逻辑：
        - primary/standby：配置固定角色
        - auto：心跳写 NS devicestatus，心跳最新且存活的实例为主。
          云主机 + 手机双开时自然只有一个主，实现去重与故障接管。"""
        role = cfg.get("instance_role", "auto")
        if role == "primary":
            return True, "角色固定为主实例"
        if role == "standby":
            return False, "角色固定为备实例"

        interval = max(int(cfg.get("interval", 60)), 15)
        alive_window_ms = interval * 1000 * 3 + 30000
        now_ms = int(time.time() * 1000)

        if not self._ns_sources(cfg):
            return True, "未配置NS，单实例模式"
        hb_ok = client.upload_heartbeat(self.device_name, self.instance_id, now_ms)
        if not hb_ok:
            return True, "NS心跳不可用，按单实例模式运行"

        statuses = client.fetch_devicestatus(count=40) or []
        peers = {}
        for item in statuses:
            dev = str(item.get("device", ""))
            if dev.startswith("aaps-push:"):
                peers[dev] = max(peers.get(dev, 0), int(item.get("date", 0) or 0))

        others_alive = {d: t for d, t in peers.items()
                        if d != self.device_name and now_ms - t < alive_window_ms}
        if not others_alive:
            return True, "当前唯一存活实例（主）"
        newest_other = max(others_alive.values())
        if peers.get(self.device_name, now_ms) > newest_other:
            return True, "心跳最新，接管主实例"
        elif peers.get(self.device_name, now_ms) == newest_other:
            # 心跳相同则按实例 id 仲裁，保证双端结果一致
            min_id_device = min([d for d, t in others_alive.items() if t == newest_other]
                                + [self.device_name])
            return self.device_name == min_id_device, "心跳相同，按实例ID仲裁"
        return False, "由其他实例负责推送（主备互备中）"

    # ---------- 规则执行 ----------
    def _run_rules(self, cfg, now):
        unit = cfg.get("unit", "mmol/L")
        tasks = cfg.get("automation_tasks", [])
        src_names = [s.get("name") or s.get("url", "") for s in self._ns_sources(cfg)]
        delay_types = {"血糖延迟(分钟)", "泵延迟(分钟)", "泵状态异常", "泵药量(单位)"}
        if not self._state["bg"]:
            # 尚无血糖数据：只执行纯断线/泵类任务（NS断线报警仍需工作）
            tasks = [t for t in tasks
                     if t.get("conditions") and all(c.get("type") in delay_types
                                                    for c in t["conditions"])]
        for task in tasks:
            try:
                # 按任务绑定的数据源构建快照（旧任务默认第一个源）
                src_name = task.get("ns_source") or (src_names[0] if src_names else None)
                with self._lock:
                    ctx = rule_engine.build_snapshot_ctx(self._state, unit, src_name)
                snap = {"now_ts": time.time(), "ctx": ctx}
                rule_engine.check_and_execute(task, snap, cfg, self.db, now)
            except Exception as e:
                logging.exception("任务[%s]执行异常: %s", task.get("name"), e)
                self.db.add_event("ERROR", task.get("name", "?"), "-", "执行异常: %s" % e)

    # ---------- 对外快照（API 用） ----------
    def snapshot(self):
        from .deltas import convert_delta_mgdl
        cfg = self.config_store.get()
        unit = cfg.get("unit", "mmol/L")
        with self._lock:
            s = dict(self._state)
            r = dict(self._run_state)
        return {
            "instance_id": self.instance_id,
            "unit": unit,
            "bg": round(s["bg"] / 18, 1) if unit != "mg/dL" else round(s["bg"], 1),
            "trend": s["trend"],
            "delta": convert_delta_mgdl(s["delta"], unit),
            "short_delta": convert_delta_mgdl(s["short_delta"], unit),
            "long_delta": convert_delta_mgdl(s["long_delta"], unit),
            "iob": s["iob"], "bolus_iob": s["bolus_iob"], "basal_iob": s["basal_iob"],
            "cob": s["cob"],
            "final_source": s["final_source"],
            "final_value": round(s["final_value"] / 18, 1) if unit != "mg/dL" else round(s["final_value"], 1),
            "params": s["params"],
            "last_entry_ts": s["last_entry_ts"],
            "watch_battery": s["watch_battery"],
            "watch_charging": s["watch_charging"],
            "watch_device": s["watch_device"],
            "watch_offline_min": (round((time.time() - s["watch_ts"] / 1000) / 60)
                                  if s["watch_ts"] else None),
            "watch_drop_30m": (round(s["watch_drop_30m"], 1)
                               if s.get("watch_drop_30m") is not None else None),
            # 泵数据
            "basal_rate": (round(s["basal_rate"], 2) if s["basal_rate"] is not None else None),
            "reservoir": s["reservoir"],
            "pump_status": s["pump_status"],
            "pump_offline_min": (round((time.time() - s["pump_ts"] / 1000) / 60)
                                 if s["pump_ts"] else None),
            # 多数据源
            "bg_source": s["bg_source"],
            "source_delays": s["source_delays"],
            "bg_delay_min": (round((time.time() - s["last_entry_ts"] / 1000) / 60)
                             if s["last_entry_ts"] else None),
            # 每源摘要（供任务绑定数据源的界面提示与调试）
            "sources_summary": {
                name: {
                    "bg": (round(v["bg"] / 18, 1) if unit != "mg/dL" else round(v["bg"], 1)),
                    "delta": convert_delta_mgdl(v["delta"], unit),
                    "trend": v["trend"],
                    "delay_min": (s.get("source_delays") or {}).get(name, 9999),
                    "last_entry_ts": v["last_entry_ts"],
                } for name, v in (s.get("sources") or {}).items()
            },
            "data_time": s["time_str"],
            "is_leader": r["is_leader"],
            "leader_reason": r["leader_reason"],
            "last_cycle_time": r["last_cycle_time"],
            "cycles": r["cycles"],
            "errors": r["errors"],
        }
