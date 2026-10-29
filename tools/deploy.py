"""Deploy the encrypted locator to GitHub without printing credentials."""
import argparse
import base64
import getpass
import json
import os
import re
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_FILE = ".github/workflows/locator.yml"
REQUIRED_SECRETS = {"LOCATOR_PASSWORD", "XM_TOKEN", "XM_UID"}
MAIL_ONLY_SECRETS = {"LOCATOR_PASSWORD", "XM_MAIL_CONFIG"}
# Keep the workflow last: publishing it activates its scheduled trigger.
UPLOAD = [
    "requirements-cloud.txt",
    "cloud/fetch.py",
    "cloud/health_check.py",
    "cloud/plan_store.py",
    "cloud/history_reports.py",
    "cloud/mail_import.py",
    "ingest.py",
    "store.py",
    "site/management.js",
    "site/management.css",
    "cloud/crypto_state.py",
    "common.py",
    "schedule.py",
    "xiaomi_cloud.py",
    "site/index.html",
    "site/app.js",
    "site/crypto.js",
    "site/plan-summary.js",
    "tests/test_panels.mjs",
    WORKFLOW_FILE,
]


def api(token, method, url, **kwargs):
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + token,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    response = requests.request(method, url, headers=headers, timeout=30, **kwargs)
    if response.status_code >= 300:
        detail = ""
        try:
            detail = (response.json().get("message") or "")[:160]
        except Exception:
            pass
        raise RuntimeError(
            "GitHub API 请求失败：HTTP %d%s"
            % (response.status_code, ("：" + detail) if detail else "")
        )
    return response.json() if response.content else {}


def repo_name(value):
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9_.-]+", value) or value.split("/")[-1] in (".", ".."):
        raise SystemExit("仓库必须使用 用户名/仓库名 格式")
    return value


def ensure_repo(token, repo):
    """Create only the requested missing repo; never replace an existing repo."""
    response = requests.get(
        f"https://api.github.com/repos/{repo}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + token,
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30,
    )
    if response.status_code == 200:
        return response.json().get("default_branch") or "main"
    if response.status_code != 404:
        raise RuntimeError("检查仓库失败：HTTP %d" % response.status_code)
    owner, name = repo.split("/", 1)
    created = api(
        token,
        "POST",
        "https://api.github.com/user/repos",
        json={"name": name, "description": "Encrypted locator workbench", "private": False, "auto_init": True},
    )
    if created.get("owner", {}).get("login", "").lower() != owner.lower():
        raise RuntimeError("PAT 当前用户与目标仓库用户名不一致")
    return created.get("default_branch") or "main"


def secret(token, repo, name, value):
    from nacl.public import PublicKey, SealedBox

    public_key = api(
        token,
        "GET",
        f"https://api.github.com/repos/{repo}/actions/secrets/public-key",
    )
    sealed = SealedBox(PublicKey(base64.b64decode(public_key["key"]))).encrypt(value.encode())
    api(
        token,
        "PUT",
        f"https://api.github.com/repos/{repo}/actions/secrets/{name}",
        json={"encrypted_value": base64.b64encode(sealed).decode(), "key_id": public_key["key_id"]},
    )


def verify_pages_workflow(token, repo):
    """Read back Pages configuration and require Actions workflow publishing."""
    page = api(token, "GET", f"https://api.github.com/repos/{repo}/pages")
    build_type = page.get("build_type")
    if build_type != "workflow":
        raise RuntimeError(
            "GitHub Pages 配置校验失败：build_type=%r；需要 workflow" % build_type
        )
    return page


def enable_pages(token, repo):
    """Enable Pages using Actions and verify the persisted build type."""
    try:
        api(
            token,
            "POST",
            f"https://api.github.com/repos/{repo}/pages",
            json={"build_type": "workflow"},
        )
    except RuntimeError as exc:
        # An existing Pages site can make creation return 409; read it back below.
        if "HTTP 409" not in str(exc):
            raise

    return verify_pages_workflow(token, repo)


def missing_required_secrets(token, repo, required=None):
    configured = set()
    page = 1
    while True:
        result = api(token, "GET", f"https://api.github.com/repos/{repo}/actions/secrets",
                     params={"per_page": 100, "page": page})
        items = result.get("secrets", [])
        configured.update(item.get("name") for item in items)
        if len(items) < 100:
            break
        page += 1
    return sorted((REQUIRED_SECRETS if required is None else required) - configured)


