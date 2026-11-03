"""WestockProvider：解析宽容性 / ParseError / 限流识别 / 命令不存在（全部用伪造输出 + monkeypatch subprocess）。

所有输出文本都是“按 PRD 描述猜的”，真实格式见 references/westock-assumptions.md，实机校对后只需改常量。
"""
from __future__ import annotations

import subprocess

import pandas as pd
import pytest

from fakes import make_env
from xb1lib import provider_westock as pw
from xb1lib.constants import KLINE_COLUMNS
from xb1lib.errors import ParseError, RateLimited, SourceUnavailable
from xb1lib.provider_westock import (WestockProvider, parse_md_table, parse_number, parse_number_unit)

KL_HEADER = "| date | open | last | high | low | volume | amount | exchange | change_pct |"
KL_SEP = "|---|---|---|---|---|---|---|---|---|"


def kl_rows(start=1, n=3, base=10.0, amount="1,234,567.5"):
    rows = []
    for i in range(n):
        d = f"2026-01-{start + i:02d}"
        rows.append(f"| {d} | {base:.2f} | {base + i:.2f} | {base + i + 1:.2f} | {base - 1:.2f} | 1,000,0{i}0 | {amount} | 1.5% | {i}.25% |")
    return "\n".join(rows)


def kl_table(**kw):
    return f"{KL_HEADER}\n{KL_SEP}\n{kl_rows(**kw)}"


class Proc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


@pytest.fixture
def env(tmp_path):
    return make_env(tmp_path)


@pytest.fixture
def prov(env, monkeypatch):
    paths, cfg = env
    monkeypatch.setenv("WESTOCK_BIN", "westock")   # 固定命令名，避免测试依赖本机是否装了 westock
    p = WestockProvider(cfg, paths)
    p.calls = []
    p.responder = lambda argv: Proc("")

    def fake_run(argv, **kw):
        p.calls.append((argv, kw))
        r = p.responder(argv)
        if isinstance(r, BaseException):
            raise r
        return r

    monkeypatch.setattr(pw.subprocess, "run", fake_run)
    return p


# ------------------------------------------------------------------ parse_number
@pytest.mark.parametrize("raw,val,explicit", [
    ("1,234.50", 1234.5, False), ("2.5%", 2.5, False), ("-3.1%", -3.1, False), ("+0.5", 0.5, False),
    ("3.2亿", 3.2e8, True), ("1.5万", 15000.0, True), ("12,345元", 12345.0, True), ("100手", 100.0, False),
    ("--", None, False), ("", None, False), ("N/A", None, False), ("1，234", 1234.0, False), ("−2.5", -2.5, False),
])
def test_parse_number_unit(raw, val, explicit):
    v, e = parse_number_unit(raw)
    assert v == val and e is explicit


def test_parse_number_rejects_text():
    with pytest.raises(ValueError):
        parse_number("abc")
    with pytest.raises(ValueError):
        parse_number("12abc")


# ------------------------------------------------------------------ parse_md_table
def test_parse_basic_with_alignment_row():
    df = parse_md_table("| a | b |\n|:---|---:|\n| 1 | 2,500 |\n| 3 | 4 |")
    assert list(df.columns) == ["a", "b"]
    assert df["b"].tolist() == [2500.0, 4.0]


def test_parse_surrounded_by_prose_and_multi_tables():
    text = ("查询成功，共 2 张表\n\n### 表一\n| x | y |\n|---|---|\n| 1 | 2 |\n\n说明：以上为示例\n\n"
            "| m | n |\n|---|---|\n| 5 | 6 |\n\n结束")
    assert parse_md_table(text)["x"].tolist() == [1.0]
    assert parse_md_table(text, required=["m", "n"])["m"].tolist() == [5.0]


def test_parse_no_outer_pipes_and_ansi_and_bold():
    text = "\x1b[32m a | b \x1b[0m\n --- | ---\n **1** | `2`"
    df = parse_md_table(text)
    assert df.iloc[0].tolist() == [1.0, 2.0]


def test_parse_units_and_missing_cells():
    df = parse_md_table("| name | amount | chg |\n|---|---|---|\n| 甲 | 3.2亿 | 1.5% |\n| 乙 | -- | -2% |")
    assert df["name"].tolist() == ["甲", "乙"]
    assert df["amount"].iloc[0] == 3.2e8 and pd.isna(df["amount"].iloc[1])
    assert df["chg"].tolist() == [1.5, -2.0]


