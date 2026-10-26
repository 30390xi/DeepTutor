"""Remotion runtime for video lessons: install once, compile per lesson, wrap in a page.

The model writes ``Lesson.tsx`` against the shipped kit (``remotion/kit.tsx``).
``build.mjs`` compiles it with esbuild, renders sample frames in a
network-isolated, read-only Docker container, and bundles it with ``@remotion/player``.
The page this module writes is self-contained: the learner's browser plays the
composition live, so no video file is ever encoded on the server.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from html import escape
import json
import os
from pathlib import Path
import shutil
import uuid

from deeptutor.services.path_service import get_path_service
from deeptutor.services.sandbox.backends import RestrictedSubprocessBackend
from deeptutor.services.sandbox.spec import ExecRequest, ExecResult, ResourceLimits

ASSETS_DIR = Path(__file__).resolve().parent / "remotion"
KIT_FILES = ("kit.tsx", "china_outline.json", "voice.ts")
BUILD_TIMEOUT_S = 600
INSTALL_TIMEOUT_S = 900
# Official multi-platform Node 22 image, pinned so render checks use one runtime.
RENDER_IMAGE = (
    "node:22-slim@sha256:de170b39439eb0e5a3444ad7d60b015614a381e10fecae2e30ec9e735122f38d"
)
RENDER_TIMEOUT_S = 180

_install_lock = asyncio.Lock()


class VideoLessonRuntimeError(RuntimeError):
    """The Node toolchain for video lessons is missing or could not be installed."""


@dataclass
class BuildResult:
    ok: bool
    stage: str
    errors: list[str] = field(default_factory=list)
    frames: list[dict] = field(default_factory=list)


def runtime_dir() -> Path:
    """Where the npm packages live; ``DEEPTUTOR_REMOTION_RUNTIME`` overrides the default."""
    override = os.environ.get("DEEPTUTOR_REMOTION_RUNTIME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return get_path_service().get_runtime_state_dir() / "remotion"


def _node() -> str:
    node = shutil.which("node")
    if not node:
        raise VideoLessonRuntimeError(
            "Video lessons need Node.js 22+ on the server (the `node` command was not found)."
        )
    return node


def _installed(root: Path) -> bool:
    manifest = json.loads((ASSETS_DIR / "package.json").read_text(encoding="utf-8"))
    for name, version in manifest["dependencies"].items():
        pkg = root / "node_modules" / name / "package.json"
        if not pkg.is_file():
            return False
        if json.loads(pkg.read_text(encoding="utf-8")).get("version") != version:
            return False
    return True


async def _run_tool(
    argv: list[str], *, timeout: int, cwd: Path | None = None, env: dict[str, str] | None = None
) -> ExecResult:
    """Run trusted tooling with bounded output and cancellation of its process tree.

    Model code never runs through this host subprocess backend. Only the
    compiler and Docker client do; the frame-check bundle goes to the container.
    """
    docker_env = {
        key: os.environ[key]
        for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "XDG_RUNTIME_DIR")
        if key in os.environ
    }
    return await RestrictedSubprocessBackend(inherit_virtualenv=False).exec(
        ExecRequest.of_argv(
            argv,
            workdir=str(cwd) if cwd else "",
            env={**docker_env, **(env or {})},
            limits=ResourceLimits(timeout_s=timeout, max_output_chars=64_000),
        )
    )


def _docker() -> str:
    docker = shutil.which("docker")
    if not docker:
        raise VideoLessonRuntimeError(
            "Video lessons need Docker with a running Linux-container daemon for isolated render checks."
        )
    return docker


def _require_success(result: ExecResult, action: str) -> None:
    if not result.ok or result.exit_code != 0:
        detail = result.error or result.stderr or result.stdout
        if result.timed_out:
            detail = "timed out"
        raise VideoLessonRuntimeError(f"{action} failed: {detail[-1500:]}")


async def ensure_runtime() -> Path:
    """Prepare pinned tooling and the isolated renderer; never fall back to host execution."""
    _node()
    docker = _docker()
    root = runtime_dir()
    async with _install_lock:
        if not _installed(root):
            npm = shutil.which("npm")
            if not npm:
                raise VideoLessonRuntimeError("Video lessons need npm to install Remotion.")
            root.mkdir(parents=True, exist_ok=True)
            shutil.copy(ASSETS_DIR / "package.json", root / "package.json")
            result = await _run_tool(
                [npm, "install", "--no-audit", "--no-fund", "--omit=dev"],
                timeout=INSTALL_TIMEOUT_S,
                cwd=root,
            )
            _require_success(result, "Installing Remotion")
            if not _installed(root):
                raise VideoLessonRuntimeError(
                    "Installed Remotion packages do not match the manifest."
                )
        result = await _run_tool([docker, "info", "--format", "{{.OSType}}"], timeout=15)
        _require_success(result, "Connecting to the Docker sandbox")
        if result.stdout.strip() != "linux":
            raise VideoLessonRuntimeError("Video lesson render checks need Linux containers.")
        image = await _run_tool([docker, "image", "inspect", RENDER_IMAGE], timeout=15)
        if not image.ok or image.exit_code != 0:
            _require_success(
                await _run_tool([docker, "pull", RENDER_IMAGE], timeout=INSTALL_TIMEOUT_S),
                "Preparing the isolated Node renderer",
            )
    return root


def prepare_workdir(work_dir: Path, code: str, voice: str | None = None) -> Path:
    """Write the lesson next to a fresh copy of the kit and return the lesson path.

    ``voice`` is the generated ``voice.ts`` module; without it the kit's empty default is used.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    for name in KIT_FILES:
        shutil.copy(ASSETS_DIR / name, work_dir / name)
    if voice is not None:
        (work_dir / "voice.ts").write_text(voice, encoding="utf-8")
    lesson = work_dir / "Lesson.tsx"
    lesson.write_text(code, encoding="utf-8")
    return lesson


