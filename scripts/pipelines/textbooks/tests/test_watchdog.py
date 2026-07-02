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