def test_parse_keeps_leading_zero_codes_as_string():
    df = parse_md_table("| code | v |\n|---|---|\n| 000001 | 1 |\n| 600519 | 2 |")
    assert df["code"].tolist() == ["000001", "600519"]


def test_parse_short_row_padded_long_row_errors():
    df = parse_md_table("| a | b | c |\n|---|---|---|\n| 1 | 2 |")
    assert pd.isna(df["c"].iloc[0])
    with pytest.raises(ParseError):
        parse_md_table("| a | b |\n|---|---|\n| 1 | 2 | 3 |")


def test_parse_skips_repeated_header_rows():
    df = parse_md_table("| a | b |\n|---|---|\n| 1 | 2 |\n| a | b |\n| 3 | 4 |")
    assert df["a"].tolist() == [1.0, 3.0]


@pytest.mark.parametrize("text", ["", "   \n\n", "没有任何表格，只有一句话", "error: something", "| a | b |\n|---|---|\n",
                                  "| only header |", "|---|---|"])
def test_parse_raises_not_empty(text):
    with pytest.raises(ParseError):
        parse_md_table(text)


def test_parse_required_missing_raises():
    with pytest.raises(ParseError):
        parse_md_table("| a | b |\n|---|---|\n| 1 | 2 |", required=["zzz"])


# ------------------------------------------------------------------ K 线
def test_single_kline_mapping_and_standard_columns(prov):
    prov.responder = lambda argv: Proc(f"K线数据 sh600519\n\n{kl_table()}\n\n共 3 条")
    ok, bad = prov.get_klines(["600519"], 250, "qfq")
    assert bad == {}
    df = ok["600519"]
    assert list(df.columns) == KLINE_COLUMNS
    assert df["date"].tolist() == ["2026-01-01", "2026-01-02", "2026-01-03"]
    assert df["close"].tolist() == [10.0, 11.0, 12.0]          # last -> close
    assert df["turnover"].iloc[0] == 1.5                          # exchange -> turnover
    assert df["pct_chg"].tolist() == [0.25, 1.25, 2.25]           # change_pct -> pct_chg
    assert df["volume"].iloc[0] == 1000000.0                      # 千分位
    assert df["amount"].iloc[0] == 1234567.5                      # amount_unit=1


def test_command_template(prov):
    prov.responder = lambda argv: Proc(kl_table())
    prov.get_klines(["600519", "300124"], 250, "qfq")
    argv = prov.calls[0][0]
    assert argv == ["westock", "kline", "sh600519,sz300124", "--period", "day", "--limit", "250", "--fq", "qfq"]
    assert prov.calls[0][1]["timeout"] > 0                         # subprocess 必须带 timeout


def test_amount_unit_factor_and_explicit_unit(env, monkeypatch):
    paths, cfg = env
    cfg["fetch"]["amount_unit"] = 10000
    p = WestockProvider(cfg, paths)
    text = f"{KL_HEADER}\n{KL_SEP}\n| 2026-01-02 | 1 | 1 | 1 | 1 | 100 | 1.5 | 1 | 1 |\n| 2026-01-05 | 1 | 1 | 1 | 1 | 100 | 2.0亿 | 1 | 1 |"
    monkeypatch.setattr(pw.subprocess, "run", lambda argv, **kw: Proc(text))
    df = p.get_klines(["600519"], 10, "qfq")[0]["600519"]
    assert df["amount"].tolist() == [15000.0, 2.0e8]               # 无单位 × amount_unit；带“亿”不再乘


def test_kline_sorted_dedup_and_date_formats(prov):
    text = (f"{KL_HEADER}\n{KL_SEP}\n| 20260105 | 1 | 2 | 3 | 1 | 10 | 100 | 1 | 1 |\n"
            "| 2026/01/02 | 1 | 5 | 3 | 1 | 10 | 100 | 1 | 1 |\n| 2026-01-02 | 1 | 6 | 3 | 1 | 10 | 100 | 1 | 1 |")
    prov.responder = lambda argv: Proc(text)
    df = prov.get_klines(["600519"], 10, "qfq")[0]["600519"]
    assert df["date"].tolist() == ["2026-01-02", "2026-01-05"]
    assert df["close"].tolist() == [6.0, 2.0]                      # 重复日期保留后出现者


