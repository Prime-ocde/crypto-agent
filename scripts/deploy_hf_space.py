"""一次性工具：把项目部署到 Hugging Face Space（Docker，单容器跑前后端）。

用法：
  1) 在 https://huggingface.co/settings/tokens 创建 write 权限 token；
  2) 保存到 D:\\Projects\\crypto-agent\\hf-tokens.txt（一行）；
  3) python scripts/deploy_hf_space.py

不打印 token；临时目录用后删除。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKEN_FILE = ROOT / "hf-tokens.txt"
API = "https://huggingface.co/api"
SPACE_NAME = "crypto-agent"
TMP_DIR = ROOT / ".hf_space_tmp"

COPY_DIRS = ("backend", "core", "frontend", ".streamlit")
COPY_FILES = ("requirements.txt",)


def load_token() -> str:
    if not TOKEN_FILE.exists():
        raise SystemExit(f"缺少 {TOKEN_FILE}（一行 write token）")
    for line in TOKEN_FILE.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if value and not value.startswith("#"):
            return value
    raise SystemExit("hf-tokens.txt 为空")


def api(token: str, method: str, path: str, payload: dict | None = None) -> tuple[int, object]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(API + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            raw = response.read().decode()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return exc.code, {"raw": raw[:300]}


def run(cmd: list[str], cwd: Path, hide_token: str | None = None) -> str:
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace")
    output = (proc.stdout or "") + (proc.stderr or "")
    if hide_token:
        output = output.replace(hide_token, "***")
    if proc.returncode != 0:
        print(output[-800:])
        raise SystemExit(f"命令失败({' '.join(cmd[:3])}…)，退出码 {proc.returncode}")
    return output


def copy_project(target: Path) -> None:
    for name in COPY_DIRS:
        source = ROOT / name
        if source.exists():
            shutil.copytree(source, target / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in COPY_FILES:
        shutil.copy2(ROOT / name, target / name)

    # Space 根目录需要 Dockerfile / README(frontmatter) / start.sh
    shutil.copy2(ROOT / "deploy" / "hf-space" / "Dockerfile", target / "Dockerfile")
    shutil.copy2(ROOT / "deploy" / "hf-space" / "README.md", target / "README.md")
    (target / "deploy" / "hf-space").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "deploy" / "hf-space" / "start.sh", target / "deploy" / "hf-space" / "start.sh")


def main() -> int:
    token = load_token()

    status, who = api(token, "GET", "/whoami-v2")
    if status != 200:
        print("token 校验失败:", status, who)
        return 1
    user = who.get("name") or who.get("fullname")
    print("HF 账号:", user)

    status, data = api(token, "POST", "/repos/create", {
        "type": "space",
        "name": SPACE_NAME,
        "private": False,
        "sdk": "docker",
    })
    if status in (200, 201):
        print("Space 已创建")
    elif status == 409:
        print("Space 已存在，直接更新")
    else:
        print("创建 Space 失败:", status, data)
        return 1

    if TMP_DIR.exists():
        shutil.rmtree(TMP_DIR, ignore_errors=True)
    repo_url = f"https://user:{token}@huggingface.co/spaces/{user}/{SPACE_NAME}"
    run(["git", "clone", repo_url, str(TMP_DIR)], ROOT, hide_token=token)

    copy_project(TMP_DIR)
    run(["git", "add", "-A"], TMP_DIR)
    run(["git", "-c", "user.name=crypto-agent", "-c", "user.email=crypto-agent@example.com",
         "commit", "-m", "deploy: docker space (fastapi + streamlit)"], TMP_DIR)
    run(["git", "push", "origin", "HEAD:main"], TMP_DIR, hide_token=token)

    print("等待 Space 构建（首次 3~10 分钟）…")
    url = f"https://{user.lower()}-{SPACE_NAME}.hf.space"
    for _ in range(80):
        status, runtime = api(token, "GET", f"/spaces/{user}/{SPACE_NAME}/runtime")
        stage = (runtime or {}).get("stage") if isinstance(runtime, dict) else "unknown"
        print("  stage:", stage)
        if stage == "RUNNING":
            print("SPACE_URL=" + url)
            return 0
        if stage in ("BUILD_ERROR", "CONFIG_ERROR", "RUNTIME_ERROR"):
            print("构建失败:", json.dumps(runtime, ensure_ascii=False)[:600])
            return 1
        time.sleep(12)
    print("超时：请到 https://huggingface.co/spaces/%s/%s 查看日志" % (user, SPACE_NAME))
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(TMP_DIR, ignore_errors=True)
