<div align="center">

# 🩸 AAPS 推送服务

**为 AAPS / 人工胰腺闭环用户打造的自托管血糖监控与预警推送服务**

[![Version](https://img.shields.io/badge/version-v1.0--beta01-blue)]()
[![License](https://img.shields.io/badge/license-PolyForm--NC--1.0.0-orange)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-green)]()
[![Docker](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)]()
[![Platform](https://img.shields.io/badge/platform-AAPS%20%7C%20Trio%20%7C%20Loop-lightgrey)]()

**持续轮询 Nightscout → 17 种条件自由组合判断异常 → 微信 / Bark 实时推送到你和家人手机**

</div>

---

## ⚠️ 免责声明

> [!IMPORTANT]
> 1. 本项目**不是医疗器械**，输出的所有通知**不构成任何医疗建议**。
> 2. 血糖数据的采集、计算与推送可能延迟、失败或出错，**绝不能**作为治疗决策（如注射胰岛素、进食补糖）的唯一依据。
> 3. 请始终以你医生的建议和专业医疗器械为准。
> 4. 本项目仅供个人学习与自我管理使用，作者不对数据准确性、服务可用性及任何后果承担责任。

---

## ✨ 功能特性

| 模块 | 说明 |
|---|---|
| 📡 多数据源 | 添加多个 Nightscout 站点（地址 + token），可命名可分组，各自独立采集与增量计算，任务可绑定指定源 |
| 🧮 官方增量算法 | delta / 短增量 / 长增量采用 AAPS 官方口径（5/15/30 分钟三档窗口平均） |
| 🔔 17 种预警条件 | 血糖绝对值 / delta、短、长增量 / IOB（总、大剂量、基础率）/ COB / 基础率 / 剩余药量 / 泵状态 / 血糖、泵延迟 / 最终预测BG / 手表电量、充电状态、30分钟掉电……与/或自由组合，每条带中文说明与中文比较符 |
| 📋 一键任务模板 | 内置低血糖、高血糖、快速下降、低电量等常用预警模板 |
| 👥 接收者模型 | 每位接收者独立配置微信（用户ID + API Key）和/或 Bark（Key + 级别）；支持接收者分组；任务按"分组 + 个人"多选分发，按各自的渠道配置推送 |
| ⏱ 提醒频率 | 一次性 / 循环提醒（间隔可设）/ 恢复自动报平安 |
| 🧪 模拟判定 | 不发真实推送即可预演任务当前是否命中，逐条显示实时取值 |
| 📜 事件日志 | 每次触发记录时间、命中原因与推送结果 |
| ❤️‍🔥 多实例去重 | 云端 + 本地同时跑也不重复推送（Nightscout 心跳自动选主） |
| 📱 手机友好 | Web 管理台响应式设计，全中文界面 |

---

## 🚀 快速开始

### 环境要求

- **推荐**：任意能跑 Docker 的机器（云服务器、NAS、树莓派、旧电脑均可）
- **备选**：Python 3.11+

### Docker 一键部署（推荐）

```bash
git clone https://github.com/zhantaopin/aaps-push.git
cd aaps-push
./deploy.sh                          # 默认端口 8300，默认密码 admin
# 或自定义端口与密码：
PORT=9000 ADMIN_PASSWORD=你的强密码 ./deploy.sh
```

### 手动 Python 运行

```bash
pip install -r requirements.txt
DATA_DIR=./data ADMIN_PASSWORD=你的强密码 python3 -m app.main --port 8300
```

### 验证

浏览器打开 `http://<服务器IP>:8300`，输入管理密码登录。

```bash
curl http://127.0.0.1:8300/api/health   # {"ok":true,"version":"1.0-beta01",...}
```

---

## 🖥 服务管理

<details>
<summary><b>启动 / 停止 / 重启 / 日志 / 升级 / 卸载（点击展开）</b></summary>

```bash
# 启动（首次自动构建镜像）
docker compose up -d

# 停止（数据保留）
docker compose stop

# 重启
docker compose restart

# 停止并移除容器（data/ 配置数据保留）
docker compose down

# 查看实时日志
docker compose logs -f

# 升级（配置数据都在 data/ 目录，代码更新不影响）
git pull && docker compose up -d --build

# 卸载
# 1.（可选）先在管理台导出配置 JSON 备份
# 2. 停止并删除镜像
docker compose down --rmi all
# 3. 删除项目目录（连带删除 data/ 下的全部配置与数据库，删前请确认已备份）
cd .. && rm -rf aaps-push
```

</details>

---

## 📖 使用指南

### 1️⃣ 配置 Nightscout 数据源

**设置 → 数据源 → 添加**

| 字段 | 说明 |
|---|---|
| 名称 | 自定义（如"默认"、"爸妈的"），任务里按名称选择 |
| 地址 | Nightscout 站点 URL，如 `https://xxx.herokuapp.com` 或自建地址 |
| Token | NS 的 `token`（在 NS 管理后台创建，需 `read` 权限）；站点未开鉴权可留空 |

保存后等一个采集周期（默认 60 秒），状态卡出现血糖数据即成功。

### 2️⃣ 配置接收者（推送目标）

**接收者 → 新建接收者**，每个接收者可勾选一个或多个渠道：

**微信（小毛机器人）**

| 字段 | 从哪获取 |
|---|---|
| 微信接收人 | 关注小毛机器人后，在机器人后台/对话里查看；**个人私聊填你的微信用户ID；发群填群ID（形如 `xxxx@chatroom`）** |
| 微信 Key | 小毛机器人后台的 API Key 页面 |
| 微信地址 | 默认 `https://api.maobot.org/send_message`，一般不用改 |

> [!WARNING]
> 小毛机器人**私聊默认只允许发给本人**；要发给家人或群，需把机器人拉进群并使用群ID，或按机器人后台的授权说明开通。

**Bark（iOS）**

| 字段 | 从哪获取 |
|---|---|
| Bark Key | App Store 安装 [Bark](https://apps.apple.com/app/bark/id1401237055)，打开 App 复制首页 URL 中 `https://api.day.app/` 后面那段 |
| Bark 地址 | 默认 `https://api.day.app`，自建 Bark 服务器则填自建地址 |
| 通知级别 | `critical`（持续响铃）> `timeSensitive`（时效性）> `active`（默认响声）> `passive`（静默）。任务发给多人时按成员取最高级别生效 |

每个接收者卡片有 **测试** 按钮，只测你勾选的渠道。

> [!TIP]
> **连发**：默认"不连发"；在接收者配置详情里选"连发 2~5 次"后，同一条消息会隔 1~10 秒（默认 2 秒）重复推送，**微信和 Bark 都生效**。不容易听见的手机建议 3次×2秒。

### 3️⃣ 接收者分组

**接收者分组 → 新建分组**，勾选成员（如"全家"）。任务里可同时勾选**分组 + 个人**（去重后最多 10 项），推送时按每个接收者自己的渠道配置分发。

### 4️⃣ 创建预警任务

**任务 → 新建任务**（或从模板一键创建）：

| 字段 | 说明 |
|---|---|
| 名称 | 自定义（任务按分类折叠展示，支持自定义排序） |
| 数据源 | 该任务绑定哪个 NS 源（留空用第一个） |
| 提醒频率 | 一次性 / 循环（间隔分钟，触发后进入冷却）/ 恢复后再次提醒 |
| 触发条件 | 17 种条件自由组合，支持"与/或"逻辑，逐条中文说明 |
| 通知内容 | 推送文案，自动附上当前血糖、增量等实时数据 |
| 接收者 | 分组 + 个人多选 |
| 模拟判定 | 保存前预演：显示每条条件实时取值与是否命中 |

### 5️⃣ 事件日志

首页下方实时记录每次触发的时间、命中条件与推送结果，用于校验任务是否按预期工作。

---

## 🔒 安全建议

1. **立即修改默认密码**（登录页下方"修改密码"）
2. **不要把 8300 端口长期暴露公网**：建议用完即关安全组，或仅通过 VPN/SSH 隧道访问
3. 生产环境建议 Nginx/Caddy 反向代理并启用 HTTPS
4. 定期在管理台导出配置 JSON 备份
5. `.env` 与 `data/` 含敏感信息，勿提交到公开仓库（本项目 `.gitignore` 已排除）

---

## ❓ 常见问题

<details>
<summary><b>微信推送返回 403 "私聊只允许本人"？</b></summary>

填的不是你自己的微信用户ID，或机器人未授权私聊他人。发群请填群ID（`xxxx@chatroom`）并确认机器人已在群里。
</details>

<details>
<summary><b>群里收不到消息？</b></summary>

三个检查：① 群ID 是否正确（机器人后台查）② 机器人是否已被拉进群 ③ 小毛后台发送日志有无拦截记录。
</details>

<details>
<summary><b>增量（delta）显示 0？</b></summary>

窗口内只有一条血糖读数时无差值可算（刚恢复数据/刚启动属正常），等下个周期有新读数即恢复。
</details>

<details>
<summary><b>云端和本地同时跑会重复推送吗？</b></summary>

不会。多实例通过 Nightscout 心跳自动选主，只有主实例执行规则。
</details>

<details>
<summary><b>支持 Gluroo / Dexcom 等其他数据源吗？</b></summary>

当前仅支持 Nightscout 兼容 API（AAPS / Trio / Loop 用户均适用）。欢迎提 Issue 讨论其他数据源支持。
</details>

---

## 🛣 路线图

- [ ] 英文 README
- [ ] 管理台截图
- [ ] 更多推送渠道（Telegram / 钉钉 / 飞书）
- [ ] 数据源健康看板

欢迎通过 Issue 提需求、PR 提交改进。

---

## 📄 许可证与商用授权

本项目基于 [PolyForm Noncommercial License 1.0.0](LICENSE) 授权：

| 用途 | 是否允许 |
|---|---|
| 个人使用、家庭使用 | ✅ 免费 |
| 学习研究、慈善/教育/医疗公益组织 | ✅ 免费 |
| 商业使用（出售、付费服务、企业内部商用） | ❌ 需事先联系作者获得**书面授权** |

商用授权联系：GitHub: [@zhantaopin](https://github.com/zhantaopin)

---

<div align="center">

**用爱发电，为了每一位闭环用户的安稳夜晚 🌙**

</div>
