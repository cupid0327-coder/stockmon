"""pytest 基座：把运行时数据目录指到临时目录，绝不碰 webapp/data。

必须在导入任何 webapp.* 之前设置 STOCKMON_DATA_DIR —— config 是在
模块导入时读取环境变量的。
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP_DATA = tempfile.mkdtemp(prefix="stockmon-test-")
os.environ["STOCKMON_DATA_DIR"] = _TMP_DATA
os.environ.setdefault("STOCKMON_SMTP_HOST", "")

import pytest  # noqa: E402

from webapp import config, db  # noqa: E402

_TABLES = ("events", "instrument_status", "instruments", "sessions",
           "users", "runs", "settings", "cache_blob")


@pytest.fixture(scope="session", autouse=True)
def _schema():
    assert str(config.DATA_DIR).startswith(tempfile.gettempdir())
    db.init_db()
    yield


@pytest.fixture(autouse=True)
def clean_db():
    """每个用例一张干净的库（表可能还没建，忽略缺表错误）。"""
    def wipe():
        conn = db.connect()
        try:
            for t in _TABLES:
                try:
                    conn.execute(f"DELETE FROM {t}")
                except Exception:
                    pass
            conn.commit()
        finally:
            conn.close()
    wipe()
    yield
    wipe()


@pytest.fixture
def conn():
    c = db.connect()
    try:
        yield c
    finally:
        c.close()
