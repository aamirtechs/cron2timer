"""Tests for cron2timer. Run with: python3 -m pytest tests/ -q"""

import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cron2timer
from cron2timer import (
    CronError, CronJob, check_text, convert_jobs, cron_to_oncalendars,
    main, parse_crontab, render_service, render_timer, sanitize_name,
    validate_field,
)


def parse_ok(text):
    jobs, errors = parse_crontab(text)
    assert errors == [], "unexpected errors: %s" % errors
    return jobs


# ---------- parsing ----------

def test_parse_basic_five_field():
    jobs = parse_ok("*/15 * * * * /usr/bin/backup.sh\n")
    assert len(jobs) == 1
    assert jobs[0].fields == ("*/15", "*", "*", "*", "*")
    assert jobs[0].command == "/usr/bin/backup.sh"
    assert jobs[0].lineno == 1


def test_parse_ignores_comments_and_blanks():
    jobs = parse_ok("# a comment\n\n   \n#another\n0 9 * * * echo hi\n")
    assert len(jobs) == 1
    assert jobs[0].command == "echo hi"
    assert jobs[0].lineno == 5


def test_parse_env_assignments():
    jobs = parse_ok("MAILTO=ops@example.com\nPATH=/usr/bin:/bin\n0 9 * * * run.sh\n")
    assert len(jobs) == 1
    assert jobs[0].env == {"MAILTO": "ops@example.com",
                          "PATH": "/usr/bin:/bin"}


def test_parse_env_strips_quotes():
    jobs = parse_ok('GREETING="hello world"\n0 9 * * * run.sh\n')
    assert jobs[0].env == {"GREETING": "hello world"}


def test_parse_env_applies_to_later_jobs_only():
    jobs = parse_ok("0 9 * * * first.sh\nFOO=bar\n0 10 * * * second.sh\n")
    assert jobs[0].env == {}
    assert jobs[1].env == {"FOO": "bar"}


def test_parse_command_with_spaces_and_flags():
    jobs = parse_ok('0 0 * * * /opt/app/sync.py --full --dest "/data/back ups"\n')
    assert jobs[0].command == '/opt/app/sync.py --full --dest "/data/back ups"'


def test_parse_shortcuts():
    for shortcut in ("@yearly", "@annually", "@monthly", "@weekly",
                     "@daily", "@midnight", "@hourly"):
        jobs = parse_ok("%s /bin/doit\n" % shortcut)
        assert len(jobs) == 1
        assert jobs[0].fields is not None
        assert jobs[0].command == "/bin/doit"


def test_parse_reboot():
    jobs = parse_ok("@reboot /opt/app/start.sh\n")
    assert len(jobs) == 1
    assert jobs[0].reboot is True
    assert jobs[0].fields is None


def test_parse_name_fields():
    jobs = parse_ok("0 9 * JAN MON /bin/x\n")
    assert jobs[0].fields[3] == "JAN"
    assert jobs[0].fields[4] == "MON"


def test_parse_error_too_few_fields():
    jobs, errors = parse_crontab("* * * echo hi\n")
    assert jobs == []
    assert len(errors) == 1
    assert errors[0].lineno == 1
    assert "5 schedule fields" in errors[0].message


def test_parse_error_unknown_shortcut():
    jobs, errors = parse_crontab("@sometimes /bin/x\n")
    assert jobs == []
    assert len(errors) == 1
    assert "unknown shortcut" in errors[0].message


def test_parse_error_shortcut_without_command():
    jobs, errors = parse_crontab("@daily\n")
    assert jobs == []
    assert len(errors) == 1


def test_parse_error_bad_minute_range():
    jobs, errors = parse_crontab("61 * * * * /bin/x\n")
    assert jobs == []
    assert len(errors) == 1
    assert "minute" in errors[0].message


def test_parse_error_bad_month_name():
    jobs, errors = parse_crontab("0 0 * FOO * /bin/x\n")
    assert jobs == []
    assert len(errors) == 1
    assert "month" in errors[0].message


def test_parse_error_reversed_range():
    jobs, errors = parse_crontab("0 10-9 * * * /bin/x\n")
    assert jobs == []
    assert len(errors) == 1


def test_parse_error_zero_step():
    jobs, errors = parse_crontab("0 */0 * * * /bin/x\n")
    assert jobs == []
    assert len(errors) == 1


def test_parse_reports_multiple_errors_with_line_numbers():
    jobs, errors = parse_crontab("0 0 * * * /bin/ok\n99 * * * * /bin/bad1\n"
                                 "0 0 * * * /bin/ok2\n* 25 * * * /bin/bad2\n")
    assert len(jobs) == 2
    assert [e.lineno for e in errors] == [2, 4]


# ---------- field validation ----------

