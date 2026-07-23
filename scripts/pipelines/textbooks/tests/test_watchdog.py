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


def test_suspect_exit_stops_without_restart():
    calls = []

    def runner(argv):
        calls.append(argv)
        return 2

    rc = wd.run_until_done(["--src", "x.pdf"], max_restarts=5, runner=runner)

    assert rc == 2
    assert len(calls) == 1


def test_failed_restart_can_end_as_suspect_without_another_restart():
    seq = [1, 2, 0]

    def runner(argv):
        return seq.pop(0)

    rc = wd.run_until_done(["--src", "x.pdf"], max_restarts=5, runner=runner)

    assert rc == 2
    assert seq == [0]


def test_unknown_nonzero_exit_restarts_then_can_succeed():
    seq = [7, -9, 0]

    def runner(argv):
        return seq.pop(0)

    rc = wd.run_until_done(["--src", "x.pdf"], max_restarts=5, runner=runner)

    assert rc == 0
    assert seq == []


def test_unknown_nonzero_exit_exhaustion_maps_to_failed():
    calls = []

    def runner(argv):
        calls.append(argv)
        return 17

    rc = wd.run_until_done(["--src", "x.pdf"], max_restarts=2, runner=runner)

    assert rc == 1
    assert len(calls) == 3


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


def test_main_forwards_work_dir_to_convert(monkeypatch):
    captured = {}
    def fake_run_until_done(argv, max_restarts):
        captured["argv"] = argv
        return 0
    monkeypatch.setattr(wd, "run_until_done", fake_run_until_done)
    monkeypatch.setattr("sys.argv",
                        ["watchdog.py", "--src", "x.pdf", "--work-dir", "/scratch"])
    with pytest.raises(SystemExit):
        wd.main()
    argv = captured["argv"]
    assert "--work-dir" in argv
    assert argv[argv.index("--work-dir") + 1] == "/scratch"


def test_main_omits_work_dir_by_default(monkeypatch):
    captured = {}
    def fake_run_until_done(argv, max_restarts):
        captured["argv"] = argv
        return 0
    monkeypatch.setattr(wd, "run_until_done", fake_run_until_done)
    monkeypatch.setattr("sys.argv", ["watchdog.py", "--src", "x.pdf"])
    with pytest.raises(SystemExit):
        wd.main()
    assert "--work-dir" not in captured["argv"]


def test_main_forwards_allow_sleep_to_convert(monkeypatch):
    captured = {}
    def fake_run_until_done(argv, max_restarts):
        captured["argv"] = argv
        return 0
    monkeypatch.setattr(wd, "run_until_done", fake_run_until_done)
    monkeypatch.setattr("sys.argv", ["watchdog.py", "--src", "x.pdf", "--allow-sleep"])
    with pytest.raises(SystemExit):
        wd.main()
    assert "--allow-sleep" in captured["argv"]


def test_main_forwards_force_ocr_and_rest_schedule(monkeypatch):
    captured = {}

    def fake_run_until_done(argv, max_restarts):
        captured["argv"] = argv
        return 0

    monkeypatch.setattr(wd, "run_until_done", fake_run_until_done)
    monkeypatch.setattr("sys.argv", [
        "watchdog.py", "--src", "x.pdf", "--force-ocr",
        "--work-hours", "6", "--rest-minutes", "40",
    ])

    with pytest.raises(SystemExit) as exc:
        wd.main()

    assert exc.value.code == 0
    argv = captured["argv"]
    assert "--force-ocr" in argv
    assert argv[argv.index("--work-hours") + 1] == "6.0"
    assert argv[argv.index("--rest-minutes") + 1] == "40.0"


def test_main_forwards_born_digital_mode_to_convert(monkeypatch):
    captured = {}
    def fake_run_until_done(argv, max_restarts):
        captured["argv"] = argv
        return 0
    monkeypatch.setattr(wd, "run_until_done", fake_run_until_done)
    monkeypatch.setattr("sys.argv",
                        ["watchdog.py", "--src", "x.pdf", "--born-digital-mode", "hybrid"])
    with pytest.raises(SystemExit):
        wd.main()
    argv = captured["argv"]
    assert "--born-digital-mode" in argv
    assert argv[argv.index("--born-digital-mode") + 1] == "hybrid"


def test_main_born_digital_mode_defaults_to_hybrid(monkeypatch):
    captured = {}
    def fake_run_until_done(argv, max_restarts):
        captured["argv"] = argv
        return 0
    monkeypatch.setattr(wd, "run_until_done", fake_run_until_done)
    monkeypatch.setattr("sys.argv", ["watchdog.py", "--src", "x.pdf"])
    with pytest.raises(SystemExit):
        wd.main()
    argv = captured["argv"]
    assert argv[argv.index("--born-digital-mode") + 1] == "hybrid"


def test_main_rejects_invalid_born_digital_mode(monkeypatch):
    monkeypatch.setattr("sys.argv",
                        ["watchdog.py", "--src", "x.pdf", "--born-digital-mode", "bogus"])
    with pytest.raises(SystemExit) as exc:
        wd.main()
    assert exc.value.code != 0


