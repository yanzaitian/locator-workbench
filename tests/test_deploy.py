"""Offline tests for the GitHub deployment helper."""
import base64
import contextlib
import json
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import tools.deploy as deploy


class DeployCheckTests(unittest.TestCase):
    def test_allowlist_check_accepts_complete_local_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for relative in deploy.UPLOAD:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture", encoding="utf-8")
            with patch.object(deploy, "ROOT", root):
                self.assertEqual(deploy.check_local_files(), deploy.UPLOAD)

    def test_allowlist_check_reports_missing_local_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "present.txt").write_text("fixture", encoding="utf-8")
            with patch.object(deploy, "ROOT", root):
                with self.assertRaisesRegex(RuntimeError, "missing.txt"):
                    deploy.check_local_files(["present.txt", "missing.txt"])

    def test_check_command_needs_no_repository_or_network(self):
        output = io.StringIO()
        with patch.object(sys, "argv", ["deploy.py", "check"]), \
             patch.object(deploy.requests, "request", side_effect=AssertionError("network access")) as request, \
             patch.object(deploy.requests, "get", side_effect=AssertionError("network access")) as get, \
             contextlib.redirect_stdout(output):
            deploy.main()
        request.assert_not_called()
        get.assert_not_called()
        self.assertIn("未连接远端", output.getvalue())


