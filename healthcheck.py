# -*- coding: utf-8 -*-
"""容器健康检查：HTTP/HTTPS 任一模式探测 /api/health 成功即健康。"""
import ssl
import sys
import urllib.request

for proto in ("http", "https"):
    try:
        ctx = ssl._create_unverified_context() if proto == "https" else None
        r = urllib.request.urlopen("%s://127.0.0.1:8300/api/health" % proto,
                                   timeout=5, context=ctx)
        if b"ok" in r.read():
            sys.exit(0)
    except Exception:
        pass
sys.exit(1)
