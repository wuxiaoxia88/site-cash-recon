from __future__ import annotations

import pytest

from cashrecon.config import settings_from_dict
from cashrecon.db import Store
from cashrecon.paths import Paths

BASE_CONFIG = {
    "site": {"name": "测试网点", "slug": "test-site"},
    "accounts": [
        {"code": "ZT_MAIN", "name": "中天主账户", "type": "ZT_PREPAY", "collection": "auto", "low_balance_yuan": 5000},
        {"code": "CORP", "name": "对公卡", "type": "CORP_BANK", "collection": "auto", "portal_code": "P-CORP",
         "bank_tail": "0001"},
        {"code": "OWNER_ALIPAY", "name": "店主支付宝", "type": "ALIPAY", "collection": "auto",
         "portal_code": "P-OWNER", "personal_funds": True},
        {"code": "ICBC_CARD", "name": "工行卡", "type": "BANK_CARD", "collection": "auto",
         "portal_code": "P-ICBC", "bank_tail": "0002"},
        {"code": "STAFF_ALIPAY", "name": "人工支付宝", "type": "ALIPAY", "collection": "manual",
         "portal_code": "P-SA"},
        {"code": "STAFF_WECHAT", "name": "人工微信", "type": "WECHAT", "collection": "manual",
         "portal_code": "P-SW"},
        {"code": "CCB_CARD", "name": "人工建行卡", "type": "BANK_CARD", "collection": "manual",
         "portal_code": "P-CCB"},
    ],
    "sources": {
        "ZT_SUMMARY": {"enabled": True, "required": True, "account": "ZT_MAIN"},
        "JOURNAL": {"enabled": True, "required": True},
    },
}


@pytest.fixture
def paths(tmp_path):
    return Paths.resolve(tmp_path / "home").ensure()


@pytest.fixture
def settings(paths):
    import copy
    return settings_from_dict(copy.deepcopy(BASE_CONFIG), paths, {"ZTO_CLI_PRIMARY_URL": "http://primary.test",
                                                                   "ZTO_CLI_PRIMARY_KEY": "k" * 20})


@pytest.fixture
def store():
    with Store(":memory:") as s:
        yield s
