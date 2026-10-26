"""Offline guards for collector/Pages separation; no credentials or GitHub calls."""
from pathlib import Path
import re

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
    assert field(final, "if") == "${{ always() && steps.collect.outcome == 'failure' }}"
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
    assert field(health, "continue-on-error") is None
    assert "LOCATOR_PASSWORD: ${{ secrets.LOCATOR_PASSWORD }}" in health
    deployment = next(s for s in configured if field(s, "id") == "deployment")
    assert configured.index(health) > configured.index(deployment)
