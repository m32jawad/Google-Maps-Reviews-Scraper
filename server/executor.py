"""Launch/inspect/kill worker processes.

DockerExecutor  -- production: one container per run, RAM + CPU capped, joined
                   to the compose network so it reaches the shared database.
SubprocessExecutor -- development: plain child process (no isolation, no RAM
                   cap); lets the whole API flow run on machines without docker.

A handle string ("docker:<id>" / "proc:<pid>") is stored on the run row; the
scheduler only ever talks to these four methods.
"""
import os
import subprocess
import sys

from . import settings


class DockerExecutor:
    def __init__(self):
        import docker  # imported lazily so subprocess mode needs no docker SDK
        self._client = docker.from_env()
        self._errors = docker.errors

    def launch(self, run):
        container = self._client.containers.run(
            image=settings.WORKER_IMAGE,
            command=["python", "-m", "server.worker"],
            environment={
                "RUN_ID": run.id,
                "DATABASE_URL": settings.WORKER_DATABASE_URL,
                "DEFAULT_PROXY_URLS": ",".join(settings.DEFAULT_PROXY_URLS),
            },
            detach=True,
            network=settings.WORKER_NETWORK,
            mem_limit=f"{run.memory_mb}m",
            memswap_limit=f"{run.memory_mb}m",  # RAM cap only, no swap overflow
            nano_cpus=int(settings.WORKER_CPUS * 1e9),
            labels={"reviews-finder.run": run.id},
            name=f"rf-worker-{run.id}",
        )
        return f"docker:{container.id}"

    def _container(self, handle):
        try:
            return self._client.containers.get(handle.split(":", 1)[1])
        except self._errors.NotFound:
            return None

    def is_running(self, handle):
        c = self._container(handle)
        return c is not None and c.status in ("created", "running", "restarting")

    def kill(self, handle):
        c = self._container(handle)
        if c is not None:
            try:
                c.kill()
            except self._errors.APIError:
                pass

    def cleanup(self, handle):
        """Remove the stopped container; returns its last log lines for errors."""
        c = self._container(handle)
        if c is None:
            return None
        try:
            logs = c.logs(tail=30).decode("utf-8", "replace")
        except self._errors.APIError:
            logs = None
        try:
            c.remove(force=True)
        except self._errors.APIError:
            pass
        return logs


class SubprocessExecutor:
    def launch(self, run):
        env = {
            **os.environ,
            "RUN_ID": run.id,
            "DATABASE_URL": settings.DATABASE_URL,
            "DEFAULT_PROXY_URLS": ",".join(settings.DEFAULT_PROXY_URLS),
            "PYTHONIOENCODING": "utf-8",
        }
        proc = subprocess.Popen(
            [sys.executable, "-m", "server.worker"], env=env,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        self._procs = getattr(self, "_procs", {})
        self._procs[proc.pid] = proc
        return f"proc:{proc.pid}"

    def _pid(self, handle):
        return int(handle.split(":", 1)[1])

    def is_running(self, handle):
        proc = getattr(self, "_procs", {}).get(self._pid(handle))
        if proc is not None:
            return proc.poll() is None
        return False  # not ours (API restarted) -> treat as gone

    def kill(self, handle):
        proc = getattr(self, "_procs", {}).get(self._pid(handle))
        if proc is not None and proc.poll() is None:
            proc.kill()

    def cleanup(self, handle):
        getattr(self, "_procs", {}).pop(self._pid(handle), None)
        return None


def make_executor():
    if settings.EXECUTOR == "subprocess":
        return SubprocessExecutor()
    return DockerExecutor()
