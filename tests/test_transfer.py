"""transfer.py：只导出「股票池 + 原始数据」，可导入到新的 XB1_HOME。"""
from __future__ import annotations

import tarfile

import pytest

from fakes import make_kline_df
from xb1lib import transfer
from xb1lib.errors import ConfigError
from xb1lib.paths import Paths

D = "2026-11-03"
POOL = ["600519", "000001", "300750"]


def _cfg():
    return {"universe": {"stocks": POOL, "sectors": [], "files": [], "groups": {}},
            "fetch": {"amount_unit": 1}}


def _home(tmp_path, name="home"):
    paths = Paths(home=tmp_path / name, check=False)
    paths.ensure_dirs()
    return paths


def _seed(paths):
    paths.config.write_text("universe:\n  stocks: [600519, 000001, 300750]\n", encoding="utf-8")
    for c in POOL:
        make_kline_df(c, D, 260).to_parquet(paths.kline(c), index=False)
        (paths.raw / "profile").mkdir(parents=True, exist_ok=True)
        paths.profile(c).write_text('{"code": "%s", "name": "测试"}' % c, encoding="utf-8")


def test_pool_codes_reads_stocks_and_groups(tmp_path):
    cfg = {"universe": {"stocks": ["600519"], "files": [], "sectors": [],
                        "groups": {"g": {"stocks": ["000001"], "files": [], "sectors": []}}}}
    assert transfer.pool_codes(_home(tmp_path), cfg) == ["000001", "600519"]


def test_pool_codes_rejects_sector_pool(tmp_path):
    cfg = {"universe": {"stocks": [], "files": [], "sectors": ["机器人"]}}
    with pytest.raises(Exception):
        transfer.pool_codes(_home(tmp_path), cfg)


def test_export_contains_only_pool_data(tmp_path):
    paths = _home(tmp_path)
    _seed(paths)
    (paths.runs / D).mkdir(parents=True, exist_ok=True)
    (paths.runs / D / "signals.json").write_text("{}", encoding="utf-8")   # 结果不应被打包

    out = tmp_path / "b.tar.gz"
    m = transfer.export_bundle(paths, _cfg(), out)
    assert m["codes"] == 3 and m["kline_files"] == 3 and m["profile_files"] == 3

    with tarfile.open(out) as tf:
        names = tf.getnames()
    assert "config.yaml" in names and transfer.MANIFEST in names
    assert sum(n.endswith(".parquet") for n in names) == 3
    assert not any(n.startswith("data/runs") for n in names)
    assert not any(n.startswith("data/cache") for n in names)
    assert transfer.POOL_TXT.as_posix() in names


def test_import_roundtrip_into_new_home(tmp_path):
    src = _home(tmp_path, "src")
    _seed(src)
    out = tmp_path / "b.tar.gz"
    transfer.export_bundle(src, _cfg(), out)

    dst = _home(tmp_path, "dst")
    r = transfer.import_bundle(dst, out)
    assert r["config"] == "written" and r["copied_files"] >= 6
    for c in POOL:
        assert dst.kline(c).exists() and dst.profile(c).exists()
    assert dst.config.exists() and "600519" in dst.config.read_text(encoding="utf-8")
    assert len((dst.raw / "pool" / "pool.txt").read_text(encoding="utf-8").split()) == 3


def test_import_keeps_existing_config_unless_forced(tmp_path):
    src = _home(tmp_path, "src")
    _seed(src)
    out = tmp_path / "b.tar.gz"
    transfer.export_bundle(src, _cfg(), out)

    dst = _home(tmp_path, "dst")
    dst.config.write_text("universe:\n  stocks: [600519]\n", encoding="utf-8")
    r = transfer.import_bundle(dst, out)
    assert r["config"].startswith("kept")
    assert dst.config.read_text(encoding="utf-8").count("600519") == 1
    assert (dst.config.parent / "config.yaml.from-bundle").exists()

    r2 = transfer.import_bundle(dst, out, force=True)
    assert r2["config"] == "written"


def test_import_rejects_bad_archive(tmp_path):
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(bad, "w:gz") as tf:
        p = tmp_path / "x.txt"
        p.write_text("x", encoding="utf-8")
        tf.add(p, arcname="evil/x.txt")
    with pytest.raises(ConfigError):
        transfer.import_bundle(_home(tmp_path), bad)
