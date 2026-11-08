# STOCKMON — 年线破位监控系统

A 股 / 美股年线（MA250）破位 + 止赢信号自动监控。纯确定性代码，**不依赖任何大模型、
零 API Key**；外部依赖仅为新浪财经 / 东方财富 / 雅虎财经的免费行情接口。

```
浏览器(响应式) ──▶ FastAPI(单进程)
                    ├─ 静态前端（原生JS，无构建步骤）
                    ├─ REST API（注册登录 / 自选维护 / 仪表盘 / 事件 / 日志）
                    ├─ 调度线程：每5分钟 → 生成基线 → 运行检测器 → 结果入库
                    └─ SQLite (webapp/data/stockmon.db)
```

## 本地运行

```bash
pip install -r webapp/requirements.txt
cd 项目根目录
python -m uvicorn webapp.app:app --host 127.0.0.1 --port 8100
# 浏览器打开 http://127.0.0.1:8100 → 注册 → 添加自选
```

> 必须单 worker 运行（调度线程在进程内）。数据目录默认 `webapp/data/`，
> 可用环境变量 `STOCKMON_DATA_DIR` 重定向；备份 = 复制该目录。

## 添加自选

| 市场 | 代码示例 | 说明 |
|---|---|---|
| A股 | `600000` `002008` `300750` | 自动解析 sh/sz 前缀与东财 secid |
| A股指数 | `000001` `399006` `000688` 等 | **常用指数目录点选添加**（含上证B股 000003、深证B股 399003、北证50 899050） |
| 美股 | `NVDA` `AAPL` | secid 探测 NASDAQ/NYSE/AMEX |
| 美股指数 | `DJI` `IXIC` `SPX` `SOX` | 目录点选；SOX 无成交量，量比豁免 |

添加时自动拉取日K（约430自然日）计算 **MA250**（近250收盘均值）与
**周线MACD**（12/26周EMA 的 DIF vs DEA，DIF<DEA 记死叉）。

**费城半导体（SOX）日K来源**：优先雅虎 `^SOX`；雅虎不可达时自动回退
SMH ETF（东财 `105.SMH`）并把 MA250 按实时比值重标定到 SOX 指数标度
（周线MACD 标度不变）。该代理隐含"SOX/SMH 比值恒定"假设，属于已知近似。

## 实时行情与详情页

- **实时行情**：看盘时段（A股盘中/美股盘中粗判）前端每 15 秒轮询
  `GET /api/quotes`（新浪批量报价，每组市场一次 HTTP），仪表盘行、顶部
  ticker、详情页现价/涨跌幅实时刷新并带 LIVE 光点；收盘时段自动降为
  60 秒。状态徽章/止赢/量比仍以检测器收盘快照口径为准。
- **个股详情**：点击仪表盘或自选任意一行 → 巨字现价、距年线、状态/止赢/
  量比徽章、近 140 日收盘迷你走势图（虚线 MA250 基准，年线下方淡红填充）
  与该标的的事件时间线。SOX 图表数据经 SMH 代理重标定并标注。

## 调度纪律（与原检测器一致）

- 每 5 分钟检查交易窗口（A股 9:30–11:30 / 13:00–15:00，收盘确认 15:00–15:35；
  美股 9:30–16:00 ET，收盘确认 16:00–16:35 ET ≈ 北京 05:00–05:35）
- 盘中只预警不确认；收盘确认破位：**MA250 下方 + 量比>1.5 + 周线死叉** 记第1天，
  连续 3 个收盘日站不回 → **有效破位**
- 止赢信号（仅收盘、仅个股）：放量滞涨 > 周线顶背离 > 跌破10日线
- 每周六 06:00–12:00（北京）自动重算全部标的基线
- 双源价格偏差 ≥0.5% 或量比缺失时不下结论（检测器内置）

## Oracle Cloud 部署（Ubuntu）