class DeployFlowTests(unittest.TestCase):
    def setUp(self):
        # Reject every HTTP request, including accidental unmocked code paths.
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(deploy.requests, "request", side_effect=AssertionError("network access")))
        self.stack.enter_context(patch.object(deploy.requests, "get", side_effect=AssertionError("network access")))
        self.stack.enter_context(patch.dict(deploy.os.environ, {}, clear=True))
        self.stack.enter_context(patch.object(deploy.getpass, "getpass", return_value="offline-fixture-123"))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def invoke(self, command, *extra):
        with patch.object(sys, "argv", ["deploy.py", command, "fixture/workbench", "--branch", "main", *extra]):
            deploy.main()

    def test_all_uploads_workflow_only_after_pages_and_files(self):
        events = []
        with patch.object(deploy, "secret", side_effect=lambda *a: events.append("secret")), \
             patch.object(deploy, "require_required_secrets", side_effect=lambda *a: events.append("check-secrets")), \
             patch.object(deploy, "upload_paths", side_effect=lambda t, r, b, p: events.append(list(p))), \
             patch.object(deploy, "enable_pages", side_effect=lambda *a: events.append("pages")), \
             patch.object(deploy, "dispatch", side_effect=lambda *a: events.append("dispatch")):
            self.invoke("all")
        self.assertEqual(events, ["secret", "check-secrets",
            [p for p in deploy.UPLOAD if p != deploy.WORKFLOW_FILE], "pages",
            [deploy.WORKFLOW_FILE], "dispatch"])

    def test_missing_secrets_stops_all_before_upload_pages_and_dispatch(self):
        with patch.object(deploy, "secret"), \
             patch.object(deploy, "missing_required_secrets", return_value=["XM_UID"]), \
             patch.object(deploy, "upload_paths") as upload, \
             patch.object(deploy, "enable_pages") as pages, \
             patch.object(deploy, "dispatch") as dispatch:
            with self.assertRaises(SystemExit):
                self.invoke("all")
        upload.assert_not_called()
        pages.assert_not_called()
        dispatch.assert_not_called()

    def test_upload_checks_pages_before_uploading_and_never_sets_secrets(self):
        with patch.object(deploy, "require_required_secrets"), \
             patch.object(deploy, "verify_pages_workflow", side_effect=RuntimeError("wrong mode")), \
             patch.object(deploy, "upload") as upload, \
             patch.object(deploy, "secret") as secret, \
             patch.object(deploy, "enable_pages") as pages:
            with self.assertRaisesRegex(RuntimeError, "wrong mode"):
                self.invoke("upload")
        upload.assert_not_called()
        secret.assert_not_called()
        pages.assert_not_called()

    def test_existing_pages_conflict_requires_workflow_readback(self):
        with patch.object(deploy, "api", side_effect=[RuntimeError("HTTP 409"), {"build_type": "workflow"}]) as api:
            self.assertEqual(deploy.enable_pages("fixture", "fixture/workbench")["build_type"], "workflow")
        self.assertEqual([call.args[1] for call in api.call_args_list], ["POST", "GET"])

    def test_existing_pages_wrong_mode_is_rejected(self):
        with patch.object(deploy, "api", side_effect=[RuntimeError("HTTP 409"), {"build_type": "legacy"}]):
            with self.assertRaisesRegex(RuntimeError, "workflow"):
                deploy.enable_pages("fixture", "fixture/workbench")

    def test_run_never_dispatches_if_pages_verification_fails(self):
        with patch.object(deploy, "require_required_secrets"), \
             patch.object(deploy, "enable_pages", side_effect=RuntimeError("verification failed")), \
             patch.object(deploy, "dispatch") as dispatch:
            with self.assertRaisesRegex(RuntimeError, "verification failed"):
                self.invoke("run")
        dispatch.assert_not_called()

    def test_secret_name_check_reads_all_pages(self):
        first = [{"name": "EXAMPLE_%d" % i} for i in range(100)]
        second = [{"name": name} for name in deploy.REQUIRED_SECRETS]
        with patch.object(deploy, "api", side_effect=[{"secrets": first}, {"secrets": second}]) as api:
            self.assertEqual(deploy.missing_required_secrets("fixture", "fixture/workbench"), [])
        self.assertEqual([call.kwargs["params"]["page"] for call in api.call_args_list], [1, 2])

    def test_missing_local_files_stops_before_credentials_or_remote(self):
        with patch.object(deploy, "check_local_files", side_effect=RuntimeError("missing file")), \
             patch.object(deploy.getpass, "getpass") as prompt, \
             patch.object(deploy, "ensure_repo") as ensure:
            with self.assertRaisesRegex(RuntimeError, "missing file"):
                self.invoke("all")
        prompt.assert_not_called()
        ensure.assert_not_called()

    def test_repo_path_rejects_url_and_query_string(self):
        for invalid in ["https://example.test/o/r", "owner/repo?ref=main", "owner/..", "owner/repo#fragment"]:
            with self.subTest(value=invalid), self.assertRaises(SystemExit):
                deploy.repo_name(invalid)
        self.assertEqual(deploy.repo_name("example-user/workbench.name"), "example-user/workbench.name")

    def mail_api(self, plans=None, names=None, saved=None):
        from cloud.crypto_state import encrypt

        saved = saved if saved is not None else {
            "kind": "locator-cloud-plans-v1",
            "request_id": "01234567-89ab-cdef-0123-456789abcdef",
            "plans": plans if plans is not None else {"sources": {"direct": False, "mail": True}},
        }
        content = base64.b64encode(json.dumps(
            encrypt(saved, "offline-fixture-123")).encode()).decode()

        def reply(token, method, url, **kwargs):
            self.assertEqual(method, "GET")  # No remote writes in plan verification.
            if url.endswith("/actions/secrets"):
                return {"secrets": [{"name": n} for n in (
                    deploy.MAIL_ONLY_SECRETS if names is None else names)]}
            if url.endswith("/contents/cloud/plans.enc.json"):
                self.assertEqual(kwargs["params"], {"ref": "main"})
                return {"type": "file", "encoding": "base64", "content": content}
            self.assertTrue(url.endswith("/repos/fixture/workbench"))
            return {"default_branch": "main"}
        return reply

    def test_mail_only_all_preserves_publication_order_without_xiaomi_secrets(self):
        events = []
        with patch.object(deploy, "api", side_effect=self.mail_api()), \
             patch.object(deploy, "secret", side_effect=lambda *a: events.append((a[2], a[3]))), \
             patch.object(deploy, "upload_paths", side_effect=lambda t, r, b, p: events.append(list(p))), \
             patch.object(deploy, "enable_pages", side_effect=lambda *a: events.append("pages")), \
             patch.object(deploy, "dispatch", side_effect=lambda *a: events.append("dispatch")):
            self.invoke("all", "--mail-only")
        self.assertEqual(events, [("LOCATOR_PASSWORD", "offline-fixture-123"),
            [p for p in deploy.UPLOAD if p != deploy.WORKFLOW_FILE], "pages",
            [deploy.WORKFLOW_FILE], "dispatch"])

    def test_mail_only_upload_and_run_keep_pages_gates(self):
        for command, gate in (("upload", "verify_pages_workflow"), ("run", "enable_pages")):
            with self.subTest(command=command), \
                 patch.object(deploy, "api", side_effect=self.mail_api()), \
                 patch.object(deploy, gate, side_effect=RuntimeError("wrong pages")), \
                 patch.object(deploy, "upload") as upload, \
                 patch.object(deploy, "dispatch") as dispatch, \
                 patch.object(deploy, "secret") as secret:
                with self.assertRaisesRegex(RuntimeError, "wrong pages"):
                    self.invoke(command, "--mail-only")
                upload.assert_not_called()
                dispatch.assert_not_called()
                secret.assert_not_called()

    def test_mail_only_upload_and_run_succeed_without_setting_secrets(self):
        for command in ("upload", "run"):
            events = []
            with self.subTest(command=command), \
                 patch.object(deploy, "api", side_effect=self.mail_api()), \
                 patch.object(deploy, "verify_pages_workflow", side_effect=lambda *a: events.append("verify")), \
                 patch.object(deploy, "enable_pages", side_effect=lambda *a: events.append("pages")), \
                 patch.object(deploy, "upload_paths", side_effect=lambda t, r, b, p: events.append(list(p))), \
                 patch.object(deploy, "dispatch", side_effect=lambda *a: events.append("dispatch")), \
                 patch.object(deploy, "secret") as secret:
                self.invoke(command, "--mail-only")
                secret.assert_not_called()
            self.assertEqual(events, ["verify", deploy.UPLOAD] if command == "upload" else ["pages", "dispatch"])

    def test_mail_only_rejects_corrupt_remote_content(self):
        reply = self.mail_api()
        def corrupt(*args, **kwargs):
            result = reply(*args, **kwargs)
            if "content" in result:
                result["content"] = "invalid-base64!"
            return result
        with patch.object(deploy, "api", side_effect=corrupt):
            with self.assertRaisesRegex(SystemExit, "--mail-only"):
                deploy.verify_mail_only_plan("fixture", "fixture/workbench", "main", "offline-fixture-123")

    def test_mail_only_missing_secrets_blocks_every_publication_command(self):
        for command in ("all", "upload", "run"):
            for missing in deploy.MAIL_ONLY_SECRETS:
                with self.subTest(command=command, missing=missing), \
                     patch.object(deploy, "verify_mail_only_plan"), \
                     patch.object(deploy, "api", return_value={"secrets": [
                         {"name": n} for n in deploy.MAIL_ONLY_SECRETS - {missing}]}), \
                     patch.object(deploy, "secret"), \
                     patch.object(deploy, "upload_paths") as upload, \
                     patch.object(deploy, "verify_pages_workflow") as verify, \
                     patch.object(deploy, "enable_pages") as pages, \
                     patch.object(deploy, "dispatch") as dispatch:
                    with self.assertRaisesRegex(SystemExit, missing):
                        self.invoke(command, "--mail-only")
                    for effect in (upload, verify, pages, dispatch):
                        effect.assert_not_called()

    def test_remote_plan_must_resolve_to_mail_only(self):
        for plans in ({}, {"sources": {"mail": True}},
                      {"sources": {"direct": True, "mail": True}},
                      {"sources": {"direct": False, "mail": False}},
                      {"sources": {"direct": 0}},
                      {"sources": {"direct": False, "unknown": True}},
                      {"sources": {"direct": False}, "devices": []}):
            with self.subTest(plans=plans), patch.object(deploy, "api", side_effect=self.mail_api(plans)):
                with self.assertRaisesRegex(SystemExit, "--mail-only"):
                    deploy.verify_mail_only_plan("fixture", "fixture/workbench", "main", "offline-fixture-123")
        # Missing mail switch resolves to True, exactly as in the runner.
        with patch.object(deploy, "api", side_effect=self.mail_api({"sources": {"direct": False}})):
            deploy.verify_mail_only_plan("fixture", "fixture/workbench", "main", "offline-fixture-123")

    def test_mail_only_rejects_unverifiable_remote_plan_before_any_write(self):
        fixtures = [self.mail_api(saved={"kind": "wrong"}),
                    self.mail_api(saved={"kind": "locator-cloud-plans-v1", "request_id": "invalid"}),
                    self.mail_api(), RuntimeError("private server detail"),
                    {"default_branch": "other"}, {"default_branch": "main"}]
        for index, fixture in enumerate(fixtures):
            # Wrong password, missing file response and API errors must all fail closed.
            with self.subTest(index=index), \
                 patch.object(deploy, "api", **({"side_effect": fixture} if callable(fixture) or isinstance(fixture, Exception)
                                               else {"return_value": fixture})), \
                 patch.object(deploy.getpass, "getpass", return_value=("wrong-password-123" if index == 2 else "offline-fixture-123")), \
                 patch.object(deploy, "secret") as secret, \
                 patch.object(deploy, "upload_paths") as upload, \
                 patch.object(deploy, "enable_pages") as pages, \
                 patch.object(deploy, "dispatch") as dispatch:
                with self.assertRaises(SystemExit) as exc:
                    self.invoke("all", "--mail-only")
                self.assertNotIn("private server detail", str(exc.exception))
                self.assertNotIn("offline-fixture-123", str(exc.exception))
                for effect in (secret, upload, pages, dispatch):
                    effect.assert_not_called()

    def test_default_mode_still_requires_xiaomi_secrets_even_with_mail_config(self):
        with patch.object(deploy, "api", return_value={"secrets": [
                {"name": n} for n in deploy.MAIL_ONLY_SECRETS]}):
            self.assertEqual(deploy.missing_required_secrets("fixture", "fixture/workbench"), ["XM_TOKEN", "XM_UID"])

    def test_mail_only_rejects_commands_that_do_not_publish(self):
        for command in ("check", "secrets", "plans"):
            with self.subTest(command=command), \
                 patch.object(deploy.getpass, "getpass") as prompt, \
                 contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    self.invoke(command, "--mail-only")
                prompt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
