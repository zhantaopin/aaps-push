# -*- coding: utf-8 -*-
"""推送网关：微信(小毛机器人)与 Bark 通道，均已用 requests 直连（去掉 v03 的 curl 依赖）。

接收者模型：
  receivers: [{name, channels:["wechat","bark"], bark_level}]
  receiver_groups: [{name, members:[名字]}]
dispatch 解析：任务分组 → 成员 → 按各自渠道发送；Bark 为设备级通道，
选中成员中任一勾选 Bark 即发送一次（级别取成员中最高，避免漏报）。
"""
import logging
import uuid

import requests

# Bark 级别严重度（数值越大越强），用于多成员时取最高级别
BARK_LEVEL_SEVERITY = {"passive": 1, "active": 2, "timeSensitive": 3, "critical": 4}
SEVERITY_TO_LEVEL = {v: k for k, v in BARK_LEVEL_SEVERITY.items()}
BARK_LEVEL_ORDER = ["passive", "active", "timeSensitive", "critical"]


class WeChatChannel:
    def __init__(self, server_url, api_key):
        self.server_url = (server_url or "").rstrip("/")
        self.api_key = api_key or ""

    def send(self, recipient, message):
        """返回 (成功?, 详情)。成功判定沿用 v03：HTTP 202。
        Idempotency-Key：每条消息一个编号，网络重试时不会产生重复消息。"""
        try:
            r = requests.post(
                self.server_url,
                headers={"Content-Type": "application/json", "X-API-Key": self.api_key,
                         "Idempotency-Key": str(uuid.uuid4())},
                json={"ToUserName": recipient, "Content": message},
                timeout=30)
            if r.status_code == 202:
                return True, "已发送至 %s" % recipient
            return False, "状态码 %s: %s" % (r.status_code, r.text[:200])
        except Exception as e:
            return False, "请求异常: %s" % e


class BarkChannel:
    def __init__(self, server_url):
        self.server_url = (server_url or "").rstrip("/")

    def send(self, key, message, options=None):
        options = options or {}
        lines = message.split("\n")
        title = lines[0] if lines else "AAPS提醒"
        data = {"body": message, "title": title}
        level = options.get("bark_level") or "timeSensitive"
        data["level"] = level if level in BARK_LEVEL_SEVERITY else "timeSensitive"
        if options.get("bark_sound") and options["bark_sound"] != "none":
            data["sound"] = options["bark_sound"]
        if options.get("bark_badge") is not None:
            data["badge"] = int(options["bark_badge"])
        if options.get("bark_category"):
            data["category"] = options["bark_category"]
        if options.get("bark_icon"):
            data["icon"] = options["bark_icon"]
        try:
            r = requests.post(f"{self.server_url}/{key}", data=data, timeout=15)
            if r.status_code == 200:
                return True, "已发送(%s)" % data["level"]
            return False, "状态码 %s: %s" % (r.status_code, r.text[:200])
        except Exception as e:
            return False, "请求异常: %s" % e


def _normalize_receivers(config):
    """迁移兼容：无 receivers 时由 default_receivers 生成（默认微信通道）。"""
    rs = config.get("receivers") or []
    if rs:
        return rs
    return [{"name": n, "channels": ["wechat"], "bark_level": "timeSensitive"}
            for n in (config.get("default_receivers") or [])]


def resolve_members(task, config):
    """解析任务应通知的成员。

    支持：receiver_groups(组名数组) + receiver_names(个人数组) 并集去重；
    兼容旧 receiver_group(单值) 与 to_users(名字数组)。
    返回 (选中接收者对象列表, 不在名单里的旧名字列表)。
    """
    receivers = _normalize_receivers(config)
    names = [r["name"] for r in receivers]
    members = set()

    groups = task.get("receiver_groups")
    if groups is None and task.get("receiver_group"):
        groups = [task["receiver_group"]]  # 兼容旧单值
    for gname in groups or []:
        if gname == "__all__":
            continue
        g = next((g for g in (config.get("receiver_groups") or [])
                  if g.get("name") == gname), None)
        if g:
            members.update(g.get("members") or [])
    for n in task.get("receiver_names") or []:
        members.add(n)
    if not members and task.get("to_users"):
        members.update(task["to_users"])  # 旧任务兼容
    if not members:
        members = set(names)  # 全部接收者

    selected = [r for r in receivers if r["name"] in members]
    bare = [m for m in members if m not in names]
    return selected, bare


