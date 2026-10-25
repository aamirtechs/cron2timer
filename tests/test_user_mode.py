"""--user mode writes user-timer install targets."""

from cron2timer import CronJob, render_service


def _job(reboot=False):
    return CronJob(1, "@reboot /opt/app/start.sh", None, "/opt/app/start.sh", {}, reboot=reboot)


def test_user_reboot_service_targets_default_target():
    text = render_service(_job(reboot=True), user=True)
    assert "WantedBy=default.target" in text
    assert "multi-user.target" not in text


def test_system_reboot_service_targets_multi_user():
    text = render_service(_job(reboot=True), user=False)
    assert "WantedBy=multi-user.target" in text


def test_regular_service_has_no_install_section():
    job = CronJob(2, "0 0 * * * /bin/true", ("0", "0", "*", "*", "*"), "/bin/true", {})
    text = render_service(job, user=True)
    assert "[Install]" not in text
