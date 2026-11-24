# -*- coding: utf-8 -*-
"""配置存储：JSON 文件 + 原子写入 + 默认值合并。兼容 v03 配置文件格式。"""
import copy
import json
import logging
import os
import threading
import uuid

DEFAULTS = {
    "url": "",                    # Nightscout 地址
    "secret": "",                 # API_SECRET 或 tokenid（如 admin-xxxx）
    "unit": "mmol/L",             # 显示/规则单位: mmol/L 或 mg/dL
    "interval": 60,               # 拉取间隔(秒)
    "push_api_key": "",           # 微信推送(小毛机器人) API Key
    "default_receivers": [],      # 微信默认接收者
    "bark_keys": [],              # Bark 设备密钥列表
    "push_server_url": "https://api.maobot.org/send_message",
    "bark_server_url": "https://api.day.app",
    "admin_password": "",         # Web 管理台密码（环境变量 ADMIN_PASSWORD 优先）
    "instance_role": "auto",      # auto=通过 NS 心跳自动选主; primary/standby 固定角色
    "automation_tasks": [],
}


class ConfigStore:
    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        self._config = {}
        self.load()

    def load(self):
        with self._lock:
            raw = {}
            if os.path.exists(self.path):
                try:
                    with open(self.path, encoding="utf-8") as f:
                        raw = json.load(f)
                except Exception as e:
                    logging.error("加载配置失败: %s", e)
                    raw = {}
            cfg = copy.deepcopy(DEFAULTS)
            cfg.update({k: v for k, v in raw.items() if v is not None})
            for task in cfg["automation_tasks"]:
                # 兼容 v03 旧任务：补齐字段并分配稳定 id（供 SQLite 状态跟踪）
                task.setdefault("id", uuid.uuid4().hex[:12])
                task.setdefault("name", "未命名任务")
                task.setdefault("enabled", True)
                task.setdefault("execution_type", "repeat")
                task.setdefault("repeat_interval", 180)
                task.setdefault("time_type", "none")
                task.setdefault("time_value", "")
                task.setdefault("weekdays", [False] * 7)
                task.setdefault("to_users", [])
                task.setdefault("push_services", ["wechat"])
                task.setdefault("push_options", {})
                task.setdefault("conditions", [])
                task.setdefault("msg", "")
            self._config = cfg
            return self.get()

    def get(self):
        with self._lock:
            return copy.deepcopy(self._config)

    def update(self, new_cfg):
        """整体替换配置（automation_tasks 全量覆盖），原子落盘。"""
        with self._lock:
            cfg = copy.deepcopy(DEFAULTS)
            cfg.update({k: v for k, v in (new_cfg or {}).items() if v is not None})
            for task in cfg["automation_tasks"]:
                task.setdefault("id", uuid.uuid4().hex[:12])
            self._config = cfg
            self._save()

    def _save(self):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._config, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception as e:
            logging.error("保存配置失败: %s", e)
            raise