def _container_argv(docker: str, name: str) -> list[str]:
    # No bind mounts, host environment, published ports, or Docker socket.
    # The self-contained check bundle is streamed over stdin by build.mjs.
    return [
        docker,
        "create",
        "--name",
        name,
        "--interactive",
        "--init",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        "65534:65534",
        "--pids-limit",
        "64",
        "--memory",
        "512m",
        "--memory-swap",
        "512m",
        "--cpus",
        "1",
        RENDER_IMAGE,
        "node",
        "--input-type=module",
    ]


async def build_lesson(lesson: Path, bundle: Path) -> BuildResult:
    """Compile on the host; execute generated code only in a disposable container."""
    root = await ensure_runtime()
    docker = _docker()
    name = "dt-lesson-" + uuid.uuid4().hex
    try:
        _require_success(
            await _run_tool(_container_argv(docker, name), timeout=30),
            "Creating the isolated renderer",
        )
        result = await _run_tool(
            [_node(), str(ASSETS_DIR / "build.mjs"), str(lesson), str(bundle)],
            timeout=BUILD_TIMEOUT_S,
            env={
                "DEEPTUTOR_REMOTION_RUNTIME": str(root),
                "DEEPTUTOR_RENDER_DOCKER": docker,
                "DEEPTUTOR_RENDER_CONTAINER": name,
                "DEEPTUTOR_RENDER_TIMEOUT_MS": str(RENDER_TIMEOUT_S * 1000),
            },
        )
    finally:
        # Killing the Docker client does not stop a daemon-owned container.
        # Always remove this turn's container, including cancelled create/start calls.
        cleanup = await _run_tool([docker, "rm", "--force", name], timeout=15)
        if not cleanup.ok or (
            cleanup.exit_code and "no such container" not in cleanup.stderr.lower()
        ):
            _require_success(cleanup, "Removing the isolated renderer")
    if result.timed_out:
        return BuildResult(False, "timeout", [f"build did not finish within {BUILD_TIMEOUT_S}s"])
    lines = result.stdout.strip().splitlines()
    try:
        data = json.loads(lines[-1])
    except (IndexError, json.JSONDecodeError):
        return BuildResult(False, "build", [(result.error or result.stderr or "no output")[-1500:]])
    return BuildResult(
        ok=bool(data.get("ok")) and result.ok and result.exit_code == 0,
        stage=str(data.get("stage", "")),
        errors=[str(e) for e in data.get("errors") or []],
        frames=list(data.get("frames") or []),
    )


PAGE = """<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600&family=Geist+Mono:wght@400;500&family=Lora:ital,wght@0,400;0,500;0,600;1,500&display=swap" rel="stylesheet">
<style>
* {{ box-sizing: border-box }}
html, body {{ margin: 0; background: #fdfcf9 }}
#player {{ width: 100%; max-width: 1280px; margin: 0 auto }}
</style>
</head>
<body>
<div id="player"></div>
<script>{bundle}</script>
</body>
</html>
"""


def write_page(bundle: Path, page: Path, *, title: str, language: str) -> Path:
    script = bundle.read_text(encoding="utf-8").replace("</script", "<\\/script")
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        PAGE.format(
            lang="zh-CN" if language.startswith("zh") else "en", title=escape(title), bundle=script
        ),
        encoding="utf-8",
    )
    return page


__all__ = [
    "BuildResult",
    "VideoLessonRuntimeError",
    "build_lesson",
    "ensure_runtime",
    "prepare_workdir",
    "runtime_dir",
    "write_page",
]
