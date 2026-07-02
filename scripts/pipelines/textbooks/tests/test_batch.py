import pytest

from scripts.pipelines.textbooks import batch as bp


def test_discover_dir_and_file_mixed_dedup(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    a = d / "A.pdf"
    a.write_bytes(b"%PDF-1.4")
    b = d / "B.pdf"
    b.write_bytes(b"%PDF-1.4")
    result = bp.discover([str(d), str(a)])   # 目录+文件混用,a 只应出现一次
    assert result == [a, b]


def test_discover_skips_non_pdf(tmp_path, capsys):
    d = tmp_path / "src"
    d.mkdir()
    (d / "notes.txt").write_text("x", encoding="utf-8")
    result = bp.discover([str(d / "notes.txt")])
    assert result == []
    assert "跳过" in capsys.readouterr().err


def test_discover_cross_dir_stem_collision_raises(tmp_path):
    d1 = tmp_path / "s1"
    d1.mkdir()
    d2 = tmp_path / "s2"
    d2.mkdir()
    (d1 / "A.pdf").write_bytes(b"%PDF-1.4")
    (d2 / "A.pdf").write_bytes(b"%PDF-1.4")
    with pytest.raises(ValueError, match="跨目录同名"):
        bp.discover([str(d1), str(d2)])
