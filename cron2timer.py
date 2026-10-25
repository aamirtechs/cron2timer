#!/usr/bin/env python3
"""cron2timer: convert crontab lines into systemd .service and .timer units.

Zero dependencies, standard library only.
"""

import argparse
import os
import re
import sys

VERSION = "1.0.0"

MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

DOW_NAMES = {
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}

DOW_NUM_TO_NAME = {
    0: "Sun", 1: "Mon", 2: "Tue", 3: "Wed",
    4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun",
}

# (minimum, maximum) for each cron field
FIELD_LIMITS = {
    "minute": (0, 59),
    "hour": (0, 23),
    "dom": (1, 31),
    "month": (1, 12),
    "dow": (0, 7),
}

SHORTCUTS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

INTERPRETERS = {
    "sh", "bash", "dash", "zsh", "ksh",
    "python", "python2", "python3", "perl", "ruby",
    "node", "nodejs", "php", "env",
}


class CronError(Exception):
    """A parse or validation error tied to a crontab line number."""

    def __init__(self, lineno, message):
        super().__init__("line %d: %s" % (lineno, message))
        self.lineno = lineno
        self.message = message


class CronJob:
    """One parsed cron job: schedule fields, command, and env assignments."""

    def __init__(self, lineno, raw, fields, command, env, reboot=False):
        self.lineno = lineno
        self.raw = raw
        self.fields = fields          # tuple of 5 strings, or None for @reboot
        self.command = command
        self.env = dict(env)          # env assignments in effect for this job
        self.reboot = reboot
        self.name = None              # assigned later
        self.oncalendars = []         # assigned later


def _token_to_int(token, kind):
    """Convert one numeric-or-name token to an int, validating the range."""
    low = token.lower()
    if kind == "month" and low in MONTH_NAMES:
        return MONTH_NAMES[low]
    if kind == "dow" and low in DOW_NAMES:
        return DOW_NAMES[low]
    if re.fullmatch(r"\d+", token):
        value = int(token)
        lo, hi = FIELD_LIMITS[kind]
        if not (lo <= value <= hi):
            raise ValueError("value %d out of range %d-%d" % (value, lo, hi))
        return value
    raise ValueError("bad value %r" % token)


def validate_field(field, kind):
    """Validate one cron field. Raises ValueError with a reason on failure."""
    if field == "*":
        return
    for part in field.split(","):
        m = re.fullmatch(r"(\*|[^-/,]+?)(?:-([^-/,]+))?(?:/(\d+))?", part)
        if not m:
            raise ValueError("bad syntax in %r" % part)
        start_s, end_s, step_s = m.group(1), m.group(2), m.group(3)
        if start_s == "*":
            if end_s is not None:
                raise ValueError("bad syntax in %r" % part)
            start = FIELD_LIMITS[kind][0]
        else:
            start = _token_to_int(start_s, kind)
        if end_s is not None:
            end = _token_to_int(end_s, kind)
            if start > end:
                raise ValueError("range start %d after end %d" % (start, end))
        if step_s is not None and int(step_s) < 1:
            raise ValueError("step must be >= 1")


def parse_crontab(text):
    """Parse crontab text into (jobs, errors). Env assignments apply to
    jobs that follow them, matching cron semantics."""
    jobs = []
    errors = []
    env = {}
    shortcut_re = re.compile(r"^(@\w+)\s+(.*)$")
    env_re = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s?(.*)$")

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = env_re.match(line)
        if m and not line.startswith("@"):
            # Distinguish env assignments from cron lines: a cron line never
            # starts with NAME=, and "=" only appears inside commands.
            name, value = m.group(1), m.group(2)
            value = value.strip()
            if (len(value) >= 2 and value[0] == value[-1]
                    and value[0] in ("'", '"')):
                value = value[1:-1]
            env[name] = value
            continue
        m = shortcut_re.match(line)
        if m:
            name, command = m.group(1).lower(), m.group(2).strip()
            if name == "@reboot":
                if not command:
                    errors.append(CronError(lineno, "@reboot needs a command"))
                    continue
                jobs.append(CronJob(lineno, raw, None, command, env,
                                   reboot=True))
                continue
            if name not in SHORTCUTS:
                errors.append(CronError(lineno, "unknown shortcut %r" % name))
                continue
            fields = tuple(SHORTCUTS[name].split())
            if not command:
                errors.append(CronError(lineno, "%s needs a command" % name))
                continue
            jobs.append(CronJob(lineno, raw, fields, command, env))
            continue
        parts = line.split()
        if len(parts) < 6:
            errors.append(CronError(
                lineno,
                "expected 5 schedule fields plus a command, found %d fields"
                % len(parts)))
            continue
        fields = tuple(parts[:5])
        command = line.split(None, 5)[5]
        kinds = ("minute", "hour", "dom", "month", "dow")
        bad = None
        for kind, field in zip(kinds, fields):
            try:
                validate_field(field, kind)
            except ValueError as exc:
                bad = "%s field %r: %s" % (kind, field, exc)
                break
        if bad:
            errors.append(CronError(lineno, bad))
            continue
        jobs.append(CronJob(lineno, raw, fields, command, env))
    return jobs, errors


