"""
Oil & gas price update from your own PC.

Yahoo Finance refuses most requests from GitHub's servers but normally answers home and office
connections. This script, started by Windows Task Scheduler, does what the GitHub workflow can't:

  1. downloads the current update script and oil & gas data files from GitHub,
  2. runs the update here (only missing days are requested, so it is quick when nothing is new),
  3. uploads the changed data files to GitHub as one commit, which republishes the website.

Nothing needs to be installed besides Python (Anaconda is fine), and git is not needed.

    python update_from_pc.py --setup    # first time: asks for the GitHub token (and EIA key), test run
    python update_from_pc.py            # normal run (this is what the scheduled task does)
    python update_from_pc.py --full     # also re-download the full price history

Settings are kept in config.json next to this file. It holds your GitHub token: keep it private.
"""
import base64
import datetime as dt
import getpass
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
LOG = HERE / "update_log.txt"
DEFAULT_REPO = "tfridrich/commodities"
BRANCH = "main"
SCRIPT = "scripts/update_oil_gas.py"
DATA = ["docs/data/oil_gas.json", "docs/data/oil_gas_curves.json"]
API = "https://api.github.com"


def log(msg):
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M}  {msg}"
    try:
        print(line)
    except Exception:
        pass          # pythonw has no console
    lines = LOG.read_text(encoding="utf-8").splitlines() if LOG.exists() else []
    lines = (lines + line.splitlines())[-800:]      # keep the log short
    LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")


def gh(cfg, method, path, body=None, raw=False):
    req = urllib.request.Request(API + path, method=method,
                                 data=None if body is None else json.dumps(body).encode())
    req.add_header("Authorization", "Bearer " + cfg["github_token"])
    req.add_header("Accept", "application/vnd.github.raw+json" if raw else "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "commodities-pc-updater")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    return data if raw else json.loads(data or b"{}")


def run_once(cfg, full):
    repo = cfg.get("repo", DEFAULT_REPO)
    head = gh(cfg, "GET", f"/repos/{repo}/git/ref/heads/{BRANCH}")["object"]["sha"]
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        before = {}
        for path in [SCRIPT] + DATA:
            p = tmp / path
            p.parent.mkdir(parents=True, exist_ok=True)
            try:
                p.write_bytes(gh(cfg, "GET", f"/repos/{repo}/contents/{path}?ref={head}", raw=True))
            except urllib.error.HTTPError as e:
                if e.code != 404 or path == SCRIPT:
                    raise
            before[path] = p.read_bytes() if p.exists() else None

        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        if cfg.get("eia_api_key"):
            env["EIA_API_KEY"] = cfg["eia_api_key"]
        cmd = [sys.executable, str(tmp / SCRIPT)] + (["--full"] if full else [])
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        res = subprocess.run(cmd, cwd=tmp, env=env, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=1500, creationflags=flags)
        for line in (res.stdout + res.stderr).splitlines():
            if line.strip():
                log("   " + line.replace("::warning::", "warning: ").replace("::error::", "error: "))
        if res.returncode != 0:
            log(f"The update script stopped with exit code {res.returncode}.")

        changed = [p for p in DATA if (tmp / p).exists() and (tmp / p).read_bytes() != before[p]]
        if not changed:
            log("Nothing new to upload.")
            return True

        tree = []
        for p in changed:
            blob = gh(cfg, "POST", f"/repos/{repo}/git/blobs",
                      {"content": base64.b64encode((tmp / p).read_bytes()).decode(), "encoding": "base64"})
            tree.append({"path": p, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        base_tree = gh(cfg, "GET", f"/repos/{repo}/git/commits/{head}")["tree"]["sha"]
        new_tree = gh(cfg, "POST", f"/repos/{repo}/git/trees", {"base_tree": base_tree, "tree": tree})["sha"]
        commit = gh(cfg, "POST", f"/repos/{repo}/git/commits", {
            "message": f"Update oil and gas prices from PC {dt.date.today():%Y-%m-%d}",
            "tree": new_tree, "parents": [head]})["sha"]
        try:
            gh(cfg, "PATCH", f"/repos/{repo}/git/refs/heads/{BRANCH}", {"sha": commit, "force": False})
        except urllib.error.HTTPError as e:
            if e.code == 422:          # GitHub pushed something meanwhile - start again
                log("GitHub changed while this ran; trying again.")
                return False
            raise
        log(f"Uploaded {', '.join(Path(p).name for p in changed)} - the website updates in a few minutes.")
        return True


def setup():
    cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    print("\nGitHub token (fine-grained, repository 'commodities', Contents: Read and write).")
    print("Paste it and press Enter (the text stays hidden)" + (", or just Enter to keep the saved one" if cfg.get("github_token") else "") + ":")
    tok = getpass.getpass("> ").strip()
    if tok:
        cfg["github_token"] = tok
    if not cfg.get("github_token"):
        sys.exit("No token given - setup stopped.")
    print("\nOptional: EIA API key (free from https://www.eia.gov/opendata/register.php).")
    print("Press Enter to skip" + (" / keep the saved one" if cfg.get("eia_api_key") else "") + ":")
    key = input("> ").strip()
    if key:
        cfg["eia_api_key"] = key
    cfg.setdefault("repo", DEFAULT_REPO)
    try:
        info = gh(cfg, "GET", f"/repos/{cfg['repo']}")
        if not info.get("permissions", {}).get("push"):
            sys.exit("The token can read the repository but not write to it. "
                     "Give it 'Contents: Read and write' and run setup again.")
    except urllib.error.HTTPError as e:
        sys.exit(f"GitHub did not accept the token ({e.code}). Check it and run setup again.")
    CONFIG.write_text(json.dumps(cfg, indent=2))
    print("\nSaved. Test run (can take a few minutes the first time)...\n")


def main():
    full = "--full" in sys.argv
    if "--setup" in sys.argv:
        setup()
    if not CONFIG.exists():
        sys.exit("Run setup first (double-click setup.bat).")
    cfg = json.loads(CONFIG.read_text())
    log("Oil & gas update started.")
    try:
        for _ in range(2):
            if run_once(cfg, full):
                break
    except urllib.error.HTTPError as e:
        log(f"GitHub error {e.code}: {e.reason}" + (" - the token may have expired; run setup.bat again." if e.code in (401, 403) else ""))
        sys.exit(1)
    except Exception as e:
        log(f"Failed: {e!r}")
        sys.exit(1)


if __name__ == "__main__":
    main()
