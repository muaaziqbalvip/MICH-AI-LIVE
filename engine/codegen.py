"""
codegen.py
─────────────────────────────────────────────────────────────────────────────
Software Factory – generates complete, runnable project code from a
natural-language specification provided via Telegram /build.

Supported project types (auto-detected from spec):
  • android   – Kotlin / Jetpack Compose Android app (full file tree)
  • web_react  – React + TypeScript SPA (Vite scaffold)
  • web_next   – Next.js 14 App Router project
  • fastapi    – Python FastAPI backend
  • flutter    – Flutter Dart mobile app
  • script     – Single Python utility script

Generation process
──────────────────
1. Classify the spec → project type + feature list.
2. Generate the complete file tree manifest (file paths + purposes).
3. For each file in the manifest, generate the FULL file contents
   (no placeholders, no "// TODO" comments, no truncation).
4. Write all files to a temporary workspace directory.
5. Hand off to QaRig for compilation / self-healing.
6. Package the result as a ZIP artifact.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from engine.api_pool import GroqPool
from engine.state_manager import DomainMatrix, SystemState

logger = logging.getLogger("codegen")

# ── Classification ────────────────────────────────────────────────────────────

PROJECT_TYPES = ["android", "web_react", "web_next", "fastapi", "flutter", "script"]


def _classify_spec(spec: str, pool: GroqPool) -> dict:
    """
    Returns:
    {
      "type": "android" | "web_react" | …,
      "app_name": "MyApp",
      "package_name": "com.example.myapp",
      "features": ["feature1", "feature2", …],
      "description": "concise one-paragraph summary"
    }
    """
    prompt = f"""Analyse this software specification and return a JSON object.

Specification:
{spec}

Return ONLY valid JSON (no markdown fences) with these fields:
- "type": one of {PROJECT_TYPES}
- "app_name": CamelCase name for the app
- "package_name": reverse-domain package (e.g. com.myorg.myapp) - for Android/Flutter
- "features": array of 4-8 specific feature strings
- "description": one paragraph describing what to build

Pick the most appropriate type based on the spec. Default to "web_react" if unclear."""

    result = pool.chat(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=512,
        temperature=0.1,
    )
    try:
        clean = re.sub(r"```[a-z]*\n?", "", result).strip().rstrip("`")
        return json.loads(clean)
    except json.JSONDecodeError:
        logger.warning("Classification JSON parse failed – using fallback.")
        return {
            "type": "web_react",
            "app_name": "GeneratedApp",
            "package_name": "com.example.generatedapp",
            "features": ["core functionality"],
            "description": spec[:300],
        }


# ── File manifest generation ──────────────────────────────────────────────────

_MANIFEST_SYSTEM = """You are an expert software architect.
Given a project specification, return a JSON array of file objects.

Each object:
{ "path": "relative/path/to/file.ext", "purpose": "one-line description" }

Rules:
- Include EVERY file needed for a fully working project.
- Include config files, manifests, entry points, components, styles, tests.
- For Android: include AndroidManifest.xml, build.gradle (project + app),
  settings.gradle, gradle.properties, proguard-rules.pro, all Kotlin source files,
  res/layout XMLs, res/values strings.xml + themes.xml + colors.xml.
- For React/Next: include package.json, tsconfig.json, vite.config.ts or next.config.js,
  tailwind.config.js, postcss.config.js, index.html, src/main.tsx, all component files,
  all page files, styles, types, hooks, utils, api files.
- For FastAPI: include main.py, requirements.txt, Dockerfile, docker-compose.yml,
  models.py, schemas.py, routes/*.py, database.py, config.py, tests/test_*.py.
- For Flutter: pubspec.yaml, all lib/**/*.dart files, AndroidManifest.xml, Info.plist.
- Return ONLY the JSON array, no markdown fences."""


def _generate_manifest(spec_data: dict, pool: GroqPool) -> list[dict]:
    prompt = (
        f"Project type: {spec_data['type']}\n"
        f"App name: {spec_data['app_name']}\n"
        f"Package: {spec_data.get('package_name', 'N/A')}\n"
        f"Features: {', '.join(spec_data['features'])}\n"
        f"Description: {spec_data['description']}\n\n"
        "Generate the complete file manifest JSON array."
    )
    result = pool.chat(
        messages=[{"role": "user", "content": prompt}],
        system=_MANIFEST_SYSTEM,
        max_tokens=2048,
        temperature=0.1,
    )
    try:
        clean = re.sub(r"```[a-z]*\n?", "", result).strip().rstrip("`")
        return json.loads(clean)
    except json.JSONDecodeError:
        logger.error("Manifest JSON parse failed. Raw: %s", result[:500])
        return []


# ── Per-file code generation ──────────────────────────────────────────────────

_CODE_SYSTEM = """You are an elite software engineer generating production-grade code.

STRICT RULES:
1. Write the COMPLETE, FULL file contents. Do NOT truncate.
2. NO placeholders like "// TODO", "/* implement later */", or "pass  # TODO".
3. NO comments saying "add your code here" or "implement this function".
4. Every function/method must have a real, working implementation.
5. Every import must be real and correct.
6. For Android Kotlin files: include all imports, proper package declarations,
   real ViewModel logic, proper Compose UI, actual data models.
7. For React/TypeScript: include all imports, proper typing, real state management,
   real API calls, complete component implementations.
8. Code must be syntactically correct and ready to compile/run.
9. Return ONLY the raw file contents. No markdown fences. No explanation."""


def _generate_file(
    file_path: str,
    purpose: str,
    spec_data: dict,
    all_files: list[dict],
    pool: GroqPool,
    knowledge_context: str = "",
) -> str:
    """Generate complete contents for a single file."""
    other_files = "\n".join(
        f"  - {f['path']}: {f['purpose']}"
        for f in all_files
        if f["path"] != file_path
    )[:1000]

    prompt = f"""Project: {spec_data['app_name']} ({spec_data['type']})
