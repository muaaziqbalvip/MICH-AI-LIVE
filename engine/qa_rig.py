"""
qa_rig.py
─────────────────────────────────────────────────────────────────────────────
Self-Healing QA Testing Rig

Pipeline:
  1. Detect project type from directory structure.
  2. Run the appropriate build command in an isolated subprocess.
  3. If build succeeds → return success.
  4. If build fails → parse stderr/stdout for error location.
  5. Send error context to Groq → get corrected file content.
  6. Overwrite the faulty file → re-run build.
  7. Repeat up to MAX_HEAL_ITERATIONS times.
  8. On persistent failure → send full error log to Telegram.

Supported build toolchains:
  • Android   – ./gradlew assembleDebug (requires JDK 17+)
  • Web React  – npm run build (Vite)
  • Web Next   – npm run build (Next.js)
  • FastAPI    – python -m py_compile + pytest
  • Flutter    – flutter build apk --debug
  • Script     – python -m py_compile
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from pathlib import Path

from engine.api_pool import GroqPool
from engine.telegram_bot import notify

logger = logging.getLogger("qa_rig")

MAX_HEAL_ITERATIONS = 5
BUILD_TIMEOUT = 600  # 10 minutes per build attempt


# ── Project type detection ────────────────────────────────────────────────────

def _detect_type(project_dir: Path) -> str:
    """Heuristically detect the project type from its file structure."""
    files = {f.name for f in project_dir.rglob("*") if f.is_file()}
    names = {f.name for f in project_dir.iterdir()}

    if "settings.gradle" in files or "build.gradle" in files:
        return "android"
    if "pubspec.yaml" in files:
        return "flutter"
    if "next.config.js" in files or "next.config.ts" in files:
        return "web_next"
    if "vite.config.ts" in files or "vite.config.js" in files:
        return "web_react"
    if any("fastapi" in f.lower() or "uvicorn" in f.lower()
           for f in files if f.endswith(".txt")):
        return "fastapi"
    if "package.json" in names:
        return "web_react"
    if any(f.endswith(".py") for f in files):
        return "script"
    return "unknown"


# ── Build commands ────────────────────────────────────────────────────────────

def _get_build_cmd(project_type: str, project_dir: Path) -> list[str] | None:
    """Return the build command list for a given project type."""
    cmd_map = {
        "android": ["./gradlew", "assembleDebug", "--no-daemon"],
        "flutter": ["flutter", "build", "apk", "--debug"],
        "web_react": ["npm", "run", "build"],
        "web_next": ["npm", "run", "build"],
        "fastapi": ["python", "-m", "pytest", "--tb=short", "-q"],
        "script": None,  # handled separately
    }
    return cmd_map.get(project_type)


def _get_install_cmd(project_type: str) -> list[str] | None:
    """Return the dependency install command, if applicable."""
    if project_type in ("web_react", "web_next"):
        return ["npm", "install", "--legacy-peer-deps"]
    if project_type == "fastapi":
        return ["pip", "install", "-r", "requirements.txt", "--quiet"]
    if project_type == "android":
        return None  # Gradle handles dependencies
    return None


def _get_python_files(project_dir: Path) -> list[Path]:
    return list(project_dir.rglob("*.py"))


# ── Subprocess runner ─────────────────────────────────────────────────────────

def _run_cmd(
    cmd: list[str],
    cwd: Path,
    timeout: int = BUILD_TIMEOUT,
) -> tuple[int, str, str]:
    """
    Run a command in cwd.
    Returns (return_code, stdout, stderr).
    """
    logger.info("[QA] Running: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Build timed out after {timeout}s"
    except FileNotFoundError:
        return -2, "", f"Command not found: {cmd[0]}"
    except Exception as exc:
        return -3, "", str(exc)


# ── Error parser ──────────────────────────────────────────────────────────────

def _parse_errors(
    stdout: str,
    stderr: str,
    project_type: str,
    project_dir: Path,
) -> list[dict]:
    """
    Extract structured error info: file path + line + message.
    Returns list of {file, line, message, snippet} dicts.
    """
    combined = (stdout + "\n" + stderr)
    errors: list[dict] = []

    if project_type == "android":
        # e.g. "e: /path/to/File.kt: (42, 10): Unresolved reference: Foo"
        pattern = re.compile(
            r"e: (.+\.kt):?\s*\(?(\d+)[,:]?\s*(\d+)?\)?:?\s*(.+)"
        )
        for match in pattern.finditer(combined):
            file_path = match.group(1).strip()
            line = int(match.group(2))
            message = match.group(4).strip()

            # Try to get file snippet
            snippet = _get_file_snippet(file_path, line)
            errors.append(
                {"file": file_path, "line": line, "message": message, "snippet": snippet}
            )

    elif project_type in ("web_react", "web_next"):
        # TypeScript / Vite errors: "src/Component.tsx:42:10 - error TS…"
        pattern = re.compile(r"([^\s]+\.[tj]sx?):(\d+):(\d+)\s*[-–]\s*(.+)")
        for match in pattern.finditer(combined):
            rel_path = match.group(1).strip()
            line = int(match.group(2))
            message = match.group(4).strip()
            abs_path = str(project_dir / rel_path)
            snippet = _get_file_snippet(abs_path, line)
            errors.append(
                {"file": abs_path, "line": line, "message": message, "snippet": snippet}
            )

    elif project_type in ("fastapi", "script"):
        # Python: "File "path.py", line 42, in …"
        pattern = re.compile(r'File "(.+\.py)", line (\d+)')
        for match in pattern.finditer(combined):
            file_path = match.group(1).strip()
            line = int(match.group(2))
            # Get next line for the actual error message
            idx = combined.find(match.group(0))
            message = combined[idx + len(match.group(0)):].split("\n")[1:3]
            message_str = " ".join(m.strip() for m in message)
            snippet = _get_file_snippet(file_path, line)
            errors.append(
                {"file": file_path, "line": line, "message": message_str, "snippet": snippet}
            )

    # Deduplicate by file+line
    seen = set()
    unique: list[dict] = []
    for e in errors:
        key = (e["file"], e["line"])
        if key not in seen:
            seen.add(key)
            unique.append(e)

    return unique[:10]  # cap at 10 errors per iteration


def _get_file_snippet(file_path: str, line: int, context: int = 10) -> str:
    """Return lines around the error with line numbers."""
    try:
        path = Path(file_path)
        if not path.exists():
            return ""
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(0, line - context - 1)
        end = min(len(lines), line + context)
        numbered = [
            f"{i + 1}{'>>>' if i + 1 == line else '   '} {l}"
            for i, l in enumerate(lines[start:end], start=start)
        ]
        return "\n".join(numbered)
    except Exception:
        return ""


# ── AI healer ─────────────────────────────────────────────────────────────────

_HEAL_SYSTEM = """You are an expert debugger and code fixer.
You will be given a source file with compile/runtime errors.
You must return the COMPLETE, FIXED file contents.

