import os


def pytest_sessionfinish(session, exitstatus):
    """CI sets TANKSIM_NO_SKIPS, so a browser or pymodbus that failed to
    install turns the run red instead of quietly skipping those tests."""
    if not os.environ.get("TANKSIM_NO_SKIPS") or exitstatus != 0:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    skipped = reporter.stats.get("skipped", []) if reporter else []
    if skipped:
        if reporter:
            reporter.write_sep("=", f"TANKSIM_NO_SKIPS: {len(skipped)} skipped, failing the run", red=True)
        session.exitstatus = 1
