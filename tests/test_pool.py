"""pool.py + `pool build`：从数据源重建板块代码表。"""
from __future__ import annotations

import pytest

from fakes import FakeProvider, make_env
from xb1lib import pool
from xb1lib.errors import ParseError
from xb1lib.provider_westock import parse_universe

CODES = {"600000": "main", "601398": "main", "603444": "main", "605001": "main",
         "000001": "main", "002594": "main", "300750": "gem", "301001": "gem",
         "688981": "star", "689009": "star", "830799": "bj", "430047": "bj"}

RAW = """📈 A股股票（12 只）

| code | name |
| --- | --- |
| sh600000 | 浦发银行 |
| sz000001 | 平安银行 |
| bj830799 | 艾融软件 |
"""


def test_parse_universe_reads_prefixed_codes():
    got = parse_universe(RAW)
    assert [x["code"] for x in got] == ["600000", "000001", "830799"]
    assert got[0]["name"] == "浦发银行"


def test_parse_universe_raises_on_garbage():
    with pytest.raises(ParseError):
        parse_universe("没有表格")


@pytest.fixture
def prov():
    p = FakeProvider(profiles={c: {"name": "x", "listed_date": "2015-01-05"} for c in CODES})
    p.all_codes = list(CODES)
    return p


def test_build_codes_filters_by_board(prov):
    main, total = pool.build_codes(prov, "main")
    assert total == 12
    assert main == sorted(c for c, b in CODES.items() if b == "main")
    assert pool.build_codes(prov, "gem")[0] == ["300750", "301001"]
    assert pool.build_codes(prov, "star")[0] == ["688981", "689009"]
    assert pool.build_codes(prov, "bj")[0] == ["430047", "830799"]
    assert len(pool.build_codes(prov, "all")[0]) == 12


def test_build_codes_rejects_unknown_board(prov):
    with pytest.raises(ValueError):
        pool.build_codes(prov, "nasdaq")


def test_write_read_roundtrip_and_diff(tmp_path):
    f = tmp_path / "pools" / "main.txt"
    f.parent.mkdir()
    pool.write_pool_file(f, ["600000", "000001"], "main", "westock")
    text = f.read_text(encoding="utf-8")
    assert text.startswith("# 主板 A 股代码表（2 只）") and "pool build" in text
    assert pool.read_pool_file(f) == ["600000", "000001"]
    added, removed = pool.diff_codes(["600000", "000001"], ["600000", "300750"])
    assert added == ["300750"] and removed == ["000001"]
    assert pool.read_pool_file(tmp_path / "none.txt") == []


def test_cli_pool_build_writes_file(tmp_path, capsys):
    from test_integration import call, make_home
    home = make_home(tmp_path, codes=["600000"])
    assert call(home, "pool", "build", "--board", "main", provider=prov_factory()) == 0
    out = capsys.readouterr().out
    assert "命中" in out and "已写入" in out
    f = home / "pools" / "main.txt"
    assert f.exists() and "600000" in f.read_text(encoding="utf-8")


def test_cli_pool_build_dry_run_does_not_write(tmp_path, capsys):
    from test_integration import call, make_home
    home = make_home(tmp_path, codes=["600000"])
    assert call(home, "pool", "build", "--board", "gem", "--dry-run", provider=prov_factory()) == 0
    assert "--dry-run" in capsys.readouterr().out
    assert not (home / "pools" / "gem.txt").exists()


def test_cli_pool_build_reports_diff(tmp_path, capsys):
    from test_integration import call, make_home
    home = make_home(tmp_path, codes=["600000"])
    (home / "pools").mkdir(exist_ok=True)
    (home / "pools" / "main.txt").write_text("# old\n600000\n600999\n", encoding="utf-8")
    assert call(home, "pool", "build", "--board", "main", provider=prov_factory()) == 0
    out = capsys.readouterr().out
    assert "新增" in out and "移除" in out and "600999" in out


def prov_factory():
    p = FakeProvider(profiles={c: {"name": "x", "listed_date": "2015-01-05"} for c in CODES})
    p.all_codes = list(CODES)
    return p
