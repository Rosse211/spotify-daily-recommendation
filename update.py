#!/usr/bin/env python3
import io
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = "Rosse211/spotify-daily-recommendation"
LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
STAMP = ROOT / "data" / "version.json"
VERSION = "v1.1"  # bump on every release: it is what a plain zip unpack reports
FOLDERS = ("src", "pages", "docs")
FILES = ("README.md", "LICENSE", ".gitignore", "update.py",
         "Daily recommendation.bat", "Daily recommendation.command")


def fetch(url):
    return urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": REPO}), timeout=30)


def latest():
    return json.load(fetch(LATEST))


def installed():
    if STAMP.exists():
        try:
            return json.loads(STAMP.read_text(encoding="utf-8")).get("tag") or VERSION
        except ValueError:
            pass
    return VERSION


def cloned():
    return (ROOT / ".git").exists()


def download(url, on_progress=None):
    with fetch(url) as r:
        total = int(r.headers.get("Content-Length") or 0)
        parts, got = [], 0
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            parts.append(chunk)
            got += len(chunk)
            if on_progress:
                on_progress(got, total)
    return b"".join(parts)


def unpack(blob, into):
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        z.extractall(into)
    roots = [p for p in into.iterdir() if p.is_dir()]
    if len(roots) != 1 or not (roots[0] / "src" / "app.py").exists():
        raise RuntimeError("That archive does not look like the app, nothing was changed.")
    return roots[0]


def install(blob, tag):
    with tempfile.TemporaryDirectory() as tmp:
        top = unpack(blob, Path(tmp))
        for name in FOLDERS:
            if (top / name).is_dir():
                shutil.rmtree(ROOT / name, ignore_errors=True)
                shutil.copytree(top / name, ROOT / name)
        for name in FILES:
            if (top / name).is_file():
                shutil.copy2(top / name, ROOT / name)
    STAMP.parent.mkdir(exist_ok=True)
    STAMP.write_text(json.dumps({"tag": tag}), encoding="utf-8")


def apply(release, on_progress=None):
    install(download(release["zipball_url"], on_progress), release["tag_name"])


def pull():
    print("This is a git clone, pulling instead of downloading.")
    try:
        done = subprocess.run(["git", "-C", str(ROOT), "pull", "--ff-only"],
                              capture_output=True, text=True)
    except FileNotFoundError:
        sys.exit("git is not installed. Pass --anyway to replace the files instead.")
    print((done.stdout + done.stderr).strip() or "Nothing to pull.")
    if done.returncode:
        print("Pass --anyway to replace the files instead.")
    sys.exit(done.returncode)


def main():
    if cloned() and "--anyway" not in sys.argv:
        pull()
    print("Looking for a new release...")
    try:
        release = latest()
    except Exception as e:
        sys.exit(f"Could not reach GitHub: {e}")
    tag, here = release.get("tag_name"), installed()
    print(f"  you have: {here or 'unknown'}")
    print(f"  latest:   {tag}")
    if tag == here and "--force" not in sys.argv:
        return print("Already up to date.")
    if "--yes" not in sys.argv:
        answer = input("Replace the app files? Your data will be kept [Y/n] ")
        if answer.strip().lower() in ("n", "no"):
            return print("Nothing changed.")
    try:
        apply(release)
    except Exception as e:
        sys.exit(str(e))
    print(f"Updated to {tag}. Start the app again.")


if __name__ == "__main__":
    main()