def require_required_secrets(token, repo, command, mail_only=False):
    missing = (missing_required_secrets(token, repo, MAIL_ONLY_SECRETS)
               if mail_only else missing_required_secrets(token, repo))
    if missing:
        if mail_only:
            raise SystemExit(
                "--mail-only 已停止，未上传文件、未启用 Pages、未发布 workflow、未触发 Actions："
                "缺少必需的 GitHub Actions Secrets：%s。" % ", ".join(missing)
            )
        if command == "all":
            raise SystemExit(
                "缺少必需的 GitHub Actions Secrets：%s。已设置 LOCATOR_PASSWORD，"
                "但未上传部署文件、未启用 Pages、未发布 workflow。"
                "请先在仓库 Secrets 配置小米凭据，再重新运行 all。" % ", ".join(missing)
            )
        if command == "run":
            raise SystemExit(
                "run 已停止，未启用 Pages、未触发 Actions："
                "缺少必需的 GitHub Actions Secrets：%s。" % ", ".join(missing)
            )
        raise SystemExit(
            "upload 已停止，未上传文件：缺少必需的 GitHub Actions Secrets：%s。"
            "首次部署请先配置 Secrets 并使用 all；upload 仅用于已准备好的仓库。"
            % ", ".join(missing)
        )


def verify_mail_only_plan(token, repo, branch, password):
    """Require saved remote plans; XM_PLANS secret contents cannot be verified.

    Saved plans take precedence in cloud.fetch. Never infer mail-only from a
    local plan, secret names, or absent source switches, and never rewrite plans.
    Require the default branch as scheduled runs also use that branch.
    """
    sys.path.insert(0, str(ROOT))
    import schedule
    from cloud.crypto_state import decrypt

    try:
        repository = api(token, "GET", f"https://api.github.com/repos/{repo}")
        if repository.get("default_branch") != branch:
            raise ValueError()
        result = api(token, "GET",
                     f"https://api.github.com/repos/{repo}/contents/cloud/plans.enc.json",
                     params={"ref": branch})
        if result.get("type") != "file" or result.get("encoding") != "base64":
            raise ValueError()
        envelope = json.loads(base64.b64decode(
            "".join(result["content"].split()), validate=True))
        saved = decrypt(envelope, password)
        if not isinstance(saved, dict) or saved.get("kind") != "locator-cloud-plans-v1":
            raise ValueError()
        revision = saved.get("request_id")
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", revision):
            raise ValueError()
        plans = schedule.validate_cloud_plans(saved["plans"])
        if schedule.cloud_sources(plans) != {"direct": False, "mail": True}:
            raise ValueError()
    except Exception:
        # Never echo decrypted plans, passwords, ciphertext, or API response text.
        raise SystemExit(
            "--mail-only 已停止：必须使用默认分支，且远端 cloud/plans.enc.json "
            "须能用当前看板密码解密并确认为 direct=false、mail=true 的有效计划。"
            "请先通过现有计划管理流程保存计划；本工具不会修改计划，也不以 XM_PLANS 名称代替验证。"
        ) from None


def dispatch(token, repo, branch):
    api(
        token,
        "POST",
        f"https://api.github.com/repos/{repo}/actions/workflows/locator.yml/dispatches",
        json={"ref": branch},
    )


def check_local_files(paths=None):
    """Check the allowlist without credentials, network, or account access."""
    paths = list(UPLOAD if paths is None else paths)
    missing = [relative_path for relative_path in paths if not (ROOT / relative_path).is_file()]
    if missing:
        raise RuntimeError("缺少部署文件：" + ", ".join(missing))
    return paths


def upload_paths(token, repo, branch, paths):
    paths = check_local_files(paths)
    for relative_path in paths:
        path = ROOT / relative_path
        if not path.is_file():
            raise RuntimeError("缺少部署文件：" + relative_path)
        url = f"https://api.github.com/repos/{repo}/contents/{relative_path}"
        headers = {"Authorization": "Bearer " + token}
        old = requests.get(url, headers=headers, params={"ref": branch}, timeout=30)
        body = {
            "message": "Deploy encrypted locator workbench",
            "branch": branch,
            "content": base64.b64encode(path.read_bytes()).decode(),
        }
        if old.status_code == 200:
            body["sha"] = old.json()["sha"]
        elif old.status_code != 404:
            raise RuntimeError("读取仓库文件失败：" + relative_path)
        api(token, "PUT", url, json=body)


