#!/usr/bin/env python3
"""
Compare latest and previous Maven JAR versions after Vineflower decompilation.

Example:
  python compare_maven_jars.py \
    --base-url https://maven.hytale.com/pre-release/ \
    --vineflower ./vineflower.jar \
    --out ./hytale-diff
"""

from __future__ import annotations

import argparse
import difflib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class Artifact:
    base_url: str
    group_id: str
    artifact_id: str
    versions: list[str]


@dataclass(frozen=True)
class JarPair:
    artifact: Artifact
    previous_version: str
    latest_version: str
    previous_jar: Path
    latest_jar: Path


def normalize_url(url: str) -> str:
    return url if url.endswith("/") else url + "/"


def fetch_text(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "maven-jar-diff/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def download_file(url: str, dest: Path, timeout: int = 60) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "maven-jar-diff/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp, dest.open("wb") as f:
        shutil.copyfileobj(resp, f)


VINEFLOWER_RELEASES_API = (
    "https://api.github.com/repos/Vineflower/vineflower/releases/latest"
)


def download_vineflower(dest: Path) -> None:
    """
    Download the latest Vineflower release JAR from GitHub if it is missing.
    """
    print(f"[vineflower] fetching latest release info from GitHub …")
    req = urllib.request.Request(
        VINEFLOWER_RELEASES_API,
        headers={
            "User-Agent": "maven-jar-diff/1.0",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        release = json.loads(resp.read().decode("utf-8"))

    assets = release.get("assets", [])
    jar_assets = [a for a in assets if a.get("name", "").endswith(".jar")]
    if not jar_assets:
        raise RuntimeError(
            "No JAR asset found in the latest Vineflower GitHub release. "
            "Download it manually from https://github.com/Vineflower/vineflower/releases "
            f"and place it at: {dest}"
        )

    asset = jar_assets[0]
    tag = release.get("tag_name", "unknown")
    print(f"[vineflower] downloading {asset['name']} (release {tag}) …")
    download_file(asset["browser_download_url"], dest, timeout=120)
    print(f"[vineflower] saved to {dest}")


def ensure_vineflower(path: Path) -> None:
    """Download Vineflower automatically when the JAR is absent."""
    if not path.exists():
        print(f"[vineflower] JAR not found at {path} — attempting auto-download.")
        try:
            download_vineflower(path)
        except Exception as exc:
            print(
                f"[vineflower] auto-download failed: {exc}\n"
                "Please download vineflower.jar manually from "
                "https://github.com/Vineflower/vineflower/releases",
                file=sys.stderr,
            )
            sys.exit(2)


def list_directory_links(url: str) -> list[str]:
    """
    Parse a basic Maven/Nginx/Apache-style directory listing.

    This requires directory listing to be enabled. If the repository does not
    expose listings, pass a direct artifact URL instead.
    """
    page = fetch_text(url)
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', page, flags=re.I)
    links: list[str] = []

    for href in hrefs:
        if href.startswith("?") or href.startswith("#"):
            continue
        if href in ("../", "/"):
            continue

        full = urllib.parse.urljoin(url, href)
        if full.startswith(url):
            links.append(full)

    return sorted(set(links))


def parse_metadata(metadata_url: str) -> Artifact | None:
    xml_text = fetch_text(metadata_url)
    root = ET.fromstring(xml_text)

    def find_text(name: str) -> str | None:
        elem = root.find(f".//{name}")
        return elem.text.strip() if elem is not None and elem.text else None

    group_id = find_text("groupId")
    artifact_id = find_text("artifactId")

    versions = [
        elem.text.strip()
        for elem in root.findall(".//versioning/versions/version")
        if elem.text and elem.text.strip()
    ]

    if not group_id or not artifact_id or len(versions) < 2:
        return None

    artifact_base_url = metadata_url.rsplit("/", 1)[0] + "/"
    return Artifact(
        base_url=artifact_base_url,
        group_id=group_id,
        artifact_id=artifact_id,
        versions=versions,
    )


def discover_artifacts(base_url: str, max_depth: int = 12) -> list[Artifact]:
    """
    Recursively discover artifact directories by finding maven-metadata.xml.

    Works when the Maven repository exposes directory listings.
    """
    base_url = normalize_url(base_url)
    seen: set[str] = set()
    artifacts: list[Artifact] = []

    def walk(url: str, depth: int) -> None:
        if depth > max_depth or url in seen:
            return
        seen.add(url)

        metadata_url = urllib.parse.urljoin(url, "maven-metadata.xml")
        try:
            artifact = parse_metadata(metadata_url)
            if artifact:
                artifacts.append(artifact)
                return
        except Exception:
            pass

        try:
            links = list_directory_links(url)
        except Exception:
            return

        for link in links:
            if link.endswith("/"):
                walk(link, depth + 1)

    walk(base_url, 0)
    return artifacts


def load_artifact_from_direct_url(base_url: str) -> Artifact:
    """
    Try treating --base-url as a direct artifact directory.
    """
    metadata_url = urllib.parse.urljoin(normalize_url(base_url), "maven-metadata.xml")
    artifact = parse_metadata(metadata_url)
    if not artifact:
        raise RuntimeError(f"No usable Maven artifact metadata found at {metadata_url}")
    return artifact


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_")


def pick_latest_two(artifact: Artifact) -> tuple[str, str]:
    """
    Maven metadata versions are usually already in publish order.
    For pre-release repos, preserving metadata order is usually safer than
    trying to invent custom version sorting.
    """
    versions = [v for v in artifact.versions if v]
    if len(versions) < 2:
        raise ValueError(f"{artifact.artifact_id} has fewer than two versions")

    return versions[-2], versions[-1]


def jar_url_for(artifact: Artifact, version: str) -> str:
    jar_name = f"{artifact.artifact_id}-{version}.jar"
    return urllib.parse.urljoin(artifact.base_url, f"{version}/{jar_name}")


def download_pair(artifact: Artifact, work_dir: Path) -> JarPair:
    previous_version, latest_version = pick_latest_two(artifact)

    artifact_dir = work_dir / safe_name(f"{artifact.group_id}.{artifact.artifact_id}")
    jars_dir = artifact_dir / "jars"

    previous_jar = jars_dir / f"{artifact.artifact_id}-{previous_version}.jar"
    latest_jar = jars_dir / f"{artifact.artifact_id}-{latest_version}.jar"

    previous_url = jar_url_for(artifact, previous_version)
    latest_url = jar_url_for(artifact, latest_version)

    print(f"[download] {artifact.artifact_id} {previous_version}")
    download_file(previous_url, previous_jar)

    print(f"[download] {artifact.artifact_id} {latest_version}")
    download_file(latest_url, latest_jar)

    return JarPair(
        artifact=artifact,
        previous_version=previous_version,
        latest_version=latest_version,
        previous_jar=previous_jar,
        latest_jar=latest_jar,
    )


def run_vineflower(vineflower_jar: Path, source_jar: Path, output_dir: Path, java_bin: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        java_bin,
        "-jar",
        str(vineflower_jar),
        str(source_jar),
        str(output_dir),
    ]

    print(f"[decompile] {source_jar.name}")
    subprocess.run(cmd, check=True)


def collect_files(root: Path, include_prefixes: list[str] | None = None) -> dict[str, Path]:
    """
    Collect decompiled files, optionally limited to package/path prefixes.

    Vineflower emits Java package paths like:
      com/hypixel/hytale/SomeClass.java

    Passing include_prefixes=["com/hypixel/hytale/"] excludes bundled libraries
    such as org/, net/, io/, kotlin/, etc. from the generated diff.
    """
    normalized_prefixes = None
    if include_prefixes:
        normalized_prefixes = [
            prefix.replace(".", "/").strip("/") + "/"
            for prefix in include_prefixes
            if prefix.strip()
        ]

    files: dict[str, Path] = {}
    for path in root.rglob("*"):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            if normalized_prefixes and not any(rel.startswith(prefix) for prefix in normalized_prefixes):
                continue
            files[rel] = path
    return files


def read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []


def make_file_diff_html(
    rel_path: str,
    old_path: Path | None,
    new_path: Path | None,
    old_label: str,
    new_label: str,
) -> tuple[str, bool]:
    old_lines = read_lines(old_path) if old_path else []
    new_lines = read_lines(new_path) if new_path else []

    changed = old_lines != new_lines

    table = difflib.HtmlDiff(tabsize=4, wrapcolumn=120).make_table(
        old_lines,
        new_lines,
        fromdesc=html.escape(old_label),
        todesc=html.escape(new_label),
        context=True,
        numlines=3,
    )

    status = "changed"
    if old_path is None:
        status = "added"
    elif new_path is None:
        status = "removed"
    elif not changed:
        status = "unchanged"

    return f"""
<section class="file-block {status}">
  <h2 id="{html.escape(rel_path)}">
    <span class="status">{status}</span>
    <code>{html.escape(rel_path)}</code>
  </h2>
  {table}
</section>
""", changed or old_path is None or new_path is None


def generate_html_report(
    pair: JarPair,
    old_src: Path,
    new_src: Path,
    report_path: Path,
    include_unchanged: bool = False,
    include_prefixes: list[str] | None = None,
) -> int:
    old_files = collect_files(old_src, include_prefixes=include_prefixes)
    new_files = collect_files(new_src, include_prefixes=include_prefixes)

    all_paths = sorted(set(old_files) | set(new_files))
    sections: list[str] = []
    changed_count = 0

    old_label = f"{pair.artifact.artifact_id} {pair.previous_version}"
    new_label = f"{pair.artifact.artifact_id} {pair.latest_version}"

    nav_items: list[str] = []

    for rel_path in all_paths:
        section, changed = make_file_diff_html(
            rel_path=rel_path,
            old_path=old_files.get(rel_path),
            new_path=new_files.get(rel_path),
            old_label=old_label,
            new_label=new_label,
        )

        if changed:
            changed_count += 1
            nav_items.append(
                f'<li><a href="#{html.escape(rel_path)}">{html.escape(rel_path)}</a></li>'
            )

        if changed or include_unchanged:
            sections.append(section)

    css = """
body {
  font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  margin: 0;
  background: #f7f7f8;
  color: #1f2328;
}
header {
  position: sticky;
  top: 0;
  z-index: 10;
  background: #ffffff;
  border-bottom: 1px solid #d0d7de;
  padding: 16px 24px;
}
main {
  padding: 24px;
}
h1 {
  margin: 0 0 8px;
  font-size: 22px;
}
.summary {
  color: #57606a;
}
.layout {
  display: grid;
  grid-template-columns: 320px minmax(0, 1fr);
  gap: 24px;
}
nav {
  position: sticky;
  top: 96px;
  align-self: start;
  max-height: calc(100vh - 120px);
  overflow: auto;
  background: #ffffff;
  border: 1px solid #d0d7de;
  border-radius: 10px;
  padding: 16px;
}
nav ul {
  padding-left: 20px;
}
nav li {
  margin: 6px 0;
  overflow-wrap: anywhere;
}
.file-block {
  background: #ffffff;
  border: 1px solid #d0d7de;
  border-radius: 10px;
  margin-bottom: 24px;
  overflow: auto;
}
.file-block h2 {
  margin: 0;
  padding: 12px 16px;
  border-bottom: 1px solid #d0d7de;
  font-size: 15px;
  background: #f6f8fa;
}
.status {
  display: inline-block;
  min-width: 78px;
  margin-right: 8px;
  font-weight: 700;
}
.added .status { color: #1a7f37; }
.removed .status { color: #cf222e; }
.changed .status { color: #9a6700; }

table.diff {
  width: 100%;
  border-collapse: collapse;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
}
.diff_header {
  background: #f6f8fa;
  color: #57606a;
}
td {
  vertical-align: top;
  padding: 2px 6px;
  white-space: pre-wrap;
}
.diff_next {
  display: none;
}
.diff_add {
  background: #dafbe1;
}
.diff_chg {
  background: #fff8c5;
}
.diff_sub {
  background: #ffebe9;
}
@media (max-width: 1000px) {
  .layout {
    grid-template-columns: 1fr;
  }
  nav {
    position: static;
  }
}
"""

    if not nav_items:
        nav_html = "<p>No changed files found.</p>"
    else:
        nav_html = "<ul>" + "\n".join(nav_items) + "</ul>"

    html_doc = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{html.escape(pair.artifact.artifact_id)} diff</title>
  <style>{css}</style>
</head>
<body>
  <header>
    <h1>{html.escape(pair.artifact.group_id)}:{html.escape(pair.artifact.artifact_id)}</h1>
    <div class="summary">
      Comparing {html.escape(pair.previous_version)} → {html.escape(pair.latest_version)}.
      Changed/added/removed files: {changed_count}.
      {"Included paths: " + html.escape(", ".join(include_prefixes)) + "." if include_prefixes else "Included paths: all decompiled files."}
    </div>
  </header>
  <main class="layout">
    <nav>
      <strong>Changed files</strong>
      {nav_html}
    </nav>
    <div>
      {''.join(sections)}
    </div>
  </main>
</body>
</html>
"""

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(html_doc, encoding="utf-8")
    return changed_count


def process_artifact(
    artifact: Artifact,
    args: argparse.Namespace,
    work_dir: Path,
    reports_dir: Path,
) -> Path:
    pair = download_pair(artifact, work_dir)

    artifact_key = safe_name(f"{artifact.group_id}.{artifact.artifact_id}")
    decompiled_base = work_dir / artifact_key / "decompiled"

    old_src = decompiled_base / pair.previous_version
    new_src = decompiled_base / pair.latest_version

    if args.clean and old_src.exists():
        shutil.rmtree(old_src)
    if args.clean and new_src.exists():
        shutil.rmtree(new_src)

    if not old_src.exists():
        run_vineflower(args.vineflower, pair.previous_jar, old_src, args.java)
    else:
        print(f"[skip] already decompiled {pair.previous_version}")

    if not new_src.exists():
        run_vineflower(args.vineflower, pair.latest_jar, new_src, args.java)
    else:
        print(f"[skip] already decompiled {pair.latest_version}")

    report_path = reports_dir / f"{artifact_key}-{pair.previous_version}-to-{pair.latest_version}.html"

    changed_count = generate_html_report(
        pair=pair,
        old_src=old_src,
        new_src=new_src,
        report_path=report_path,
        include_unchanged=args.include_unchanged,
        include_prefixes=args.include_prefix,
    )

    print(f"[report] {report_path} ({changed_count} changed/added/removed files)")
    return report_path


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download the latest and previous Maven JARs, decompile them with Vineflower, and generate HTML diffs."
    )
    parser.add_argument(
        "--base-url",
        default="https://maven.hytale.com/pre-release/com/hypixel/hytale/Server/",
        help="Maven repository URL or direct artifact directory URL.",
    )
    parser.add_argument(
        "--vineflower",
        default=Path("./vineflower.jar"),
        type=Path,
        help="Path to vineflower.jar.",
    )
    parser.add_argument(
        "--out",
        default=Path("./hytale-diff"),
        type=Path,
        help="Output directory.",
    )
    parser.add_argument(
        "--java",
        default="java",
        help="Java executable to use. Defaults to 'java'.",
    )
    parser.add_argument(
        "--artifact-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Treat --base-url as a direct artifact directory instead of crawling.",
    )
    parser.add_argument(
        "--include-prefix",
        action="append",
        default=["com.hypixel.hytale"],
        help=(
            "Only include decompiled files under this package/path prefix. "
            "Can be passed multiple times. Use --include-prefix '' to disable filtering. "
            "Default: com.hypixel.hytale"
        ),
    )
    parser.add_argument(
        "--max-artifacts",
        type=int,
        default=0,
        help="Limit number of discovered artifacts. 0 means no limit.",
    )
    parser.add_argument(
        "--include-unchanged",
        action="store_true",
        help="Include unchanged files in the HTML report.",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Delete existing decompiled folders before running Vineflower.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    args.base_url = normalize_url(args.base_url)
    args.include_prefix = [prefix for prefix in args.include_prefix if prefix.strip()]

    ensure_vineflower(args.vineflower)

    work_dir = args.out / "work"
    reports_dir = args.out / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    if args.artifact_only:
        artifacts = [load_artifact_from_direct_url(args.base_url)]
    else:
        print(f"[discover] crawling {args.base_url}")
        artifacts = discover_artifacts(args.base_url)

        if not artifacts:
            print(
                "No artifacts found by crawling. If directory listing is disabled, "
                "rerun with --artifact-only and pass the direct artifact directory URL.",
                file=sys.stderr,
            )
            return 1

    if args.max_artifacts > 0:
        artifacts = artifacts[: args.max_artifacts]

    print(f"[discover] found {len(artifacts)} artifact(s)")

    report_paths: list[Path] = []

    for artifact in artifacts:
        if len(artifact.versions) < 2:
            print(f"[skip] {artifact.artifact_id}: fewer than two versions")
            continue

        try:
            report_paths.append(process_artifact(artifact, args, work_dir, reports_dir))
        except Exception as exc:
            print(
                f"[error] failed {artifact.group_id}:{artifact.artifact_id}: {exc}",
                file=sys.stderr,
            )

    index_path = args.out / "index.html"

    index_css = """
    :root {
      color-scheme: light dark;
      --bg: #0f172a;
      --panel: #111827;
      --panel-2: #1f2937;
      --text: #e5e7eb;
      --muted: #9ca3af;
      --border: #374151;
      --accent: #60a5fa;
      --accent-2: #93c5fd;
      --shadow: 0 20px 45px rgba(0, 0, 0, 0.25);
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      min-height: 100vh;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at top left, rgba(96, 165, 250, 0.25), transparent 36rem),
        radial-gradient(circle at bottom right, rgba(147, 197, 253, 0.14), transparent 32rem),
        var(--bg);
      color: var(--text);
    }

    main {
      width: min(1100px, calc(100% - 32px));
      margin: 0 auto;
      padding: 56px 0;
    }

    .hero {
      margin-bottom: 28px;
    }

    .eyebrow {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 6px 10px;
      margin-bottom: 16px;
      border: 1px solid rgba(96, 165, 250, 0.35);
      border-radius: 999px;
      color: var(--accent-2);
      background: rgba(96, 165, 250, 0.08);
      font-size: 13px;
      font-weight: 700;
      letter-spacing: 0.02em;
    }

    h1 {
      margin: 0;
      font-size: clamp(34px, 6vw, 64px);
      line-height: 1;
      letter-spacing: -0.055em;
    }

    .subtitle {
      max-width: 720px;
      margin: 16px 0 0;
      color: var(--muted);
      font-size: 17px;
      line-height: 1.6;
    }

    .stats {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 16px;
      margin: 28px 0;
    }

    .stat {
      padding: 18px;
      border: 1px solid var(--border);
      border-radius: 18px;
      background: rgba(17, 24, 39, 0.78);
      box-shadow: var(--shadow);
    }

    .stat strong {
      display: block;
      font-size: 32px;
      line-height: 1;
    }

    .stat span {
      display: block;
      margin-top: 8px;
      color: var(--muted);
      font-size: 14px;
    }

    .card {
      overflow: hidden;
      border: 1px solid var(--border);
      border-radius: 22px;
      background: rgba(17, 24, 39, 0.86);
      box-shadow: var(--shadow);
    }

    .card-header {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: center;
      padding: 18px 20px;
      border-bottom: 1px solid var(--border);
      background: rgba(31, 41, 55, 0.65);
    }

    .card-header h2 {
      margin: 0;
      font-size: 18px;
    }

    .card-header span {
      color: var(--muted);
      font-size: 14px;
    }

    .report-list {
      display: grid;
      gap: 0;
      margin: 0;
      padding: 0;
      list-style: none;
    }

    .report-list li + li {
      border-top: 1px solid var(--border);
    }

    .report-list a {
      display: flex;
      justify-content: space-between;
      gap: 18px;
      align-items: center;
      padding: 18px 20px;
      color: var(--text);
      text-decoration: none;
      transition: background 160ms ease, color 160ms ease;
    }

    .report-list a:hover {
      color: white;
      background: rgba(96, 165, 250, 0.12);
    }

    .report-name {
      overflow-wrap: anywhere;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 14px;
    }

    .report-action {
      flex: 0 0 auto;
      color: var(--accent-2);
      font-weight: 700;
    }

    .empty {
      padding: 28px 20px;
      color: var(--muted);
    }

    footer {
      margin-top: 28px;
      color: var(--muted);
      font-size: 13px;
    }
    """

    report_items = "\n".join(
        f"""<li>
          <a href="{html.escape(path.relative_to(args.out).as_posix())}">
            <span class="report-name">{html.escape(path.name)}</span>
            <span class="report-action">Open →</span>
          </a>
        </li>"""
        for path in report_paths
    )

    if report_items:
        reports_html = f'<ul class="report-list">{report_items}</ul>'
    else:
        reports_html = '<div class="empty">No reports were generated.</div>'

    index_path.write_text(
        f"""<!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Maven JAR diffs</title>
      <style>{index_css}</style>
    </head>
    <body>
      <main>
        <section class="hero">
          <div class="eyebrow">Maven diff index</div>
          <h1>Maven JAR diffs</h1>
          <p class="subtitle">
            Generated comparison reports for the latest Maven artifact versions.
            Open a report below to review changed, added, and removed decompiled files.
          </p>
        </section>

        <section class="stats">
          <div class="stat">
            <strong>{len(report_paths)}</strong>
            <span>Generated report{"s" if len(report_paths) != 1 else ""}</span>
          </div>
        </section>

        <section class="card">
          <div class="card-header">
            <h2>Reports</h2>
            <span>{len(report_paths)} total</span>
          </div>
          {reports_html}
        </section>

        <footer>
          Generated by compare_maven_jars.py
        </footer>
      </main>
    </body>
    </html>
    """,
        encoding="utf-8",
    )

    print(f"[index] {index_path}")
    return 0 if report_paths else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))