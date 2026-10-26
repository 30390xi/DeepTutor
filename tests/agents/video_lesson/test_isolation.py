"""Real container checks; CI enables these with DEEPTUTOR_TEST_VIDEO_SANDBOX=1."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from deeptutor.agents.video_lesson import runtime

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("DEEPTUTOR_TEST_VIDEO_SANDBOX") != "1",
        reason="requires Docker and the Remotion npm runtime",
    ),
]

CONFIG = "export const LESSON = {durationInFrames:60, fps:30, width:1920, height:1080};\n"
VALID = CONFIG + 'export const Lesson = () => <div>{"A safe lesson. ".repeat(50)}</div>;'


async def test_real_render_and_runtime_error(tmp_path: Path) -> None:
    lesson = runtime.prepare_workdir(tmp_path, VALID)
    result = await runtime.build_lesson(lesson, tmp_path / "player.js")
    assert result.ok and len(result.frames) == 25
    assert (tmp_path / "player.js").stat().st_size > 0
    lesson.write_text(CONFIG + 'export const Lesson = () => {throw new Error("fixture failure")};')
    result = await runtime.build_lesson(lesson, tmp_path / "player.js")
    assert not result.ok and any("fixture failure" in error for error in result.errors)


async def test_model_cannot_read_host_secrets_files_or_connect_to_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "host-secret.txt"
    marker.write_text("dummy-host-secret")
    monkeypatch.setenv("DT_REVIEW_SENTINEL", "dummy-environment-secret")
    code = """import React from "react";
let probe = Promise.resolve();
if (typeof process !== "undefined") {
  if (process.env.DT_REVIEW_SENTINEL) throw new Error("host environment leaked");
  const fs = process.getBuiltinModule("fs");
  if (fs.existsSync(HOST_FILE)) throw new Error("host file exposed");
  let writable = false;
  try { fs.writeFileSync("/tmp/should-not-exist", "x"); writable = true; } catch {}
  if (writable) throw new Error("container filesystem is writable");
  probe = fetch("http://1.1.1.1", {signal: AbortSignal.timeout(500)}).then(
    () => {throw new Error("outbound network allowed");}, () => undefined);
}
export const Lesson = () => {
  React.use(probe);
  return <div>{"Isolated lesson. ".repeat(50)}</div>;
};
""".replace("HOST_FILE", json.dumps(str(marker)))
    lesson = runtime.prepare_workdir(tmp_path, CONFIG + code)
    result = await runtime.build_lesson(lesson, tmp_path / "player.js")
    assert result.ok, result.errors
    assert marker.read_text() == "dummy-host-secret"


async def test_cancelled_render_removes_running_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await runtime.ensure_runtime()
    original = runtime._run_tool
    created = asyncio.Event()
    names: list[str] = []

    async def observe(argv, **kwargs):
        result = await original(argv, **kwargs)
        if "create" in argv:
            names.append(argv[argv.index("--name") + 1])
            created.set()
        return result

    monkeypatch.setattr(runtime, "_run_tool", observe)
    lesson = runtime.prepare_workdir(
        tmp_path, CONFIG + "export const Lesson = () => { while (true) {} };"
    )
    task = asyncio.create_task(runtime.build_lesson(lesson, tmp_path / "player.js"))
    try:
        await asyncio.wait_for(created.wait(), 30)
        for _ in range(100):
            state = await original(
                [runtime._docker(), "inspect", "--format", "{{.State.Running}}", names[0]],
                timeout=5,
            )
            if state.stdout.strip() == "true":
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("renderer never started")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 20)
        state = await original([runtime._docker(), "inspect", names[0]], timeout=5)
        assert state.exit_code != 0 and "no such" in state.stderr.lower()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for name in names:
            await original([runtime._docker(), "rm", "--force", name], timeout=10)


async def test_timed_out_render_is_rejected_and_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "RENDER_TIMEOUT_S", 1)
    original = runtime._run_tool
    names: list[str] = []

    async def observe(argv, **kwargs):
        if "create" in argv:
            names.append(argv[argv.index("--name") + 1])
        return await original(argv, **kwargs)

    monkeypatch.setattr(runtime, "_run_tool", observe)
    lesson = runtime.prepare_workdir(
        tmp_path, CONFIG + "export const Lesson = () => { while (true) {} };"
    )
    result = await runtime.build_lesson(lesson, tmp_path / "player.js")
    assert not result.ok and result.stage == "render"
    state = await original([runtime._docker(), "inspect", names[0]], timeout=5)
    assert state.exit_code != 0 and "no such" in state.stderr.lower()


async def test_build_script_refuses_to_execute_without_container(tmp_path: Path) -> None:
    root = await runtime.ensure_runtime()
    lesson = runtime.prepare_workdir(tmp_path, VALID)
    result = await runtime._run_tool(
        [
            runtime._node(),
            str(runtime.ASSETS_DIR / "build.mjs"),
            str(lesson),
            str(tmp_path / "player.js"),
        ],
        timeout=30,
        env={"DEEPTUTOR_REMOTION_RUNTIME": str(root)},
    )
    assert result.exit_code != 0
    assert json.loads(result.stdout.strip().splitlines()[-1])["stage"] == "sandbox"
    assert not (tmp_path / "player.js").exists()
