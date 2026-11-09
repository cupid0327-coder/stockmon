"""Email notifications: stdlib smtplib.

SMTP 配置两级来源：数据库 settings 表（管理员页面可编辑）优先，
环境变量（config.SMTP_*）兜底。均未配置时静默关闭。
"""
import logging
import smtplib
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr

from . import config

log = logging.getLogger("stockmon.mailer")


class MailerError(Exception):
    pass


def _f(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def effective_settings():
    """合并后的生效 SMTP 配置：DB settings 表优先，环境变量兜底。
    返回 {host, port, user, pass, mail_from, tls, configured}。"""
    rows = {}
    try:
        from . import db as dbm
        conn = dbm.connect()
        try:
            rows = {r["key"]: r["value"] for r in
                    conn.execute("SELECT key, value FROM settings")}
        finally:
            conn.close()
    except Exception as e:
        log.warning("settings read failed, falling back to env: %s", e)
    raw_port = str(rows.get("smtp_port") or config.SMTP_PORT or "465").strip()
    try:
        port = int(raw_port)
        if not 1 <= port <= 65535:
            raise ValueError(port)
    except ValueError:
        # 非法端口不能变成 ValueError 冒到接口层（会 500）；按配置错误上报
        raise MailerError(f"SMTP 端口无效：{raw_port!r}（应为 1-65535 的数字）")
    user = rows.get("smtp_user") or config.SMTP_USER
    host = rows.get("smtp_host") or config.SMTP_HOST
    pwd = rows.get("smtp_pass") or config.SMTP_PASS
    mail_from = rows.get("mail_from") or config.MAIL_FROM or user
    tls = (rows.get("smtp_tls") or config.SMTP_TLS or "ssl").lower()
    configured = bool(host and user and pwd and mail_from)
    return {"host": host, "port": port, "user": user, "pass": pwd,
            "mail_from": mail_from, "tls": tls, "configured": configured}


def smtp_configured():
    return effective_settings()["configured"]


def _connect(cfg):
    if cfg["port"] == 465 or cfg["tls"] == "ssl":
        srv = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=20)
    else:
        srv = smtplib.SMTP(cfg["host"], cfg["port"], timeout=20)
        srv.starttls()
    srv.login(cfg["user"], cfg["pass"])
    return srv


def send_mail(to, subject, body):
    """to: str 或 list。返回 True；未配置或失败抛 MailerError。"""
    cfg = effective_settings()
    if not cfg["configured"]:
        raise MailerError("SMTP 未配置：请在管理页「邮件通知 SMTP」填写，"
                          "或设置 STOCKMON_SMTP_* 环境变量")
    to_list = [to] if isinstance(to, str) else list(to)
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr(("STOCKMON", cfg["mail_from"]))
    msg["To"] = ", ".join(to_list)
    try:
        with _connect(cfg) as srv:
            srv.sendmail(cfg["mail_from"], to_list, msg.as_string())
        log.info("mail sent to %s: %s", to_list, subject)
        return True
    except Exception as e:
        log.warning("mail send failed to %s: %s", to_list, e)
        raise MailerError(f"邮件发送失败：{e}")


# 事件类型 → 邮件中的信号名（与前端 EVENT_TYPE 口径一致）
_EVENT_LABEL = {
    "intraday_warning": "盘中跌破预警", "intraday_recross": "盘中站回（记录）",
    "close_break_day1": "破位第 1 天", "close_break_pending": "跌破年线 · 待确认",
    "close_recross_release": "站回年线", "effective_break": "有效破位",
    "close_take_profit": "止赢信号", "close_add_position": "加仓信号",
    "index_intraday_warning": "指数盘中跌破预警", "index_intraday_recross": "指数盘中站回（记录）",
    "index_close_break_day1": "指数破位第 1 天", "index_close_break_pending": "指数跌破年线 · 待确认",
    "index_close_recross_release": "指数站回年线", "index_effective_break": "指数有效破位",
    "us_intraday_warning": "美股盘中跌破预警", "us_intraday_recross": "美股盘中站回（记录）",
    "us_close_break_day1": "美股破位第 1 天", "us_close_break_pending": "美股跌破年线 · 待确认",
    "us_close_recross_release": "美股站回年线", "us_effective_break": "美股有效破位",
    "st_macd_golden": "短线MACD金叉 · 买", "st_macd_dead": "短线MACD死叉 · 卖",
    "st_rsi_oversold_in": "RSI进入超卖区 · 买", "st_rsi_oversold_out": "RSI离开超卖区 · 回升",
    "st_rsi_overbought_in": "RSI进入超买区 · 卖", "st_rsi_overbought_out": "RSI离开超买区 · 回落",
}


def send_event_digest(email, events):
    """events: [{ts, event_type, name, code, price, dist_pct, day_change_pct, note}]
    合并为一封摘要邮件。返回 True；无收件地址返回 False。"""
    if not email:
        return False
    events = [e for e in events if _EVENT_LABEL.get(e.get("event_type"))]
    if not events:
        return False
    lines = []
    for e in events:
        label = _EVENT_LABEL.get(e["event_type"], e["event_type"])
        note = e.get("note") or ""
        lines.append(
            f"[{label}] {e.get('name')} {e.get('code')}\n"
            f"    现价 {e.get('price')} · 距年线 {e.get('dist_pct')}% · "
            f"当日 {e.get('day_change_pct')}%\n"
            + (f"    {note}\n" if note else "")
            + f"    {e.get('ts', '')}")
    body = ("\n".join(lines) +
            "\n\n—— STOCKMON 自动监控 · 信号仅供参考，不构成投资建议")
    subject = f"STOCKMON 信号提醒（{len(events)} 条）"
    return send_mail(email, subject, body)