def test_validate_field_lists_ranges_steps():
    for field in ("*", "*/15", "0", "0,15,30,45", "9-17", "9-17/2", "0/20"):
        validate_field(field, "minute")
    validate_field("1-5", "dow")
    validate_field("jan,mar,dec", "month")


def test_validate_field_rejects_garbage():
    for kind in ("minute", "hour", "dom", "month", "dow"):
        with pytest.raises(ValueError):
            validate_field("abc!", kind)


# ---------- OnCalendar conversion ----------

def test_every_fifteen_minutes():
    assert cron_to_oncalendars(("*/15", "*", "*", "*", "*")) == [
        "*-*-* *:0/15:00"]


def test_every_minute():
    assert cron_to_oncalendars(("*", "*", "*", "*", "*")) == ["*-*-* *:*:00"]


def test_daily_midnight():
    assert cron_to_oncalendars(("0", "0", "*", "*", "*")) == [
        "*-*-* 00:00:00"]


def test_hourly():
    assert cron_to_oncalendars(("0", "*", "*", "*", "*")) == ["*-*-* *:00:00"]


def test_weekday_range_names():
    assert cron_to_oncalendars(("30", "9", "*", "*", "1-5")) == [
        "Mon..Fri *-*-* 09:30:00"]


def test_dow_zero_and_seven_are_sunday():
    assert cron_to_oncalendars(("0", "0", "*", "*", "0")) == [
        "Sun *-*-* 00:00:00"]
    assert cron_to_oncalendars(("0", "0", "*", "*", "7")) == [
        "Sun *-*-* 00:00:00"]


def test_dow_names_case_insensitive():
    assert cron_to_oncalendars(("0", "0", "*", "*", "mon,wed,FRI")) == [
        "Mon,Wed,Fri *-*-* 00:00:00"]


def test_dow_step_expands_to_names():
    assert cron_to_oncalendars(("0", "0", "*", "*", "*/2")) == [
        "Sun,Tue,Thu,Sat *-*-* 00:00:00"]


def test_month_names_become_numbers():
    assert cron_to_oncalendars(("0", "0", "1", "jan", "*")) == [
        "*-1-1 00:00:00"]


def test_month_step_uses_one_based_start():
    assert cron_to_oncalendars(("0", "0", "1", "*/3", "*")) == [
        "*-1/3-1 00:00:00"]


def test_hour_range_uses_dotdot():
    assert cron_to_oncalendars(("0", "9-17", "*", "*", "*")) == [
        "*-*-* 9..17:00:00"]


def test_dom_and_dow_both_restricted_splits_into_two():
    cals = cron_to_oncalendars(("0", "0", "1", "*", "0"))
    assert cals == ["*-*-1 00:00:00", "Sun *-*-* 00:00:00"]


def test_shortcut_mappings():
    assert cron_to_oncalendars(("0", "0", "1", "1", "*")) == ["*-1-1 00:00:00"]
    assert cron_to_oncalendars(("0", "0", "*", "*", "0")) == [
        "Sun *-*-* 00:00:00"]
    jobs = parse_ok("@monthly /bin/x\n@weekly /bin/y\n")
    convert_jobs(jobs)
    assert jobs[0].oncalendars == ["*-*-1 00:00:00"]
    assert jobs[1].oncalendars == ["Sun *-*-* 00:00:00"]


def test_generated_calendars_validate_with_systemd_analyze():
    analyzer = shutil.which("systemd-analyze")
    if analyzer is None:
        pytest.skip("systemd-analyze not available")
    samples = [
        "*/15 * * * *", "* * * * *", "0 0 * * *", "0 * * * *",
        "30 9 * * 1-5", "0 0 * * 0", "0 0 * * 7", "0 0 * JAN MON",
        "5 4 * * sun", "0 0 1 */3 *", "0 9-17 * * *", "15 14 1 * *",
        "0 22 * * 1-5", "*/20 * * * *", "0 0 * * mon,wed,fri",
        "0 0 * * */2", "0 12 * * mon-fri", "0 0 29 2 *",
    ]
    bad = []
    for sample in samples:
        jobs = parse_ok("%s /bin/x\n" % sample)
        convert_jobs(jobs)
        for cal in jobs[0].oncalendars:
            proc = subprocess.run([analyzer, "calendar", cal],
                                  capture_output=True, text=True)
            if proc.returncode != 0:
                bad.append((sample, cal))
    assert bad == [], "invalid OnCalendar expressions: %s" % bad


# ---------- unit naming ----------

def test_name_from_program_basename():
    assert sanitize_name("/usr/local/bin/backup-db.sh --full", set()) == \
        "backup-db"


def test_name_skips_interpreter():
    assert sanitize_name("python3 /opt/app/sync.py --fast", set()) == "sync"