```bash
# 1. 上传整个 stockmon 目录（含 a_share_breakdown.py 与 webapp/）
sudo apt update && sudo apt install -y python3-pip python3-venv nginx
cd ~/stockmon && python3 -m venv .venv && . .venv/bin/activate
pip install -r webapp/requirements.txt

# 2. systemd（单 worker）
sudo tee /etc/systemd/system/stockmon.service <<'EOF'
[Unit]
Description=stockmon breakdown monitor
After=network-online.target
[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/stockmon
ExecStart=/home/ubuntu/stockmon/.venv/bin/uvicorn webapp.app:app \
          --host 127.0.0.1 --port 8100 --workers 1
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now stockmon

# 3. nginx 反代
sudo tee /etc/nginx/sites-available/stockmon <<'EOF'
server {
    listen 80;
    server_name _;
    location / {
        proxy_pass http://127.0.0.1:8100;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
EOF
sudo ln -sf /etc/nginx/sites-available/stockmon /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

# 4. 防火墙：UBuntu 本机 + OCI 控制台安全列表都要放行 80/443
sudo ufw allow 80/tcp && sudo ufw allow 443/tcp

# 5.（有域名时）HTTPS
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d 你的域名
# 启用 HTTPS 后建议把 webapp/app.py 中 set_session_cookie 的 secure=False 改为 True
```

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `STOCKMON_DATA_DIR` | `webapp/data` | SQLite / 基线 / 检测器状态目录 |
| `STOCKMON_POLL_INTERVAL` | `300` | 调度间隔（秒） |
| `STOCKMON_REGISTRATION_OPEN` | `1` | 设为 `0` 关闭注册 |
| `STOCKMON_INTRADAY_REFRESH` | `60` | 盘中分时图前端刷新间隔（秒） |
| `STOCKMON_SMTP_HOST` / `_PORT` / `_USER` / `_PASS` / `_MAIL_FROM` / `_TLS` | 空 / 465 / - / - / 同USER / ssl | 邮件通知；HOST 留空则关闭 |
| `BREAKDOWN_DETECTOR` | 项目根 `a_share_breakdown.py` | 检测器脚本路径 |
| `BREAKDOWN_BASELINE_DIR` / `BREAKDOWN_STATE_PATH` | data 目录 | 检测器路径覆盖（自动设置） |

### 邮件通知配置

**方式一（推荐）：管理员页面直接配置** —— 管理页 →「邮件通知 SMTP」填服务器/账号/授权码 → 保存 → 发送测试邮件。配置存数据库，优先于环境变量，改完即时生效、无需重启。

**方式二：环境变量**（初始部署或自动化场景）：

1. 准备一个邮箱的 SMTP 授权码（不是登录密码）：
   - **QQ 邮箱**：设置 → 账户 → 开启 SMTP → 生成授权码；服务器 `smtp.qq.com:465`
   - **163 邮箱**：设置 → POP3/SMTP → 开启并生成授权码；服务器 `smtp.163.com:465`
   - **Gmail**：需要应用专用密码；服务器 `smtp.gmail.com:465`
2. systemd 单元里加环境变量后重启：
   ```ini
   Environment=STOCKMON_SMTP_HOST=smtp.qq.com
   Environment=STOCKMON_SMTP_PORT=465
   Environment=STOCKMON_SMTP_USER=你的邮箱
   Environment=STOCKMON_SMTP_PASS=你的授权码
   Environment=STOCKMON_MAIL_FROM=你的邮箱
   ```
3. 页面右上角 🔒 图标 →「邮件通知」→ 填通知邮箱 → 「发送测试邮件」验证

信号触发（破位/止赢/加仓/短线买卖点）时，每个用户收到**自己标的**的摘要邮件（一次多事件合并为一封，不刷屏）。未配置 SMTP 时邮件功能静默关闭，不影响监控。

## 目录结构

```
a_share_breakdown.py   # 检测器（纯标准库，可独立运行/测试）
webapp/
  app.py  auth.py  db.py  baseline_gen.py  scheduler.py  config.py
  static/ (index.html style.css app.js icons.js fonts/)
  data/                # 运行时数据（备份这个目录）
  requirements.txt
```

## 免责声明

所有信号仅供个人参考，不构成投资建议；信号可能因数据源延迟或缺失而搁置，
系统只提醒、不自动交易。
