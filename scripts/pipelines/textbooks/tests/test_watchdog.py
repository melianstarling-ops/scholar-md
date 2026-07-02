import pytest
from scripts.pipelines.textbooks import watchdog as wd


def test_stops_on_success_first_try():
    calls = []
    def runner(argv):
        calls.append(argv)
        return 0
    rc = wd.run_until_done(["--src", "x.pdf"], max_restarts=5, runner=runner)
    assert rc == 0
    assert len(calls) == 1          # 一次成功,不重启


def test_restarts_until_success():
    seq = [1, 1, 0]                 # 崩两次,第三次成功
    def runner(argv):
        return seq.pop(0)
    rc = wd.run_until_done(["--src", "x.pdf"], max_restarts=5, runner=runner)
    assert rc == 0


def test_gives_up_over_max_restarts():
    def runner(argv):
        return 1                    # 永远崩
    rc = wd.run_until_done(["--src", "x.pdf"], max_restarts=3, runner=runner)
    assert rc == 1                  # 兜底放弃


def test_counts_restarts_not_first_run():
    calls = []
    def runner(argv):
        calls.append(1)
        return 1
    wd.run_until_done(["--src", "x.pdf"], max_restarts=3, runner=runner)
    # 首跑 + 3 次重启 = 4 次调用
    assert len(calls) == 4


def test_main_forwards_no_selfcheck_json_flag(monkeypatch):
    captured = {}
    def fake_run_until_done(argv, max_restarts):
        captured["argv"] = argv
        return 0
    monkeypatch.setattr(wd, "run_until_done", fake_run_until_done)
    monkeypatch.setattr("sys.argv", ["watchdog.py", "--src", "x.pdf", "--no-selfcheck-json"])
    with pytest.raises(SystemExit) as exc:
        wd.main()
    assert exc.value.code == 0
    assert "--no-selfcheck-json" in captured["argv"]


def test_main_omits_no_selfcheck_json_flag_by_default(monkeypatch):
    captured = {}
    def fake_run_until_done(argv, max_restarts):
        captured["argv"] = argv
        return 0
    monkeypatch.setattr(wd, "run_until_done", fake_run_until_done)
    monkeypatch.setattr("sys.argv", ["watchdog.py", "--src", "x.pdf"])
    with pytest.raises(SystemExit):
        wd.main()
    assert "--no-selfcheck-json" not in captured["argv"]