def test_kline_chinese_headers(prov):
    text = "| 日期 | 开盘 | 收盘 | 最高 | 最低 | 成交量 | 成交额 | 换手率 | 涨跌幅 |\n|---|---|---|---|---|---|---|---|---|\n| 2026-01-02 | 1 | 2 | 3 | 1 | 10 | 100 | 1.5 | 2 |"
    prov.responder = lambda argv: Proc(text)
    df = prov.get_klines(["600519"], 10, "qfq")[0]["600519"]
    assert df["close"].iloc[0] == 2.0 and df["pct_chg"].iloc[0] == 2.0


def test_kline_missing_optional_columns_nan_but_missing_required_is_failure(prov):
    text = "| date | open | last | high | low | volume | amount |\n|---|---|---|---|---|---|---|\n| 2026-01-02 | 1 | 2 | 3 | 1 | 10 | 100 |"
    prov.responder = lambda argv: Proc(text)
    df = prov.get_klines(["600519"], 10, "qfq")[0]["600519"]
    assert pd.isna(df["turnover"].iloc[0]) and pd.isna(df["pct_chg"].iloc[0])
    bad_text = "| date | open | last |\n|---|---|---|\n| 2026-01-02 | 1 | 2 |"
    prov.responder = lambda argv: Proc(bad_text)
    ok, bad = prov.get_klines(["600519"], 10, "qfq")
    assert ok == {} and "600519" in bad and bad["600519"].startswith("failed")


def test_batch_split_by_headings_and_unsplittable_is_single_failure(prov):
    text = (f"批量 K 线\n## sh600519\n{kl_table(base=100)}\n\n## sz300124\n{kl_table(base=20)}\n\n"
            "## sz000001\n（暂无数据）\n")
    prov.responder = lambda argv: Proc(text)
    ok, bad = prov.get_klines(["600519", "300124", "000001", "002979"], 250, "qfq")
    assert set(ok) == {"600519", "300124"}
    assert ok["600519"]["close"].iloc[0] == 100.0 and ok["300124"]["close"].iloc[0] == 20.0
    assert set(bad) == {"000001", "002979"}                        # 无表 / 根本没出现 -> 单只失败
    assert all(v.startswith("failed") for v in bad.values())


def test_batch_split_by_code_column(prov):
    hdr = "| code | date | open | last | high | low | volume | amount | exchange | change_pct |"
    sep = "|---|---|---|---|---|---|---|---|---|---|"
    rows = ["| sh600519 | 2026-01-02 | 1 | 2 | 3 | 1 | 10 | 100 | 1 | 1 |",
            "| sz300124 | 2026-01-02 | 1 | 5 | 3 | 1 | 10 | 100 | 1 | 1 |",
            "| sz300124 | 2026-01-05 | 1 | 6 | 3 | 1 | 10 | 100 | 1 | 1 |",
            "| sz999999 | 2026-01-05 | 1 | 6 | 3 | 1 | 10 | 100 | 1 | 1 |"]     # 非请求代码被忽略
    prov.responder = lambda argv: Proc("\n".join([hdr, sep] + rows))
    ok, bad = prov.get_klines(["600519", "300124"], 5, "qfq")
    assert bad == {} and len(ok["300124"]) == 2 and len(ok["600519"]) == 1


def test_batch_split_by_bold_label_line(prov):
    text = f"**sh600519**\n{kl_table(base=100)}\n**sz300124**\n{kl_table(base=20)}"
    prov.responder = lambda argv: Proc(text)
    ok, bad = prov.get_klines(["600519", "300124"], 5, "qfq")
    assert bad == {} and ok["300124"]["close"].iloc[0] == 20.0


def test_bad_row_in_one_stock_only_fails_that_stock(prov):
    broken = kl_table(base=20).replace("2026-01-02", "not-a-date")
    prov.responder = lambda argv: Proc(f"## sh600519\n{kl_table()}\n## sz300124\n{broken}")
    ok, bad = prov.get_klines(["600519", "300124"], 5, "qfq")
    assert set(ok) == {"600519"} and "300124" in bad and "parse_error" in bad["300124"]


