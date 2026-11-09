"""Central config, all overridable via environment variables."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent

# Runtime data: SQLite db, generated baselines, detector state.
DATA_DIR = Path(os.environ.get("STOCKMON_DATA_DIR", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "stockmon.db"
BASELINE_DIR = DATA_DIR / "baselines"
DETECTOR_STATE_PATH = DATA_DIR / "detector_state.json"

# The stdlib detector at the project root.
DETECTOR_SCRIPT = Path(os.environ.get(
    "BREAKDOWN_DETECTOR", PROJECT_DIR / "a_share_breakdown.py"))

POLL_INTERVAL_SECS = int(os.environ.get("STOCKMON_POLL_INTERVAL", "300"))
REGISTRATION_OPEN = os.environ.get("STOCKMON_REGISTRATION_OPEN", "1") == "1"

# 盘中分时图前端刷新间隔（秒）；数据源分钟级，默认 60s 对齐
INTRADAY_REFRESH_SECS = int(os.environ.get("STOCKMON_INTRADAY_REFRESH", "60"))

# 邮件通知：全部走环境变量；SMTP_HOST 未配置时邮件功能静默关闭
SMTP_HOST = os.environ.get("STOCKMON_SMTP_HOST", "")
try:
    SMTP_PORT = int(os.environ.get("STOCKMON_SMTP_PORT", "465"))
except ValueError:          # 非法环境变量不应让整个应用起不来
    SMTP_PORT = 465
SMTP_USER = os.environ.get("STOCKMON_SMTP_USER", "")
SMTP_PASS = os.environ.get("STOCKMON_SMTP_PASS", "")
MAIL_FROM = os.environ.get("STOCKMON_MAIL_FROM", SMTP_USER)
SMTP_TLS = os.environ.get("STOCKMON_SMTP_TLS", "ssl")  # ssl | starttls

def smtp_configured() -> bool:
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASS and MAIL_FROM)

SESSION_COOKIE = "stockmon_session"
SESSION_TTL_DAYS = 30

TZ_NAME = "Asia/Shanghai"