def upload(token, repo, branch):
    """Upload the allowlist in order, with the scheduled workflow last."""
    upload_paths(token, repo, branch, UPLOAD)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["check", "upload", "secrets", "all", "plans", "run"])
    parser.add_argument("repo", nargs="?")
    parser.add_argument("--branch")
    parser.add_argument("--plans", type=Path)
    parser.add_argument("--mail-only", action="store_true",
                        help="仅邮箱部署；要求 XM_MAIL_CONFIG 及可解密验证的远端仅邮箱计划")
    args = parser.parse_args()
    if args.mail_only and args.command not in ("upload", "all", "run"):
        parser.error("--mail-only 仅支持 upload、all、run")
    if args.command == "check":
        paths = check_local_files()
        print("本地部署白名单检查通过：%d 个文件；未连接远端，未读取凭据。" % len(paths))
        return
    if not args.repo:
        parser.error("此命令需要 用户名/仓库名")
    repo_name(args.repo)
    if args.command in ("upload", "all"):
        check_local_files()

    token = os.environ.get("GITHUB_TOKEN") or getpass.getpass("GitHub PAT（不会显示）：")
    branch = args.branch or (
        "main" if args.command in ("upload", "run") else ensure_repo(token, args.repo)
    )

    password = None
    if args.mail_only:
        password = getpass.getpass("当前看板访问密码（仅用于验证远端计划，不会显示）：")
        if len(password) < 12:
            raise SystemExit("访问密码至少12个字符")
        verify_mail_only_plan(token, args.repo, branch, password)

    def require_secrets(command):
        if args.mail_only:
            require_required_secrets(token, args.repo, command, mail_only=True)
        else:
            require_required_secrets(token, args.repo, command)

    if args.command == "upload":
        require_secrets("upload")
        verify_pages_workflow(token, args.repo)
        upload(token, args.repo, branch)
        print("已上传白名单文件（workflow 最后）；未设置 Secrets，也未更改 Pages 配置。")
        return

    if args.command in ("secrets", "all"):
        if password is None:
            password = getpass.getpass("看板访问密码（至少12字符，不会显示）：")
        if len(password) < 12:
            raise SystemExit("访问密码至少12个字符")
        secret(token, args.repo, "LOCATOR_PASSWORD", password)
        print("已设置 LOCATOR_PASSWORD。")

    if args.command == "all":
        # Check names before uploading any workflow or enabling Pages.
        require_secrets("all")
        non_workflow = [path for path in UPLOAD if path != WORKFLOW_FILE]
        upload_paths(token, args.repo, branch, non_workflow)
        enable_pages(token, args.repo)
        # The scheduled workflow is activated only after files, Secrets, and Pages are ready.
        upload_paths(token, args.repo, branch, [WORKFLOW_FILE])
        dispatch(token, args.repo, branch)
        print("必需 Secrets 已配置；文件已上传、Pages workflow 模式已确认，workflow 最后发布并已触发。")
        return

    if args.command == "secrets":
        print("请在 GitHub Actions Secrets 中设置 XM_TOKEN 和 XM_UID。")
        return

    if args.command == "run":
        require_secrets("run")
        enable_pages(token, args.repo)
        dispatch(token, args.repo, branch)
        print("Pages workflow 模式已确认；已触发定位工作台 Actions。请在 GitHub Actions 页面确认结果。")
        return

    if args.command == "plans":
        if not args.plans:
            raise SystemExit("plans 命令需要 --plans cloud-plans.json")
        sys.path.insert(0, str(ROOT))
        import schedule

        value = json.loads(args.plans.read_text(encoding="utf-8"))
        schedule.validate_cloud_plans(value)
        secret(
            token,
            args.repo,
            "XM_PLANS",
            json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        )
        print("已验证并设置 XM_PLANS。")


if __name__ == "__main__":
    main()
