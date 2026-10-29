"""Offline guards for collector/Pages separation; no credentials or GitHub calls."""
from pathlib import Path
import itertools
import os
import re
import shutil
import subprocess

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/locator.yml"


def steps():
    text = WORKFLOW.read_text(encoding="utf-8")
    return re.findall(r"^      - .*(?:\n(?!      - )[^\n]*)*", text, re.MULTILINE)


def field(step, name):
    match = re.search(r"^        " + re.escape(name) + r": (.+)$", step, re.MULTILINE)
    return match.group(1) if match else None


def test_collector_failure_is_tolerated_but_only_success_can_save_state():
    configured = steps()
    collect = next(s for s in configured if field(s, "id") == "collect")
    assert field(collect, "continue-on-error") == "true"
    assert field(collect, "run") == "python cloud/fetch.py"
    save = next(s for s in configured if "git add site/state.enc.json" in s)
    assert field(save, "if") == "${{ steps.collect.outcome == 'success' }}"
    assert configured.index(collect) < configured.index(save)
    # Failed collection must not create placeholders or replace historical ciphertext.
    state_steps = [s for s in configured if "state.enc.json" in s]
    assert state_steps == [save]


def test_pages_ignore_collection_failure_but_require_successful_publish_dependencies():
    configured = steps()
    pages = [s for s in configured if re.search(r"actions/(?:configure-pages|upload-pages-artifact|deploy-pages)@", s)]
    assert len(pages) == 3
    assert field(pages[0], "id") == "configure_pages"
    assert field(pages[0], "if") == "${{ !cancelled() }}"
    assert field(pages[1], "id") == "upload_pages"
    assert field(pages[1], "if") == "${{ !cancelled() && steps.configure_pages.outcome == 'success' }}"
    assert field(pages[2], "if") == "${{ !cancelled() && steps.upload_pages.outcome == 'success' }}"
    assert "          path: site" in pages[1]
    assert field(pages[2], "id") == "deployment"
    save_index = next(i for i, s in enumerate(configured) if "git add site/state.enc.json" in s)
    assert all(configured.index(s) > save_index for s in pages)
    assert not any(field(s, "continue-on-error") == "true" for s in pages)


def test_collection_failure_is_reported_after_deployment_as_a_real_failure():
    configured = steps()
    final = configured[-1]
    assert field(final, "if") == "${{ always() && (steps.collect.outcome == 'failure' || steps.health.outcome == 'failure') }}"
    assert field(final, "continue-on-error") is None
    assert re.search(r"^          exit 1$", final, re.MULTILINE)
    assert "::error::" in final
    assert configured.index(next(s for s in configured if field(s, "id") == "deployment")) < len(configured) - 1


def test_encrypted_plan_dispatch_inputs_are_declared_and_never_interpolated_in_shell():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "options: [collect, save_plans]" in text
    assert "      payload:\n        type: string" in text
    plan = next(s for s in steps() if "python cloud/plan_store.py" in s)
    assert field(plan, "if") == "${{ inputs.operation == 'save_plans' }}"
    assert "LOCATOR_PLAN_PAYLOAD: ${{ inputs.payload }}" in plan
    assert "git add cloud/plans.enc.json" in plan
    run = plan.split("        env:")[0]
    assert "inputs.payload" not in run


def test_published_fallback_cannot_report_a_healthy_workflow():
    configured = steps()
    health = next(s for s in configured if field(s, "run") == "python cloud/health_check.py")
    assert field(health, "if") == "${{ !cancelled() && steps.collect.outcome == 'success' && steps.deployment.outcome == 'success' }}"
    assert field(health, "id") == "health"
    assert field(health, "continue-on-error") is None
    assert "LOCATOR_PASSWORD: ${{ secrets.LOCATOR_PASSWORD }}" in health
    deployment = next(s for s in configured if field(s, "id") == "deployment")
    assert configured.index(health) > configured.index(deployment)
    assert configured[-2] == health

@pytest.mark.parametrize("collect,health,cancelled", list(itertools.product(
    ("success", "failure", "skipped", "cancelled"),
    ("success", "failure", "skipped", "cancelled"),
    (False, True),
)))
def test_failure_report_condition_uses_actual_outcomes_even_after_failure_or_cancellation(
    collect, health, cancelled
):
    condition = field(steps()[-1], "if")
    # Evaluate the configured expression with GitHub's implicit success() guard:
    # a status function is required to report after an earlier health failure.
    expression = condition.removeprefix("${{").removesuffix("}}").strip()
    expression = expression.replace("steps.collect.outcome", repr(collect))
    expression = expression.replace("steps.health.outcome", repr(health))
    expression = expression.replace("&&", " and ").replace("||", " or ")
    has_status = re.search(r"\b(?:always|success|failure|cancelled)\(\)", expression)
    status = {
        "always": lambda: True,
        "success": lambda: not cancelled and collect == health == "success",
        "failure": lambda: "failure" in (collect, health),
        "cancelled": lambda: cancelled,
    }
    actual = (bool(has_status) or status["success"]()) and eval(
        expression, {"__builtins__": {}}, status
    )
    assert actual == (collect == "failure" or health == "failure")


@pytest.mark.parametrize("collect,health,expected,forbidden", [
    ("failure", "skipped", "云端采集失败", "发布数据健康检查失败"),
    ("success", "failure", "发布数据健康检查失败", "历史加密状态未更新"),
    ("failure", "failure", "云端采集失败", "发布数据健康检查失败"),
])
def test_failure_report_shell_distinguishes_collection_and_health_without_secrets(
    collect, health, expected, forbidden
):
    report = steps()[-1]
    assert "COLLECT_OUTCOME: ${{ steps.collect.outcome }}" in report
    assert "secrets." not in report
    assert "steps.collect.conclusion" not in report
    script = report.split("        run: |\n", 1)[1].split("\n        env:", 1)[0]
    script = "\n".join(line.removeprefix("          ") for line in script.splitlines())
    bash = shutil.which("bash")
    assert bash is not None, "Bash is required to execute the Actions report script"
    sentinel = "TEST_SECRET_MUST_NOT_APPEAR"
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
        env={**os.environ, "COLLECT_OUTCOME": collect,
             "HEALTH_OUTCOME": health, "LOCATOR_PASSWORD": sentinel, "XM_TOKEN": sentinel},
        capture_output=True, text=True, encoding="utf-8", timeout=10,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert output.count("::error::") == 1
    assert expected in output
    assert forbidden not in output
    assert sentinel not in output
    if collect == "failure":
        assert "历史加密状态未更新" in output
    else:
        assert "历史位置" in output
        assert "不代表获取到新位置" in output
