# Overview

Automatically downloads the two most recent versions of Hytale server JARs of a patchline, decompiles
them with [Vineflower](https://github.com/Vineflower/vineflower), and produces
side-by-side HTML diff reports so you can see exactly what changed between
releases.

---

## Requirements

| Dependency         | Notes                                                            |
|--------------------|------------------------------------------------------------------|
| **Python 3.10+**   | Uses `match`-style type hints; 3.11+ recommended                 |
| **Java 25+**       | Must be on your `PATH` as `java`, or pass `--java /path/to/java` |
| **vineflower.jar** | Downloaded automatically on first run (see below)                |

No third-party Python packages are required — the script uses only the standard
library.

---

## Setup

```bash
# 1. Clone or copy the script wherever you like
git clone <your-repo>   # or just copy compare_maven_jars.py

# 2. Verify Java is available
java -version

# 3. (Optional) Pre-download Vineflower
#    If you skip this step, the script downloads it automatically on first run.
curl -L -o vineflower.jar \
  https://github.com/Vineflower/vineflower/releases/latest/download/vineflower.jar
```

### Vineflower auto-download

If `vineflower.jar` is not present at the path given by `--vineflower` (default
`./vineflower.jar`), the script queries the GitHub Releases API for the latest
release and downloads the JAR automatically. It prints progress so you can see
what it is fetching.

If the download fails (e.g. no network access), the script prints an error with
the manual download URL and exits.

---

## Basic usage

```bash
# Compare the two most recent versions
python compare_maven_jars.py \
  --out ./hytale-diff
```

Open `./hytale-diff/index.html` in your browser when the script finishes.

---

## All options

```
usage: compare_maven_jars.py [-h]
    [--base-url URL]
    [--vineflower PATH]
    [--out PATH]
    [--java PATH]
    [--artifact-only | --no-artifact-only]
    [--include-prefix PREFIX]
    [--max-artifacts N]
    [--include-unchanged]
    [--clean]
```

| Flag                  | Default                      | Description                                                                                                                                                                   |
|-----------------------|------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `--base-url`          | Hytale pre-release Maven URL | Maven repository root **or** a direct artifact directory URL.                                                                                                                 |
| `--vineflower`        | `./vineflower.jar`           | Path to the Vineflower JAR. Downloaded automatically if absent.                                                                                                               |
| `--out`               | `./hytale-diff`              | Output directory for reports and intermediate files.                                                                                                                          |
| `--java`              | `java`                       | Java executable. Override if `java` is not on your `PATH`.                                                                                                                    |
| `--artifact-only`     | `true`                       | Treat `--base-url` as a direct artifact directory instead of crawling the whole repository tree. Use `--no-artifact-only` to crawl.                                           |
| `--include-prefix`    | `com.hypixel.hytale`         | Only include decompiled files under this Java package prefix. Pass multiple times for multiple prefixes. Pass `--include-prefix ''` to disable filtering and diff everything. |
| `--max-artifacts`     | `0` (no limit)               | Stop after processing this many artifacts. Useful for quick tests when crawling large repositories.                                                                           |
| `--include-unchanged` | off                          | Also emit unchanged files in the HTML report (makes it much larger).                                                                                                          |
| `--clean`             | off                          | Delete previously decompiled folders before running Vineflower, forcing a fresh decompile.                                                                                    |

---

## Examples

### Crawl a whole Maven repository

```bash
python compare_maven_jars.py \
  --no-artifact-only \
  --out ./my-diff
```

### Diff only a specific package

```bash
python compare_maven_jars.py \
  --base-url https://maven.hytale.com/pre-release/com/hypixel/hytale/Server/ \
  --include-prefix com.hypixel.hytale.network \
  --include-prefix com.hypixel.hytale.world \
  --out ./hytale-diff
```

### Use a custom Java binary and pre-downloaded Vineflower

```bash
python compare_maven_jars.py \
  --java /usr/lib/jvm/java-21-openjdk/bin/java \
  --vineflower /opt/tools/vineflower.jar \
  --out ./hytale-diff
```

### Force a clean re-decompile

```bash
python compare_maven_jars.py \
  --clean \
  --out ./hytale-diff
```

---

## Output structure

```
<out>/
├── index.html              ← open this in your browser
├── reports/
│   └── <group>.<artifact>-<prev>-to-<latest>.html
└── work/
    └── <group>.<artifact>/
        ├── jars/
        │   ├── <artifact>-<prev>.jar
        │   └── <artifact>-<latest>.jar
        └── decompiled/
            ├── <prev>/     ← decompiled Java sources
            └── <latest>/
```

Intermediate files (JARs, decompiled sources) are cached under `work/` so
subsequent runs skip already-decompiled versions. Use `--clean` to force
re-decompilation.

---

## Memory usage

Vineflower is invoked with `-Xmx4g`, capping the JVM heap at **4 GB**. This
covers even large JARs without risking out-of-memory errors on typical
developer machines. If you need to raise or lower the limit, edit the
`run_vineflower` function in the script and adjust the `-Xmx` value.

---

## Troubleshooting

**`vineflower auto-download failed`**  
The script could not reach GitHub. Download the JAR manually from
<https://github.com/Vineflower/vineflower/releases> and place it next to the
script (or pass `--vineflower /path/to/vineflower.jar`).

**`java: command not found`**  
Install a JDK (Java 25 or newer) or pass the full path with `--java`.