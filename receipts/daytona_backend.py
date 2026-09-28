"""Daytona stand-in for Token Factory Sandboxes, used until Nebius Sandboxes access lands.

Mirrors the slice of the Contree image API the engine uses (`run(shell, files, disposable, timeout)`).
Daytona fork isn't available on this account, so an "image" is a Daytona snapshot plus the state-changing
steps recorded on top of it (git apply, rm .git). Every run starts a fresh sandbox from the snapshot,
replays the steps, runs the command and deletes the sandbox.
"""
import asyncio
import os
from functools import lru_cache
from types import SimpleNamespace

from daytona import (AsyncDaytona, CreateSandboxFromImageParams, CreateSandboxFromSnapshotParams, DaytonaConfig,
                     DaytonaNotFoundError, FileUpload, Image)
from deepagents.backends.protocol import ExecuteResponse, FileDownloadResponse, FileUploadResponse
from deepagents.backends.sandbox import BaseSandbox

from . import config
from . import sandbox
from .sandbox import ENV


@lru_cache(maxsize=1)
def client() -> AsyncDaytona:
    return AsyncDaytona(DaytonaConfig(api_key=os.environ["DAYTONA_API_KEY"], api_url=os.environ.get("DAYTONA_API_URL")))


async def _start(snapshot: str):
    # ponytail: network_block_all is requested but this account ignored it in the probe; the writer's
    # tool_log in the evidence is the audit trail until it takes effect.
    params = CreateSandboxFromSnapshotParams(snapshot=snapshot, network_block_all=True, ephemeral=True)
    return await client().create(params, timeout=600)


async def _exec(sb, shell: str, files: dict | None, timeout: float | None):
    if files:
        await sb.fs.upload_files([FileUpload(source=data, destination=path) for path, data in files.items()])
    return await sb.process.exec(shell, timeout=int(timeout or config.SANDBOX_TIMEOUT_S))


class DaytonaImage:
    def __init__(self, snapshot: str, steps=(), exit_code: int = 0, stdout: str = ""):
        self.snapshot, self.steps, self.exit_code, self.stdout = snapshot, tuple(steps), exit_code, stdout

    async def open(self):
        """A live sandbox in this image's state. The caller deletes it."""
        sb = await _start(self.snapshot)
        try:
            for shell, files in self.steps:
                r = await _exec(sb, shell, files, None)
                if r.exit_code != 0:
                    raise RuntimeError(f"replaying {shell!r} failed: {r.result[-500:]}")
        except BaseException:
            await sb.delete()
            raise
        return sb

    async def run(self, shell=None, files=None, disposable=True, timeout=None, **_):
        sb = await self.open()
        try:
            r = await _exec(sb, shell, files, timeout)
        finally:
            await sb.delete()
        if disposable:
            return SimpleNamespace(stdout=r.result, stderr="", exit_code=r.exit_code)
        return DaytonaImage(self.snapshot, self.steps + ((shell, files),), r.exit_code, r.result)

    async def agent_backend(self, log: list) -> "DaytonaAgentSandbox":
        return DaytonaAgentSandbox(await self.open(), log)


class DaytonaAgentSandbox(BaseSandbox):
    """Deepagents backend on one live Daytona sandbox. Async only: the writer runs the agent with ainvoke."""

    def __init__(self, sandbox, log: list):
        self._sb, self.log = sandbox, log

    @property
    def id(self) -> str:
        return self._sb.id

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        sandbox.check_budget(self.log)
        try:
            r = await self._sb.process.exec(f"{ENV} && {command}", timeout=int(timeout or config.SANDBOX_TIMEOUT_S))
            out = ExecuteResponse(output=r.result or "", exit_code=r.exit_code)
        except Exception as e:
            out = ExecuteResponse(output=f"sandbox error: {type(e).__name__}: {e}", exit_code=1)
        return sandbox.record(self.log, command, out)

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        await self._sb.fs.upload_files([FileUpload(source=data, destination=path) for path, data in files])
        return [FileUploadResponse(path=path, error=None) for path, _ in files]

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        out = []
        for path in paths:
            try:
                out.append(FileDownloadResponse(path=path, content=await self._sb.fs.download_file(path)))
            except Exception:
                out.append(FileDownloadResponse(path=path, error="file_not_found"))
        return out

    async def aclose(self) -> None:
        await self._sb.delete()

    def execute(self, command, *, timeout=None):
        raise NotImplementedError("async only")

    def upload_files(self, files):
        raise NotImplementedError("async only")

    def download_files(self, paths):
        raise NotImplementedError("async only")


async def base_image(ref: str, name: str) -> DaytonaImage:
    """Snapshot `name` of the SWE-bench env `ref`, built once (sandbox from image -> snapshot) and reused."""
    d = client()
    try:
        await d.snapshot.get(name)
    except DaytonaNotFoundError:
        sb = await d.create(CreateSandboxFromImageParams(name=f"{name}-build", image=Image.base(ref)), timeout=1800)
        await sb.create_snapshot(name, timeout=0)
        await sb.delete()
    for _ in range(360):  # a snapshot started by an earlier, interrupted run may still be building
        state = str((await d.snapshot.get(name)).state).upper()
        if "ACTIVE" in state:
            return DaytonaImage(name)
        if "ERROR" in state or "FAIL" in state:
            raise RuntimeError(f"Daytona snapshot {name} is {state}")
        await asyncio.sleep(5)
    raise TimeoutError(f"Daytona snapshot {name} not ready after 30 min")
