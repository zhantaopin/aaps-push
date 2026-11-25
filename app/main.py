# -*- coding: utf-8 -*-
"""AAPS 推送服务 v04（方案 B 重构版）—— Flask Web 管理台 + 调度器入口。

用法:
  python -m app.main                          # 默认 0.0.0.0:8300
  PORT=9000 ADMIN_PASSWORD=xxx python -m app.main
  python -m app.main --port 9000 --data-dir /data
"""
import argparse
import logging
import os
import secrets
import sys
import time
import uuid
from datetime import timedelta
from logging.handlers import RotatingFileHandler

from flask import Flask, jsonify, request, session, send_from_directory

from .config_store import ConfigStore
from .database import Database
from .scheduler import Scheduler
from . import push_channels, rule_engine

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def setup_logging(data_dir):
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)
    file_handler = RotatingFileHandler(
        os.path.join(data_dir, "aaps_push.log"),
        maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)


def create_app(scheduler, config_store, db, admin_password, secure_cookies=False):
    app = Flask(__name__, static_folder=None)
    app.secret_key = db.get_kv("secret_key") or secrets.token_hex(32)
    if not db.get_kv("secret_key"):
        db.set_kv("secret_key", app.secret_key)
    # HTTPS 模式下会话 Cookie 仅经加密通道传输
    app.config["SESSION_COOKIE_SECURE"] = secure_cookies
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    # 更换 Cookie 名，避免与旧版(HTTPS时代)残留的 Secure Cookie 冲突
    app.config["SESSION_COOKIE_NAME"] = "aaps_push_session"
    app.permanent_session_lifetime = timedelta(days=7)  # 会话 7 天有效

    # 登录限速：同一 IP 连续失败 5 次锁定 10 分钟（内存计数，重启即清）
    _login_fails = {}

    def authorized():
        return session.get("authed") is True

    @app.route("/")
    def index():
        resp = send_from_directory(STATIC_DIR, "index.html")
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp

    @app.route("/api/health")
    def health():
        snap = scheduler.snapshot()
        return jsonify({"ok": True, "version": "1.0-beta02", "is_leader": snap["is_leader"],
                        "instance_id": snap["instance_id"],
                        "data_time": snap["data_time"]})

    @app.route("/api/login", methods=["POST"])
    def login():
        ip = request.remote_addr or "?"
        rec = _login_fails.get(ip)
        if rec and rec[1] > time.time():
            return jsonify({"ok": False, "error": "尝试过多，%d 秒后再试"
                            % int(rec[1] - time.time())}), 429
        data = request.get_json(silent=True) or {}
        if data.get("password") == admin_password:
            _login_fails.pop(ip, None)
            session["authed"] = True
            session.permanent = True
            return jsonify({"ok": True})
        rec = _login_fails.setdefault(ip, [0, 0])
        rec[0] += 1
        if rec[0] >= 5:
            rec[0] = 0
            rec[1] = time.time() + 600
            return jsonify({"ok": False, "error": "失败过多，已锁定 10 分钟"}), 429
        return jsonify({"ok": False, "error": "密码错误"}), 401

    @app.route("/api/logout", methods=["POST"])
    def logout():
        session.clear()
        return jsonify({"ok": True})

    @app.before_request
    def auth_gate():
        public = ("/api/health", "/api/login", "/static/")
        if request.path == "/" or request.path.startswith(public):
            return None
        if not authorized():
            return jsonify({"error": "未登录"}), 401
        return None

    @app.route("/api/status")
    def status():
        snap = scheduler.snapshot()
        snap["password_is_default"] = admin_password == "admin"
        return jsonify(snap)

    @app.route("/api/config", methods=["GET"])
    def get_config():
        cfg = config_store.get()
        # 不回传明文密码，避免泄露；前端仅可设置新密码
        cfg["admin_password"] = ""
        return jsonify(cfg)

    @app.route("/api/config", methods=["PUT"])
    def put_config():
        data = request.get_json(silent=True) or {}
        current = config_store.get()
        # 空密码表示不修改
        if not data.get("admin_password"):
            data["admin_password"] = current.get("admin_password", "")
        config_store.update(data)
        db.add_event("INFO", "-", "-", "配置已更新并立即生效")
        return jsonify({"ok": True})

    @app.route("/api/events")
    def events():
        limit = min(int(request.args.get("limit", 100)), 500)
        return jsonify(db.recent_events(limit))

    @app.route("/api/task-save", methods=["POST"])
    def task_save():
        """单独保存一个任务：按 id 更新，无 id 则分配并追加。不触碰其他配置。"""
        data = request.get_json(silent=True) or {}
        task = data.get("task") or {}
        if not task.get("name"):
            return jsonify({"error": "任务名称不能为空"}), 400
        cfg = config_store.get()
        tasks = cfg.setdefault("automation_tasks", [])
        tid = task.get("id")
        if tid:
            for i, t in enumerate(tasks):
                if t.get("id") == tid:
                    tasks[i] = task
                    break
            else:
                tasks.append(task)
        else:
            task["id"] = uuid.uuid4().hex[:12]
            tasks.append(task)
        config_store.update(cfg)
        # 保存即重新生效：清空该任务运行状态（once 当日已触发标记、重复间隔冷却）
        db.set_task_state(task["id"], last=0.0, last_trigger_date="", executed=False)
        db.add_event("INFO", "任务编辑", "-", "任务已单独保存: %s" % task.get("name"))
        return jsonify({"ok": True, "id": task["id"]})

    @app.route("/api/receiver-save", methods=["POST"])
    def receiver_save():
        """单独保存一个接收者：按 old_name 匹配更新（支持改名），找不到则追加。
        改名时级联同步 receiver_groups.members 与任务 receiver_names/to_users。"""
        data = request.get_json(silent=True) or {}
        rc = data.get("receiver") or {}
        rc.pop("_old_name", None)  # 前端临时字段，不落库
        if not rc.get("name"):
            return jsonify({"error": "接收者名称不能为空"}), 400
        old_name = data.get("old_name") or rc["name"]
        new_name = rc["name"]
        cfg = config_store.get()
        receivers = cfg.setdefault("receivers", [])
        if new_name != old_name and any(r.get("name") == new_name for r in receivers):
            return jsonify({"error": "已存在同名接收者: %s" % new_name}), 400
        renamed = False
        for i, r in enumerate(receivers):
            if r.get("name") == old_name:
                receivers[i] = rc
                renamed = old_name != new_name
                break
        else:
            receivers.append(rc)
        if renamed:
            for g in cfg.get("receiver_groups") or []:
                g["members"] = [new_name if m == old_name else m
                                for m in (g.get("members") or [])]
            for t in cfg.get("automation_tasks") or []:
                for key in ("receiver_names", "to_users"):
                    lst = t.get(key)
                    if isinstance(lst, list):
                        t[key] = [new_name if n == old_name else n for n in lst]
        config_store.update(cfg)
        msg = ("接收者改名: %s → %s" % (old_name, new_name) if renamed
               else "接收者已单独保存: %s" % new_name)
        db.add_event("INFO", "接收者编辑", "-", msg)
        return jsonify({"ok": True})

    @app.route("/api/test-push", methods=["POST"])
    def test_push():
        data = request.get_json(silent=True) or {}
        cfg = config_store.get()

        if data.get("receiver"):
            # 按单个接收者的渠道与级别测试（用其独立微信接收人/Bark Key）
            rc = next((r for r in (cfg.get("receivers") or [])
                       if r.get("name") == data["receiver"]), None)
            if not rc:
                return jsonify({"error": "接收者不存在"}), 400
            task = {
                "name": "接收者测试",
                "receiver_names": [rc["name"]],
                "push_options": {"bark_level": rc.get("bark_level") or "timeSensitive",
                                 "bark_sound": "bell", "bark_badge": 1},
            }
            only = None
        else:
            # 连通性测试：可指定通道与 Bark 级别
            channel = data.get("channel", "wechat")
            task = {
                "name": "手动测试",
                "receiver_group": "__all__",
                "push_options": {"bark_level": data.get("bark_level") or "timeSensitive",
                                 "bark_sound": "bell", "bark_badge": 1},
            }
            only = channel

        message = data.get("message") or "🔔 AAPS推送服务测试\n\n这是一条测试消息。"
        results = push_channels.dispatch(task, message, cfg, db,
                                         task_name="手动测试", only_channel=only)
        return jsonify({"ok": all(r["ok"] for r in results), "results": results})

    @app.route("/api/dry-run", methods=["POST"])
    def dry_run():
        """不实际推送，输出每个任务当前判定结果（调试规则用）。按任务绑定源判定。"""
        cfg = config_store.get()
        from datetime import datetime
        state = scheduler._state
        src_names = [s.get("name") or s.get("url", "") for s in scheduler._ns_sources(cfg)]
        report = []
        now = datetime.now()
        for task in cfg.get("automation_tasks", []):
            task["_st"] = db.get_task_state(task.get("id"))
            src_name = task.get("ns_source") or (src_names[0] if src_names else None)
            snap = {
                "now_ts": time.time(),
                "ctx": rule_engine.build_snapshot_ctx(state, cfg.get("unit", "mmol/L"), src_name),
            }
            tv = rule_engine.effective_task(task)  # 运行时视图：时间已迁为组内条件
            time_ok, time_reason = rule_engine.evaluate_time(tv, now)
            cond_ok, cond_reason, hit_names = rule_engine.evaluate_conditions(tv, snap, now)
            report.append({
                "name": task.get("name"),
                "source": src_name or "-",
                "enabled": task.get("enabled", True),
                "time_ok": time_ok, "time_reason": time_reason,
                "conditions_ok": cond_ok, "conditions_reason": cond_reason,
                "hit_groups": hit_names,
                "would_trigger": (task.get("enabled", True)
                                  and time_ok and cond_ok
                                  and not (task.get("execution_type") == "once"
                                           and task["_st"].get("last_trigger_date")
                                           == now.strftime("%Y-%m-%d"))),
            })
        return jsonify({"ok": True, "report": report})

    return app