def test_name_sanitizes_weird_characters():
    name = sanitize_name("/opt/my_app/do-thing!!!.sh", set())
    assert name == "do-thing"


def test_name_dedupes():
    used = set()
    assert sanitize_name("/bin/backup.sh", used) == "backup"
    assert sanitize_name("/usr/bin/backup.sh", used) == "backup-2"
    assert sanitize_name("/sbin/backup.sh", used) == "backup-3"


# ---------- rendering ----------

def test_service_unit_contents():
    jobs = parse_ok("MAILTO=a@b.c\n*/15 * * * * /usr/bin/backup.sh --full\n")
    convert_jobs(jobs)
    text = render_service(jobs[0])
    assert "[Service]" in text
    assert "Type=oneshot" in text
    assert 'Environment="MAILTO=a@b.c"' in text
    assert "ExecStart=/bin/sh -c '/usr/bin/backup.sh --full'" in text
    assert "# Original cron line:" in text
    assert jobs[0].name + ".service" not in text  # name lives in filename


def test_service_quotes_single_quotes_in_command():
    jobs = parse_ok("0 0 * * * echo 'hi'\n")
    convert_jobs(jobs)
    text = render_service(jobs[0])
    assert "echo '\\''hi'\\''" in text


def test_env_value_with_quotes_is_escaped():
    jobs = parse_ok('MSG=say "hi"\n0 0 * * * /bin/x\n')
    convert_jobs(jobs)
    text = render_service(jobs[0])
    assert 'Environment="MSG=say \\"hi\\""' in text


def test_timer_unit_contents():
    jobs = parse_ok("*/15 * * * * /usr/bin/backup.sh\n")
    convert_jobs(jobs)
    job = jobs[0]
    text = render_timer(job)
    assert "[Timer]" in text
    assert "OnCalendar=*-*-* *:0/15:00" in text
    assert "Unit=%s.service" % job.name in text
    assert "WantedBy=timers.target" in text


def test_timer_with_split_dom_dow_has_two_oncalendar():
    jobs = parse_ok("0 0 1 * 0 /bin/x\n")
    convert_jobs(jobs)
    text = render_timer(jobs[0])
    assert text.count("OnCalendar=") == 2


def test_reboot_service_targets_multi_user():
    jobs = parse_ok("@reboot /opt/app/start.sh\n")
    convert_jobs(jobs)
    text = render_service(jobs[0])
    assert "WantedBy=multi-user.target" in text
    assert jobs[0].oncalendars == []


# ---------- CLI ----------

def write_tmp(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return str(path)


def test_check_mode_ok_exit_zero(tmp_path, capsys):
    src = write_tmp(tmp_path, "c.tab", "*/15 * * * * /bin/x\n")
    assert main([src, "--check"]) == 0
    out = capsys.readouterr().out
    assert "OK" in out


def test_check_mode_errors_exit_one(tmp_path, capsys):
    src = write_tmp(tmp_path, "c.tab", "99 * * * * /bin/x\n")
    assert main([src, "--check"]) == 1
    out = capsys.readouterr().out
    assert "error: line 1" in out


def test_check_mode_warns_on_percent_but_stays_zero(tmp_path, capsys):
    src = write_tmp(tmp_path, "c.tab", "0 0 * * * echo a%b\n")
    assert main([src, "--check"]) == 0
    out = capsys.readouterr().out
    assert "feeds the text after % to the command on stdin" in out
    assert "Use an explicit printf pipeline" in out


def test_stdout_mode_prints_units(tmp_path, capsys):
    src = write_tmp(tmp_path, "c.tab", "0 9 * * 1-5 /usr/bin/report.sh\n")
    assert main([src, "--stdout"]) == 0
    out = capsys.readouterr().out
    assert "report.service" in out
    assert "report.timer" in out
    assert "OnCalendar=Mon..Fri *-*-* 09:00:00" in out


def test_out_dir_writes_files(tmp_path):
    src = write_tmp(tmp_path, "c.tab",
                    "0 9 * * * /usr/bin/report.sh\n@reboot /opt/app/start.sh\n")
    outdir = str(tmp_path / "units")
    assert main([src, "--out-dir", outdir]) == 0
    files = sorted(os.listdir(outdir))
    assert files == ["report.service", "report.timer", "start.service"]
    timer = open(os.path.join(outdir, "report.timer")).read()
    assert "OnCalendar=*-*-* 09:00:00" in timer


def test_cli_errors_exit_one_without_writing(tmp_path, capsys):
    src = write_tmp(tmp_path, "c.tab", "bogus line here\n")
    outdir = str(tmp_path / "units")
    assert main([src, "--out-dir", outdir]) == 1
    assert not os.path.exists(outdir)