@pytest.mark.parametrize("text", ["", "没有数据", "| a | b |\n|---|---|\n| 1 | 2 |", "| date | x |\n|---|---|\n"])
def test_kline_unparseable_raises_parse_error(prov, text):
    prov.responder = lambda argv: Proc(text)
    with pytest.raises(ParseError):
        prov.get_klines(["600519"], 10, "qfq")


def test_kline_batch_over_20_rejected(prov):
    with pytest.raises(ValueError):
        prov.get_klines([f"{600000 + i}" for i in range(21)], 10, "qfq")
    assert prov.calls == []


# ------------------------------------------------------------------ 限流 / 命令异常
@pytest.mark.parametrize("where", ["stdout", "stderr"])
def test_rate_limit_detected(prov, where):
    kw = {where: "ERROR LOCAL_RATE_LIMITED, retry later"}
    prov.responder = lambda argv: Proc(returncode=1, **kw)
    with pytest.raises(RateLimited):
        prov.get_klines(["600519"], 10, "qfq")


def test_rate_limit_detected_even_with_rc0_and_table(prov):
    prov.responder = lambda argv: Proc(kl_table() + "\nLOCAL_RATE_LIMITED")
    with pytest.raises(RateLimited):
        prov.get_klines(["600519"], 10, "qfq")


def test_command_not_found_is_source_unavailable_mocked(prov):
    prov.responder = lambda argv: FileNotFoundError(2, "No such file")
    for call in (lambda: prov.version(), lambda: prov.trade_calendar(), lambda: prov.get_klines(["600519"], 1, "qfq")):
        with pytest.raises(SourceUnavailable):
            call()


def test_command_not_found_real_subprocess(env):
    paths, cfg = env
    cfg["fetch"]["command"] = "xb1-definitely-not-installed-zzz"
    with pytest.raises(SourceUnavailable):
        WestockProvider(cfg, paths).version()


def test_timeout_is_source_unavailable(prov):
    prov.responder = lambda argv: subprocess.TimeoutExpired(argv, 60)
    with pytest.raises(SourceUnavailable):
        prov.trade_calendar()


def test_nonzero_exit_without_table_is_source_unavailable(prov):
    prov.responder = lambda argv: Proc("", "unauthorized, please login", returncode=2)
    with pytest.raises(SourceUnavailable):
        prov.trade_calendar()