def _cron_set(field, kind):
    """Expand one cron field into the set of ints it matches."""
    lo, hi = FIELD_LIMITS[kind]
    if kind == "dow":
        hi = 6  # treat 7 as 0 below
    result = set()
    if field == "*":
        return set(range(lo, hi + 1))
    for part in field.split(","):
        m = re.fullmatch(r"(\*|[^-/]+?)(?:-([^-/]+))?(?:/(\d+))?", part)
        start_s = m.group(1)
        if start_s == "*":
            start, end = lo, hi
        else:
            start = _token_to_int(start_s, kind)
            end = _token_to_int(m.group(2), kind) if m.group(2) else start
        step = int(m.group(3)) if m.group(3) else 1
        if kind == "dow":
            start %= 7
            end %= 7
        result.update(range(start, end + 1, step))
    return result


def _numeric_component(field, kind):
    """Rewrite a numeric cron field into systemd calendar syntax.

    systemd needs ".." for ranges and an explicit start for steps
    ("0/15", not "*/15"). Names are not accepted for months, so convert
    them to numbers first.
    """
    lo, _hi = FIELD_LIMITS[kind]
    out = []
    for part in field.split(","):
        if part == "*":
            out.append("*")
            continue
        m = re.fullmatch(r"(\*|[^-/]+?)(?:-([^-/]+))?(?:/(\d+))?", part)
        start_s, end_s, step_s = m.group(1), m.group(2), m.group(3)
        if start_s == "*":
            # "*/n" -> "0/n" (minute/hour) or "1/n" (dom/month)
            start_s = str(lo)
        else:
            start_s = str(_token_to_int(start_s, kind))
        if end_s is not None:
            piece = "%s..%s" % (start_s, _token_to_int(end_s, kind))
        else:
            piece = start_s
        if step_s is not None:
            piece += "/%s" % step_s
        out.append(piece)
    return ",".join(out)


def _dow_component(field):
    """Rewrite a cron day-of-week field into systemd weekday names.

    systemd only accepts names here (no numbers, no steps), so step
    expressions are expanded into explicit name lists.
    """
    if field == "*":
        return "*"
    values = sorted(_cron_set(field, "dow"))
    if values == list(range(7)):
        return "*"
    # single contiguous run -> "Mon..Fri" form
    if values == list(range(values[0], values[-1] + 1)):
        if len(values) == 1:
            return DOW_NUM_TO_NAME[values[0]]
        return "%s..%s" % (DOW_NUM_TO_NAME[values[0]],
                           DOW_NUM_TO_NAME[values[-1]])
    return ",".join(DOW_NUM_TO_NAME[v] for v in values)


def cron_to_oncalendars(fields):
    """Convert 5 cron fields into one or more systemd OnCalendar strings.

    When both day-of-month and day-of-week are restricted, cron uses OR
    semantics while a single systemd expression uses AND, so two
    OnCalendar lines are emitted (a timer ORs multiple OnCalendar lines).
    """
    minute, hour, dom, month, dow = fields
    minute_s = _numeric_component(minute, "minute")
    hour_s = _numeric_component(hour, "hour")
    dom_s = _numeric_component(dom, "dom")
    month_s = _numeric_component(month, "month")
    dow_s = _dow_component(dow)

    def build(dom_part, dow_part):
        date = "*-{m}-{d}".format(m=month_s, d=dom_part)
        hour = hour_s.zfill(2) if hour_s.isdigit() else hour_s
        minute = minute_s.zfill(2) if minute_s.isdigit() else minute_s
        time = "{h}:{mi}:00".format(h=hour, mi=minute)
        if dow_part == "*":
            return "%s %s" % (date, time)
        return "%s %s %s" % (dow_part, date, time)

    dom_restricted = dom != "*"
    dow_restricted = dow_s != "*"
    if dom_restricted and dow_restricted:
        return [build(dom_s, "*"), build("*", dow_s)]
    return [build(dom_s, dow_s)]


def sanitize_name(command, used):
    """Derive a systemd-safe unit name from the job command.

    Uses the real program name (skipping interpreters like python3/sh),
    lowercases, swaps non-alphanumerics for dashes, and de-duplicates.
    """
    words = command.split()
    prog = words[0] if words else "job"
    base = os.path.basename(prog)
    if base.lower() in INTERPRETERS and len(words) > 1:
        base = os.path.basename(words[1])
    base = os.path.splitext(base)[0]
    name = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-") or "job"
    name = name[:40].rstrip("-") or "job"
    candidate = name
    n = 2
    while candidate in used:
        candidate = "%s-%d" % (name, n)
        n += 1
    used.add(candidate)
    return candidate