def _max_bark_level(members):
    """选中成员中 Bark 级别最高者（警告>时效性>常规>静默）。"""
    best = 3  # 默认时效性
    for r in members:
        best = max(best, BARK_LEVEL_SEVERITY.get(r.get("bark_level"), 3))
    return SEVERITY_TO_LEVEL[best]


def dispatch(task, message, config, db, task_name="测试", only_channel=None):
    """按接收者模型的渠道配置分发消息。返回结果列表 [{channel, target, ok, detail}]。

    接收者账号：微信收件人=wechat_to(缺省=名字)；Bark Key=每人独立(缺省回退全局bark_keys)。
    only_channel: 限定只走某通道（连通性测试用），None=按成员配置全发。
    """
    results = []
    selected, bare = resolve_members(task, config)

    # 微信目标：(收件人, 服务地址, API Key)——各自字段缺省回退全局
    wx_targets = []
    for r in selected:
        if "wechat" in (r.get("channels") or []):
            wx_targets.append((
                r.get("wechat_to") or r["name"],
                (r.get("wechat_url") or "").strip() or config.get("push_server_url"),
                (r.get("wechat_key") or "").strip() or config.get("push_api_key")))
    wx_targets += [(b, config.get("push_server_url"), config.get("push_api_key")) for b in bare]
    bark_members = [r for r in selected if "bark" in (r.get("channels") or [])]
    bark_level = (task.get("push_options") or {}).get("bark_level") or \
        (_max_bark_level(bark_members) if bark_members else "timeSensitive")

    # 微信通道
    if only_channel in (None, "wechat"):
        if not wx_targets or all(not k for _, _, k in wx_targets):
            results.append({"channel": "wechat", "target": "-", "ok": False,
                            "detail": "微信推送未配置（接收者与全局均缺 API Key）"})
        else:
            ok_cnt = 0
            for rcpt, url, key in wx_targets:
                if not key:
                    results.append({"channel": "wechat", "target": rcpt, "ok": False,
                                    "detail": "缺少 API Key（接收者与全局均未配置）"})
                    continue
                ch = WeChatChannel(url, key)
                ok, detail = ch.send(rcpt, message)
                ok_cnt += 1 if ok else 0
                results.append({"channel": "wechat", "target": rcpt, "ok": ok, "detail": detail})
            db.add_event("INFO", task_name, "wechat",
                         "微信推送完成 %d/%d" % (ok_cnt, len(wx_targets)))

    # Bark 通道：按接收者各自的 bark_key/bark_url 发送（多设备各响各的），
    # 无独立 Key 回退全局 bark_keys，无独立地址回退全局 bark_server_url
    if only_channel in (None, "bark") and (bark_members or only_channel == "bark"):
        bark_targets = []  # (key, url)
        for r in bark_members:
            k = (r.get("bark_key") or "").strip()
            u = (r.get("bark_url") or "").strip() or config.get("bark_server_url")
            if k and (k, u) not in bark_targets:
                bark_targets.append((k, u))
        if not bark_targets:
            bark_targets = [(k, config.get("bark_server_url"))
                            for k in (config.get("bark_keys") or [])]
        if not bark_targets:
            results.append({"channel": "bark", "target": "-", "ok": False,
                            "detail": "Bark 推送未配置（接收者未填 Bark Key 且无全局 Keys）"})
        else:
            ok_cnt = 0
            opts = dict(task.get("push_options") or {})
            opts["bark_level"] = bark_level
            for key, url in bark_targets:
                ch = BarkChannel(url)
                display = key[:10] + "..." + key[-5:] if len(key) > 15 else key
                ok, detail = ch.send(key, message, opts)
                ok_cnt += 1 if ok else 0
                results.append({"channel": "bark", "target": display, "ok": ok, "detail": detail})
            who = "、".join(r["name"] for r in bark_members) or "-"
            db.add_event("INFO", task_name, "bark",
                         "Bark推送完成 %d/%d (%s级, %s)" % (ok_cnt, len(bark_targets), bark_level, who))
    return results