def test_log_written_without_credentials(prov, env):
    paths, _ = env
    prov.responder = lambda argv: Proc("LOCAL_RATE_LIMITED")
    with pytest.raises(RateLimited):
        prov.get_klines(["600519"], 10, "qfq")
    prov.responder = lambda argv: Proc(kl_table())
    prov.get_klines(["600519"], 10, "qfq")
    logs = list(paths.logs.glob("westock-*.log"))
    assert len(logs) == 1
    lines = logs[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and "rate_limited=1" in lines[0] and "rate_limited=0" in lines[1]
    assert "kline" in lines[0] and "elapsed=" in lines[0]


# ------------------------------------------------------------------ 其它命令
def test_version(prov):
    # 实测输出（2026-11-03，沙箱 linux/arm64）
    prov.responder = lambda argv: Proc('{"version":"0.0.9","channel":"wbgeneral","tier":20,"auth":true,'
                                       '"rate_limit_source":"remote","goos":"linux","goarch":"arm64"}\n')
    assert prov.version() == "v0.0.9"
    assert prov.calls[0][0] == ["westock", "version", "--json"]
    prov.responder = lambda argv: Proc("\nwestock v0.0.9\n")       # 非 JSON 兜底：取首行
    assert prov.version() == "westock v0.0.9"
    prov.responder = lambda argv: Proc("  \n")
    with pytest.raises(ParseError):
        prov.version()


def test_search_sector(prov):
    prov.responder = lambda argv: Proc("找到 2 个板块\n| 代码 | 名称 | 类型 |\n|---|---|---|\n| pt02GN2238 | 人形机器人 | 概念 |\n| pt01801093 | 汽车零部件 | 行业 |")
    assert prov.search_sector("人形机器人") == [{"code": "pt02GN2238", "name": "人形机器人"},
                                              {"code": "pt01801093", "name": "汽车零部件"}]
    assert prov.calls[0][0] == ["westock", "search", "人形机器人", "--type", "sector"]


def test_search_sector_no_result_is_empty_but_garbage_is_error(prov):
    prov.responder = lambda argv: Proc("| code | name |\n|---|---|\n")
    assert prov.search_sector("不存在") == []
    prov.responder = lambda argv: Proc("something went wrong")
    with pytest.raises(ParseError):
        prov.search_sector("x")


def test_sector_constituents(prov):
    text = "成分股 468 只\n| 代码 | 名称 | 现价 |\n|---|---|---|\n| sz300124 | 汇川技术 | 70.1 |\n| sh601100 | 恒立液压 | 60 |\n| 600519.SH | 贵州茅台 | 1400 |\n| sz300124 | 汇川技术 | 70.1 |"
    prov.responder = lambda argv: Proc(text)
    out = prov.sector_constituents("pt02GN2238")
    assert out == [{"code": "300124", "name": "汇川技术"}, {"code": "601100", "name": "恒立液压"},
                   {"code": "600519", "name": "贵州茅台"}]
    assert prov.calls[0][0] == ["westock", "sector", "constituent", "pt02GN2238"]
    prov.responder = lambda argv: Proc("| 代码 | 名称 |\n|---|---|\n")
    with pytest.raises(ParseError):
        prov.sector_constituents("pt0")


def test_trade_calendar_table_and_flag(prov):
    prov.responder = lambda argv: Proc("| date | is_trading |\n|---|---|\n| 2026-01-02 | 1 |\n| 2026-01-03 | 0 |\n| 2026-01-05 | 1 |")
    assert prov.trade_calendar() == ["2026-01-02", "2026-01-05"]
    prov.responder = lambda argv: Proc("| 交易日 |\n|---|\n| 20260105 |\n| 20260102 |")
    assert prov.trade_calendar() == ["2026-01-02", "2026-01-05"]


def test_trade_calendar_bare_dates_fallback_and_garbage(prov):
    prov.responder = lambda argv: Proc("2026-01-02, 2026-01-05\n2026-01-06")
    assert prov.trade_calendar() == ["2026-01-02", "2026-01-05", "2026-01-06"]
    prov.responder = lambda argv: Proc("nothing here")
    with pytest.raises(ParseError):
        prov.trade_calendar()


REAL_CAL_2026 = """### A股交易日历（2026-01-01 ~ 2026-12-31）

| date | isLastTradeDayInMonth | isLastTradeDayInQuarter | isLastTradeDayInWeek | isLastTradeDayInYear | isTrading |
| --- | --- | --- | --- | --- | --- |
| 2026-10-30 | 是 | 否 | 是 | 否 | 是 |
| 2026-11-02 | 否 | 否 | 否 | 否 | 是 |
| 2026-11-03 | 否 | 否 | 否 | 否 | 是 |
"""


def test_trade_calendar_real_format_per_year(prov):
    """实测格式：按年请求（--trading-only --year Y --limit 9999）；中文 是/否；某年无数据跳过。"""
    def responder(argv):
        if "2026" in argv:
            return Proc(REAL_CAL_2026)
        return Proc("### A股交易日历\n\n暂无数据\n")         # 其他年份解析不出 -> 跳过
    prov.responder = responder
    assert prov.trade_calendar() == ["2026-10-30", "2026-11-02", "2026-11-03"]
    argv = next(a for a, _ in prov.calls if "2026" in a)
    assert argv == ["westock", "trade-calendar", "--trading-only", "--year", "2026", "--limit", "9999"]
    assert len(prov.calls) == pw.CMD_TRADE_CALENDAR_YEARS + 1


def test_trade_calendar_source_error_propagates(prov):
    """数据源错误不能被逐年循环吞掉。"""
    prov.responder = lambda argv: Proc("LOCAL_RATE_LIMITED")
    with pytest.raises(RateLimited):
        prov.trade_calendar()


def test_profiles_table_with_code_column(prov):
    text = "| code | name | listedDate |\n|---|---|---|\n| sz300124 | 汇川技术 | 2010-09-28 |\n| sh601100 | 恒立液压 | 20110128 |"
    prov.responder = lambda argv: Proc(text)
    out = prov.get_profiles(["300124", "601100", "688017"])
    assert out["300124"] == {"name": "汇川技术", "listed_date": "2010-09-28"}
    assert out["601100"]["listed_date"] == "2011-01-28"
    assert "688017" not in out
    assert prov.calls[0][0] == ["westock", "profile", "sz300124,sh601100,sh688017"]


def test_profiles_keyvalue_single_and_headed_sections(prov):
    prov.responder = lambda argv: Proc("| 字段 | 值 |\n|---|---|\n| name | 贵州茅台 |\n| listedDate | 2001-08-27 |")
    assert prov.get_profiles(["600519"]) == {"600519": {"name": "贵州茅台", "listed_date": "2001-08-27"}}
    prov.responder = lambda argv: Proc("## sh600519\nname: 贵州茅台\nlistedDate: 2001-08-27\n## sz300124\nname: 汇川技术\n")
    out = prov.get_profiles(["600519", "300124"])
    assert out["600519"]["listed_date"] == "2001-08-27"
    assert out["300124"] == {"name": "汇川技术", "listed_date": None}      # 缺 listedDate -> None（不抛）


def test_profiles_unparseable_raises(prov):
    prov.responder = lambda argv: Proc("no profile")
    with pytest.raises(ParseError):
        prov.get_profiles(["600519"])


def test_profiles_auto_split_over_10(prov, monkeypatch):
    """实测：profile 一次最多 10 个代码。超过时 provider 自动分批，调用方无感。"""
    monkeypatch.setattr(pw.time, "sleep", lambda s: None)
    codes = [f"6000{i:02d}" for i in range(25)]

    def responder(argv):
        asked = argv[2].split(",")
        assert len(asked) <= 10, "单次 profile 超过 10 个代码，westock 会报错"
        rows = "\n".join(f"| {a} | 股{a[-6:]} | 2010-01-0{1 + i % 9} |" for i, a in enumerate(asked))
        return Proc(f"| code | name | listedDate |\n|---|---|---|\n{rows}")
    prov.responder = responder
    out = prov.get_profiles(codes)
    assert sorted(out) == sorted(codes)
    assert [len(a[0][2].split(",")) for a in prov.calls] == [10, 10, 5]


def test_profile_extra_fields_parsed_from_real_format(prov):
    """实测表头：code|name|listedDate|business|website|chairman|industry|sector|...；只取 business/industry/sector。"""
    text = ("| code | name | listedDate | business | website | chairman | industry | sector | issuePrice |\n"
            "|---|---|---|---|---|---|---|---|---|\n"
            "| sz300124 | 汇川技术 | 2010-09-28 | 工业自动化核心部件 | http://x | 朱兴明 | 机械设备 | 机械设备 | 71.88 |")
    prov.responder = lambda argv: Proc(text)
    r = prov.get_profiles(["300124"])["300124"]
    assert r == {"name": "汇川技术", "listed_date": "2010-09-28", "business": "工业自动化核心部件",
                 "industry": "机械设备", "sector": "机械设备"}


SERVER_LIMITED = ("\n[Batch] 状态: error | 总数: 10 | 成功: 0 | 失败: 10\n失败列表:\n"
                  "  sz300319: [SERVER_RATE_LIMITED] 查询公司简况失败：[code=1620053006] error_type=2 suggest=2 msg=服务限频\n\n数据为空")


def test_server_rate_limited_is_rate_limited_not_parse_error(prov):
    """2026-11-03 实测：profile 连续调用会触发服务端限流 SERVER_RATE_LIMITED（退出码 0、无表格）。
    必须按限流处理（等待后重试），而不是 ParseError / 静默跳过。"""
    prov.responder = lambda argv: Proc(SERVER_LIMITED)
    with pytest.raises(RateLimited) as e:
        prov.get_profiles(["300319"])
    assert "SERVER_RATE_LIMITED" in str(e.value)
    with pytest.raises(RateLimited):
        prov.get_klines(["300319"], 10, "qfq")
    assert "rate_limited=1" in next(prov.paths.logs.glob("westock-*.log")).read_text(encoding="utf-8")
