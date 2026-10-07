"""Private native STT worker. No agent, tools, or raw provider output."""
import json
import os
from pathlib import Path
import sys
from contextlib import redirect_stdout, redirect_stderr


def main():
    # Invoked only as an argv-based trusted script in a supervised process.
    try:
        with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
            from hermes_constants import get_hermes_home
            from scope import profile_scope
            home = get_hermes_home().resolve()
            path = Path(sys.argv[1]).resolve()
            # Only plugin-downloaded media, never arbitrary native credential files.
            media = (home / "business-powerpack" / "media").resolve()
            if path.parent != media or path.suffix != ".ogg" or not path.is_file():
                raise ValueError("Invalid STT input")
            with profile_scope(home):
                from tools.transcription_tools import transcribe_audio
                result = transcribe_audio(str(path), source="gateway")
                if not isinstance(result, dict) or result.get("success") is not True:
                    raise ValueError("STT failed")
                text = result.get("transcript")
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("STT empty")
        print(json.dumps({"type": "result", "exit_code": 0, "text": text}, ensure_ascii=False))
        return 0
    except Exception:
        # No native traceback, provider URL/token or local path reaches the caller.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