Rules:
1. Fix EVERY error indicated. Do not introduce new errors.
2. Return ONLY the raw file contents. No markdown fences. No explanation.
3. Keep all existing functionality intact – only fix the errors.
4. Ensure all imports are correct and present.
5. The file must compile/run successfully after your fix."""


def _heal_file(
    file_path: str,
    errors: list[dict],
    pool: GroqPool,
    project_type: str,
) -> str | None:
    """
    Ask Groq to produce a fixed version of the file.
    Returns the fixed file contents, or None on failure.
    """
    try:
        current_content = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    error_descriptions = "\n".join(
        f"Line {e['line']}: {e['message']}" for e in errors
    )

    snippet = errors[0].get("snippet", "") if errors else ""

    prompt = f"""File: {file_path}
Project type: {project_type}

Errors to fix:
{error_descriptions}

Error context (surrounding lines):
{snippet}

Current file contents:
{current_content[:8000]}

Return the complete fixed file."""

    try:
        return pool.chat(
            messages=[{"role": "user", "content": prompt}],
            system=_HEAL_SYSTEM,
            max_tokens=4096,
            temperature=0.1,
        )
    except Exception as exc:
        logger.error("[QA] Heal call failed: %s", exc)
        return None


# ── Main QA rig class ─────────────────────────────────────────────────────────


class QaRig:
    """
    Runs the self-healing build loop on a generated project directory.
    """

    def __init__(self, pool: GroqPool) -> None:
        self._pool = pool

    def run(self, project_dir: Path, job_id: str) -> dict:
        """
        Execute the full QA pipeline.

        Returns:
        {
          "success": bool,
          "iterations": int,
          "errors_fixed": int,
          "final_output": str,
          "artifact_path": Path | None
        }
        """
        project_type = _detect_type(project_dir)
        logger.info("[QA] Detected project type: %s", project_type)
        notify(f"🔨 QA started | job={job_id} | type={project_type}")

        # ── Install dependencies ───────────────────────────────────────────
        install_cmd = _get_install_cmd(project_type)
        if install_cmd:
            logger.info("[QA] Installing dependencies: %s", " ".join(install_cmd))
            rc, out, err = _run_cmd(install_cmd, project_dir, timeout=300)
            if rc != 0:
                logger.warning("[QA] Dependency install failed (rc=%d): %s", rc, err[:200])

        # ── Handle Python script type (syntax check only) ──────────────────
        if project_type == "script":
            return self._check_python_scripts(project_dir, job_id)

        build_cmd = _get_build_cmd(project_type, project_dir)
        if not build_cmd:
            logger.warning("[QA] No build command for type: %s", project_type)
            return {
                "success": True,
                "iterations": 0,
                "errors_fixed": 0,
                "final_output": "No build required.",
                "artifact_path": None,
            }

        errors_fixed_total = 0
        last_output = ""

        for iteration in range(1, MAX_HEAL_ITERATIONS + 1):
            logger.info("[QA] Build attempt %d/%d", iteration, MAX_HEAL_ITERATIONS)
            rc, stdout, stderr = _run_cmd(build_cmd, project_dir)
            last_output = (stdout + "\n" + stderr)[:5000]

            if rc == 0:
                logger.info("[QA] ✓ Build succeeded on iteration %d", iteration)
                notify(f"✅ QA passed | job={job_id} | iterations={iteration} | fixed={errors_fixed_total}")
                artifact = self._find_artifact(project_dir, project_type)
                return {
                    "success": True,
                    "iterations": iteration,
                    "errors_fixed": errors_fixed_total,
                    "final_output": last_output,
                    "artifact_path": artifact,
                }

            # Build failed – parse errors and heal
            logger.warning("[QA] Build failed (rc=%d). Parsing errors …", rc)
            errors = _parse_errors(stdout, stderr, project_type, project_dir)

            if not errors:
                # Can't parse specific errors – send full log to Telegram and abort
                logger.error("[QA] Could not parse specific errors. Full log:\n%s", last_output[:1000])
                notify(
                    f"❌ QA failed | job={job_id} | iter={iteration}\n"
                    f"```\n{last_output[:1000]}\n```"
                )
                break

            logger.info("[QA] Found %d distinct errors to fix.", len(errors))

            # Group errors by file
            errors_by_file: dict[str, list[dict]] = {}
            for e in errors:
                errors_by_file.setdefault(e["file"], []).append(e)

            for file_path, file_errors in errors_by_file.items():
                logger.info(
                    "[QA] Healing %s (%d errors) …", file_path, len(file_errors)
                )
                fixed_content = _heal_file(
                    file_path=file_path,
                    errors=file_errors,
                    pool=self._pool,
                    project_type=project_type,
                )
                if fixed_content:
                    try:
                        Path(file_path).write_text(fixed_content, encoding="utf-8")
                        errors_fixed_total += len(file_errors)
                        logger.info("[QA] ✓ Healed: %s", file_path)
                    except Exception as exc:
                        logger.error("[QA] Could not write healed file %s: %s", file_path, exc)

            time.sleep(2)  # brief pause before re-build

        # All iterations exhausted
        logger.error("[QA] Build failed after %d iterations.", MAX_HEAL_ITERATIONS)
        notify(
            f"❌ QA exhausted | job={job_id} | "
            f"iterations={MAX_HEAL_ITERATIONS} | "
            f"fixed={errors_fixed_total}\n"
            f"```\n{last_output[-800:]}\n```"
        )
        return {
            "success": False,
            "iterations": MAX_HEAL_ITERATIONS,
            "errors_fixed": errors_fixed_total,
            "final_output": last_output,
            "artifact_path": None,
        }

    def _check_python_scripts(self, project_dir: Path, job_id: str) -> dict:
        """Python syntax check + auto-heal for script projects."""
        py_files = _get_python_files(project_dir)
        errors_fixed = 0
        success = True

        for py_file in py_files:
            for iteration in range(1, MAX_HEAL_ITERATIONS + 1):
                rc, out, err = _run_cmd(
                    ["python", "-m", "py_compile", str(py_file)],
                    cwd=project_dir,
                    timeout=30,
                )
                if rc == 0:
                    break
                errors = _parse_errors(out, err, "script", project_dir)
                fixed = _heal_file(
                    file_path=str(py_file),
                    errors=errors or [{"file": str(py_file), "line": 0, "message": err, "snippet": ""}],
                    pool=self._pool,
                    project_type="script",
                )
                if fixed:
                    py_file.write_text(fixed, encoding="utf-8")
                    errors_fixed += 1
                else:
                    success = False
                    break

        return {
            "success": success,
            "iterations": 1,
            "errors_fixed": errors_fixed,
            "final_output": f"Checked {len(py_files)} Python files.",
            "artifact_path": None,
        }

    def _find_artifact(self, project_dir: Path, project_type: str) -> Path | None:
        """Locate the compiled build artifact."""
        if project_type == "android":
            apks = list(project_dir.rglob("*.apk"))
            return apks[0] if apks else None
        if project_type == "flutter":
            apks = list(project_dir.rglob("*.apk"))
            return apks[0] if apks else None
        if project_type in ("web_react", "web_next"):
            dist = project_dir / "dist"
            if dist.exists():
                return dist
            build = project_dir / ".next"
            if build.exists():
                return build
        return None