def main():
    parser = argparse.ArgumentParser(description="AAPS 推送服务 v04")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("PORT", 8300)))
    parser.add_argument("--data-dir",
                        default=os.environ.get("DATA_DIR",
                                               os.path.expanduser("~/.aaps-push")))
    args = parser.parse_args()

    os.makedirs(args.data_dir, exist_ok=True)
    setup_logging(args.data_dir)

    config_path = os.path.join(args.data_dir, "config.json")
    config_store = ConfigStore(config_path)
    db = Database(os.path.join(args.data_dir, "state.db"))

    # 接收者模型迁移：default_receivers(纯名字) → receivers(含渠道与Bark级别)
    cfg0 = config_store.get()
    if not cfg0.get("receivers") and (cfg0.get("default_receivers")
                                      or cfg0.get("receiver_groups") is None):
        cfg0["receivers"] = [{"name": n, "channels": ["wechat"],
                              "bark_level": "timeSensitive"}
                             for n in (cfg0.get("default_receivers") or [])]
        cfg0["receiver_groups"] = cfg0.get("receiver_groups") or []
        config_store.update(cfg0)
        logging.info("接收者模型迁移完成: %d 名接收者", len(cfg0["receivers"]))

    # 多数据源迁移：旧单 url/secret → ns_sources
    cfg0 = config_store.get()
    if not cfg0.get("ns_sources") and cfg0.get("url"):
        cfg0["ns_sources"] = [{"name": "默认", "url": cfg0["url"],
                               "secret": cfg0.get("secret", ""), "group": ""}]
        config_store.update(cfg0)
        logging.info("NS数据源迁移完成: 1 个默认源")

    scheduler = Scheduler(config_store, db)

    admin_password = (os.environ.get("ADMIN_PASSWORD")
                      or config_store.get().get("admin_password") or "")
    if not admin_password:
        if os.environ.get("ALLOW_DEFAULT") == "1":  # 仅限本地开发
            admin_password = "admin"
            logging.warning("⚠️ ALLOW_DEFAULT=1 开发模式：使用默认密码 admin")
        else:
            logging.error("未设置管理密码：请通过环境变量 ADMIN_PASSWORD 配置"
                          "（开发环境可用 ALLOW_DEFAULT=1）")
            sys.exit(1)

    scheduler.start()

    # TLS（方案4：自签证书 HTTPS）。证书路径由环境变量 TLS_CERT/TLS_KEY 指定，
    # 未配置或文件缺失时回退 HTTP 并告警。
    # 说明：waitress 2.1+ 已移除 TLS 支持，HTTPS 模式改用 Werkzeug 内置多线程服务器
    # （个人管理台并发量极低，完全够用）。
    tls_cert = os.environ.get("TLS_CERT", "")
    tls_key = os.environ.get("TLS_KEY", "")
    app = create_app(scheduler, config_store, db, admin_password,
                     secure_cookies=bool(tls_cert and tls_key))
    if tls_cert and tls_key and os.path.exists(tls_cert) and os.path.exists(tls_key):
        logging.info("HTTPS 已启用: %s", tls_cert)
        logging.info("管理台: https://0.0.0.0:%s", args.port)
        app.run(host=args.host, port=args.port, threaded=True,
                ssl_context=(tls_cert, tls_key))
    else:
        logging.warning("⚠️ 未配置 TLS 证书，管理台将以 HTTP 明文运行！")
        logging.info("管理台: http://0.0.0.0:%s", args.port)
        from waitress import serve
        serve(app, host=args.host, port=args.port, threads=8)


if __name__ == "__main__":
    main()
