"""Native CLI subprocess: bounded JSONL, final-result-only, process-group cleanup."""
import asyncio
import json
import os
import re
import signal
import sys
from pathlib import Path


class RunFailed(Exception):
    """Deliberately carries no child output or credentials."""


class StreamParser:
    def __init__(self):
        self.result = None
        self.size = 0

    def feed(self, line):
        self.size += len(line)
        if self.size > 8 * 1024 * 1024:
            raise RunFailed()
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            raise RunFailed() from None
        if not isinstance(event, dict) or self.result is not None:
            raise RunFailed()
        if event.get("type") == "result":
            if (type(event.get("exit_code")) is not int or event["exit_code"] != 0
                    or event.get("error") or not isinstance(event.get("text"), str)
                    or event.get("failed") or event.get("partial")
                    or event.get("interrupted") or event.get("completed") is False):
                raise RunFailed()
            self.result = event["text"]

    def finish(self, returncode):
        if returncode != 0 or self.result is None:
            raise RunFailed()
        return self.result


def bounded_text(text):
    text = text.strip()
    if len(text) > 24000:
        return text[:23950].rstrip() + "\n[Текст сокращён до лимита плагина.]"
    return text


def safe_text(text):
    # Media tags are directives to native delivery, not imaginary attachments.
    if re.search(r"MEDIA:|<\|(?:image|audio|video)|\[\[(?:audio|voice)|(?:file|sandbox)://", text, re.I):
        return "Hermes подготовил медиа. Этот плагин поддерживает только текст; файл не отправлен."
    # Final text can itself contain tool-produced paths. Do not publish local paths.
    text = re.sub(r"(?<![\w:/])(?:/[\w.~-]+(?:/[^\s`<>\]\)]+)+|~/[^\s`<>]+|[A-Za-z]:\\[^\s`<>]+)",
                  "[локальный путь скрыт]", text)
    return bounded_text(text) or "Hermes завершил запрос без текстового ответа."


async def complete_cleanup(awaitable):
    # Repeated lifecycle cancellations must not release route/file/capacity
    # while the supervised process group is still being torn down.
    cleanup = asyncio.ensure_future(awaitable)
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
    result = cleanup.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


class CliRunner:
    def __init__(self, home, config):
        self.home, self.config = home, config

    def argv(self, route):
        return [self.config.cli, "chat", "--query-file", "-", "--oneshot", "--format", "stream-json",
                "--source", "tool", "--continue", "business-powerpack-" + route, "--create-if-missing"]

    def environment(self):
        # Child resolves its own profile's credentials natively. No tokens copied,
        # no ambient model/approval/session/profile routing overrides inherited.
        env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE",
                                               "SSL_CERT_DIR") if key in os.environ}
        from tools.environments.local import served_profile_child_env
        env = served_profile_child_env(base=env, target_home=self.home, inherit_credentials=False)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    async def run(self, route, query):
        envelope = (
            "[Telegram Business: authenticated owner request. Your final text will be visible "
            "to the conversation partner, not just the owner. Do not expose secrets, private "
            "profile memory, internal logs or local paths. Do not contact support or send "
            "additional messages on your own. The plugin delivers the final answer. "
            "This is a separate Business session, not the owner's main Hermes chat.]\n\n"
        )
        return await self.run_command(self.argv(route), envelope + query, self.config.timeout)

    async def transcribe(self, path):
        # Same native profile STT, but in a killable process rather than an
        # unkillable executor thread. No model agent or tools are launched.
        return await self.run_command(self.stt_argv(path), "", self.config.stt_timeout)

    def stt_argv(self, path):
        return [sys.executable, str(Path(__file__).with_name("stt_worker.py")), str(path)]

    async def run_command(self, argv, input_text, timeout):
        spawning = asyncio.create_task(asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=self.environment(), cwd=self.home,
            start_new_session=True, limit=256 * 1024))
        try:
            proc = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            # Cancellation during transport creation must not orphan a process
            # before we have acquired its PID for group cleanup.
            async def stop_spawned():
                await self.stop(await spawning)
            await complete_cleanup(stop_spawned())
            raise
        parser = StreamParser()
        async def exchange():
            proc.stdin.write(input_text.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
            while line := await proc.stdout.readline():
                parser.feed(line)
            return parser.finish(await proc.wait())
        try:
            return await asyncio.wait_for(exchange(), timeout)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise RunFailed() from None
        finally:
            # Kill the whole group even if the leader has already exited; tools may
            # have inherited stdout or deliberately stayed running after it.
            await complete_cleanup(self.stop(proc))

    @staticmethod
    async def stop(proc):
        def send(sig):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                pass
        async def discard_stdout():
            # exchange() has stopped reading on timeout/parser failure. Keeping
            # the pipe drained avoids asyncio.wait() stalling on a full reader
            # even after the OS process has been killed. Never accumulate data.
            try:
                while await proc.stdout.read(65536):
                    pass
            except (OSError, RuntimeError):
                pass
        draining = asyncio.create_task(discard_stdout())
        try:
            send(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), 2)
            except asyncio.TimeoutError:
                pass
            send(signal.SIGKILL)
            try:
                await asyncio.wait_for(proc.wait(), 2)
            except asyncio.TimeoutError:
                # A detached writer may still own this pipe after our group
                # exits. asyncio has no public StreamReader close operation.
                # Close only this owned stdout transport, then reap the leader.
                proc.stdout._transport.close()
                await asyncio.wait_for(proc.wait(), 2)
        finally:
            draining.cancel()
            await asyncio.gather(draining, return_exceptions=True)