Package/namespace: {spec_data.get('package_name', 'N/A')}
Features: {', '.join(spec_data['features'])}
Description: {spec_data['description']}

File to generate: {file_path}
Purpose of this file: {purpose}

Other files in the project (for reference):
{other_files}

{f"Relevant technical knowledge:{chr(10)}{knowledge_context}" if knowledge_context else ""}

Generate the COMPLETE contents of {file_path}.
Write every line. No truncation. No placeholders."""

    return pool.chat(
        messages=[{"role": "user", "content": prompt}],
        system=_CODE_SYSTEM,
        max_tokens=4096,
        temperature=0.2,
    )


# ── Knowledge context retrieval ───────────────────────────────────────────────

def _get_knowledge_context(project_type: str, features: list[str]) -> str:
    """Pull relevant summaries from domain matrices to inform code generation."""
    domain_map = {
        "android": "android_core",
        "flutter": "android_core",
        "web_react": "web_dev",
        "web_next": "web_dev",
        "fastapi": "web_dev",
        "script": "automation",
    }
    domain = domain_map.get(project_type, "web_dev")
    matrix = DomainMatrix(domain)

    relevant_entries: list[str] = []
    for feature in features[:4]:
        matches = matrix.search(feature)
        for entry in matches[:2]:
            relevant_entries.append(
                f"[{entry['title'][:60]}]\n{entry['summary'][:300]}"
            )

    return "\n\n".join(relevant_entries[:6])


# ── Main factory function ─────────────────────────────────────────────────────


class SoftwareFactory:
    """
    Generates complete project code from a specification string.
    Returns the path to a ZIP archive of the generated project.
    """

    def __init__(self, pool: GroqPool, system_state: SystemState) -> None:
        self._pool = pool
        self._state = system_state

    def generate(self, spec: str, job_id: str) -> Path:
        """
        Full generation pipeline.
        Returns Path to a .zip file containing the generated project.
        """
        logger.info("[Factory] Starting generation for job %s", job_id)
        logger.info("[Factory] Spec: %s", spec[:200])

        # ── 1. Classify ────────────────────────────────────────────────────
        spec_data = _classify_spec(spec, self._pool)
        logger.info(
            "[Factory] Classified: type=%s app=%s features=%s",
            spec_data["type"],
            spec_data["app_name"],
            spec_data["features"],
        )

        # ── 2. Generate file manifest ──────────────────────────────────────
        manifest = _generate_manifest(spec_data, self._pool)
        if not manifest:
            raise RuntimeError("Failed to generate file manifest.")
        logger.info("[Factory] Manifest: %d files", len(manifest))

        # ── 3. Create workspace ────────────────────────────────────────────
        workspace = Path(tempfile.mkdtemp(prefix=f"factory_{job_id}_"))
        project_dir = workspace / spec_data["app_name"]
        project_dir.mkdir(parents=True, exist_ok=True)

        # ── 4. Get knowledge context ───────────────────────────────────────
        knowledge = _get_knowledge_context(
            spec_data["type"], spec_data["features"]
        )

        # ── 5. Generate each file ──────────────────────────────────────────
        generated_files: list[Path] = []
        for i, file_info in enumerate(manifest, 1):
            rel_path = file_info["path"].lstrip("/")
            purpose = file_info.get("purpose", "")
            out_path = project_dir / rel_path

            logger.info(
                "[Factory] Generating file %d/%d: %s",
                i, len(manifest), rel_path
            )

            out_path.parent.mkdir(parents=True, exist_ok=True)

            try:
                content = _generate_file(
                    file_path=rel_path,
                    purpose=purpose,
                    spec_data=spec_data,
                    all_files=manifest,
                    pool=self._pool,
                    knowledge_context=knowledge,
                )
                out_path.write_text(content, encoding="utf-8")
                generated_files.append(out_path)
                logger.info("[Factory] ✓ Written: %s (%d chars)", rel_path, len(content))
            except Exception as exc:
                logger.error("[Factory] Failed to generate %s: %s", rel_path, exc)
                # Write a stub so the project structure is complete
                out_path.write_text(
                    f"// Generation failed for this file: {exc}\n",
                    encoding="utf-8",
                )

        # ── 6. Write manifest summary ──────────────────────────────────────
        manifest_path = project_dir / "FACTORY_MANIFEST.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "job_id": job_id,
                    "spec": spec,
                    "spec_data": spec_data,
                    "files": manifest,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        # ── 7. Package as ZIP ──────────────────────────────────────────────
        zip_path = workspace / f"{spec_data['app_name']}_{job_id}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in project_dir.rglob("*"):
                if file_path.is_file():
                    zf.write(file_path, file_path.relative_to(workspace))

        logger.info(
            "[Factory] ✓ Project packaged: %s (%.1f KB)",
            zip_path.name,
            zip_path.stat().st_size / 1024,
        )

        # Record job in state
        self._state.add_factory_job(
            {
                "id": job_id,
                "spec": spec[:200],
                "type": spec_data["type"],
                "app_name": spec_data["app_name"],
                "files_generated": len(generated_files),
                "zip_path": str(zip_path),
            }
        )

        return zip_path

    def cleanup_workspace(self, zip_path: Path) -> None:
        """Remove the temp workspace directory after delivery."""
        try:
            shutil.rmtree(zip_path.parent, ignore_errors=True)
        except Exception as exc:
            logger.warning("Workspace cleanup failed: %s", exc)