# ---------------------------------------------------------------------------
# Task B(2026-07-17 所有者批准):--formula-repair 三入口透传之一——watchdog
# 只做 argv 透传给 convert.py 子进程,不做任何编排本体(镜像 --born-digital-mode
# 的既有透传模式)。
# ---------------------------------------------------------------------------

def test_main_forwards_formula_repair_to_convert(monkeypatch):
    captured = {}
    def fake_run_until_done(argv, max_restarts):
        captured["argv"] = argv
        return 0
    monkeypatch.setattr(wd, "run_until_done", fake_run_until_done)
    monkeypatch.setattr("sys.argv",
                        ["watchdog.py", "--src", "x.pdf", "--formula-repair", "agents"])
    with pytest.raises(SystemExit):
        wd.main()
    argv = captured["argv"]
    assert "--formula-repair" in argv
    assert argv[argv.index("--formula-repair") + 1] == "agents"


def test_main_forwards_agents_apply_to_convert(monkeypatch):
    captured = {}
    def fake_run_until_done(argv, max_restarts):
        captured["argv"] = argv
        return 0
    monkeypatch.setattr(wd, "run_until_done", fake_run_until_done)
    monkeypatch.setattr("sys.argv", [
        "watchdog.py", "--src", "x.pdf", "--formula-repair", "agents-apply",
    ])
    with pytest.raises(SystemExit) as exc:
        wd.main()
    assert exc.value.code == 0
    argv = captured["argv"]
    assert argv[argv.index("--formula-repair") + 1] == "agents-apply"


def test_main_auto_forwards_unified_policy_to_convert(monkeypatch):
    captured = {}
    def fake_run_until_done(argv, max_restarts):
        captured["argv"] = argv
        return 0
    monkeypatch.setattr(wd, "run_until_done", fake_run_until_done)
    monkeypatch.setattr("sys.argv", ["watchdog.py", "--src", "x.pdf"])
    with pytest.raises(SystemExit):
        wd.main()
    argv = captured["argv"]
    assert argv[argv.index("--repair") + 1] == "auto"
    assert argv[argv.index("--repair-workers") + 1] == "4"
    assert argv[argv.index("--repair-max-rounds") + 1] == "2"
    assert "--formula-repair" not in argv
    assert "--quality-repair" not in argv
    assert "--quality-agent" not in argv


def test_main_auto_with_explicit_agent_forwards_unified_agent(monkeypatch):
    captured = {}

    def fake_run_until_done(argv, max_restarts):
        captured["argv"] = argv
        return 0

    monkeypatch.setattr(wd, "run_until_done", fake_run_until_done)
    monkeypatch.setattr("sys.argv", [
        "watchdog.py", "--src", "x.pdf",
        "--repair-agent", "codex:gpt-5.6-sol:high",
    ])

    with pytest.raises(SystemExit) as exc:
        wd.main()
    assert exc.value.code == 0
    argv = captured["argv"]
    assert argv[argv.index("--repair") + 1] == "auto"
    assert argv[argv.index("--repair-agent") + 1] == "codex:gpt-5.6-sol:high"
    assert "--formula-repair" not in argv
    assert "--quality-repair" not in argv
    assert "--quality-agent" not in argv


def test_main_repair_off_with_legacy_formula_override_is_stage_local(monkeypatch):
    captured = {}

    def fake_run_until_done(argv, max_restarts):
        captured["argv"] = argv
        return 0

    monkeypatch.setattr(wd, "run_until_done", fake_run_until_done)
    monkeypatch.setattr("sys.argv", [
        "watchdog.py", "--src", "x.pdf", "--repair", "off",
        "--formula-repair", "deterministic",
    ])

    with pytest.raises(SystemExit) as exc:
        wd.main()
    assert exc.value.code == 0
    argv = captured["argv"]
    assert argv[argv.index("--repair") + 1] == "off"
    assert argv[argv.index("--formula-repair") + 1] == "deterministic"
    assert "--quality-repair" not in argv


def test_main_rejects_invalid_formula_repair(monkeypatch):
    monkeypatch.setattr("sys.argv",
                        ["watchdog.py", "--src", "x.pdf", "--formula-repair", "bogus"])
    with pytest.raises(SystemExit) as exc:
        wd.main()
    assert exc.value.code != 0


def test_main_forwards_quality_configuration_to_convert(monkeypatch):
    captured = {}

    def fake_run_until_done(argv, max_restarts):
        captured["argv"] = argv
        return 0

    monkeypatch.setattr(wd, "run_until_done", fake_run_until_done)
    monkeypatch.setattr("sys.argv", [
        "watchdog.py", "--src", "x.pdf", "--quality-repair", "propose",
        "--quality-agent", "codex:gpt-5.6-sol:high",
        "--quality-agent", "gemini:m:medium", "--quality-learn", "package",
    ])
    with pytest.raises(SystemExit) as exc:
        wd.main()
    assert exc.value.code == 0
    argv = captured["argv"]
    assert argv[argv.index("--quality-repair") + 1] == "propose"
    assert argv.count("--quality-agent") == 2
    assert argv[argv.index("--quality-learn") + 1] == "package"