def _quote_shell_single(command):
    return "'%s'" % command.replace("'", "'\\''")


def _quote_systemd_env(value):
    return (value.replace("\\", "\\\\").replace('"', '\\"')
            .replace("\n", "\\n").replace("\t", "\\t"))


def render_service(job, user=False):
    lines = [
        "# Generated by cron2timer from crontab line %d" % job.lineno,
        "# Original cron line: %s" % job.raw.strip(),
        "[Unit]",
        "Description=Cron job converted by cron2timer: %s"
        % (job.command[:80]),
        "",
        "[Service]",
        "Type=oneshot",
    ]
    for name, value in job.env.items():
        lines.append('Environment="%s=%s"'
                     % (name, _quote_systemd_env(value)))
    lines.append("ExecStart=/bin/sh -c %s" % _quote_shell_single(job.command))
    if job.reboot:
        lines += [
            "",
            "[Install]",
            "WantedBy=default.target" if user else "WantedBy=multi-user.target",
        ]
    return "\n".join(lines) + "\n"


def render_timer(job):
    lines = [
        "# Generated by cron2timer from crontab line %d" % job.lineno,
        "# Original cron line: %s" % job.raw.strip(),
        "[Unit]",
        "Description=Timer for cron job %s" % job.name,
        "",
        "[Timer]",
    ]
    for cal in job.oncalendars:
        lines.append("OnCalendar=%s" % cal)
    lines += [
        "Unit=%s.service" % job.name,
        "",
        "[Install]",
        "WantedBy=timers.target",
    ]
    return "\n".join(lines) + "\n"


def convert_jobs(jobs):
    """Assign unit names and OnCalendar expressions to parsed jobs."""
    used = set()
    for job in jobs:
        job.name = sanitize_name(job.command, used)
        if not job.reboot:
            job.oncalendars = cron_to_oncalendars(job.fields)
    return jobs


def check_text(text):
    """Validate crontab text. Returns (ok, messages)."""
    jobs, errors = parse_crontab(text)
    messages = []
    for err in errors:
        messages.append("error: %s" % err)
    for job in jobs:
        if "%" in job.command:
            messages.append(
                "warning: line %d: unescaped %% in cron splits the command "
                "at that point and feeds the text after %% to the command "
                "on stdin; cron2timer cannot map this to a systemd unit. "
                "Use an explicit printf pipeline to provide stdin instead"
                % job.lineno)
    ok = not errors
    if ok:
        messages.append("OK: %d job(s) parsed, no errors" % len(jobs))
    return ok, messages


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="cron2timer",
        description="Convert crontab lines into systemd .service "
                    "and .timer unit files.")
    parser.add_argument("input", nargs="?",
                        help="crontab file to read (default: stdin)")
    parser.add_argument("--check", action="store_true",
                        help="validate the crontab and exit 0/1, "
                             "writing no files")
    parser.add_argument("--stdout", action="store_true",
                        help="print the units instead of writing files")
    parser.add_argument("--out-dir", default="units",
                        help="directory for the generated units "
                             "(default: units; with --user try "
                             "~/.config/systemd/user)")
    parser.add_argument("--user", action="store_true",
                        help="target systemd user timers "
                             "(install with systemctl --user enable --now)")
    parser.add_argument("--version", action="version",
                        version="cron2timer %s" % VERSION)
    args = parser.parse_args(argv)

    if args.input:
        with open(args.input, "r", encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = sys.stdin.read()

    if args.check:
        ok, messages = check_text(text)
        for msg in messages:
            print(msg)
        return 0 if ok else 1

    jobs, errors = parse_crontab(text)
    for err in errors:
        print("error: %s" % err, file=sys.stderr)
    if errors:
        return 1
    convert_jobs(jobs)

    units = []
    for job in jobs:
        units.append(("%s.service" % job.name, render_service(job, user=args.user)))
        if not job.reboot:
            units.append(("%s.timer" % job.name, render_timer(job)))
        else:
            print("note: line %d is @reboot, service installs to "
                  "%s, no timer generated"
                  % (job.lineno, "default.target" if args.user else "multi-user.target"),
                  file=sys.stderr)

    if args.stdout:
        for fname, content in units:
            print("#" + "-" * 60)
            print("# %s" % fname)
            print("#" + "-" * 60)
            print(content, end="")
        return 0

    os.makedirs(args.out_dir, exist_ok=True)
    for fname, content in units:
        path = os.path.join(args.out_dir, fname)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        print("wrote %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
