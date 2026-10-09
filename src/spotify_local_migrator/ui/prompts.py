"""Review shortcuts with immediate Escape handling on POSIX terminals."""

import os
import select
import sys
from typing import TextIO

from rich.console import Console
from rich.prompt import IntPrompt
from rich.text import Text


class ReviewChoicePrompt(IntPrompt):
    ESCAPE = -1

    def process_response(self, value: str) -> int:
        if value.strip() == "\x1b":
            return self.ESCAPE
        return super().process_response(value)

    @classmethod
    def get_input(
        cls, console: Console, prompt: Text | str, password: bool, stream: TextIO | None = None
    ) -> str:
        source = stream if stream is not None else sys.stdin
        if os.name != "posix" or not source.isatty() or password:
            return super().get_input(console, prompt, password, stream=stream)

        import termios
        import tty

        descriptor = source.fileno()
        previous = termios.tcgetattr(descriptor)
        value: list[str] = []
        try:
            # Keep signal handling (Ctrl+C), but receive Enter/Escape directly
            # instead of waiting for a complete line. Restore settings on exit.
            tty.setcbreak(descriptor, termios.TCSANOW)
            console.print(prompt, end="")
            while True:
                key = os.read(descriptor, 1)
                if not key or key == b"\x04":
                    raise EOFError
                if key in (b"\r", b"\n"):
                    return "".join(value)
                if key == b"\x1b":
                    # Arrow/function keys also start with Escape. Consume their
                    # terminal sequence rather than treating them as a skip.
                    if select.select([descriptor], [], [], 0.05)[0]:
                        prefix = os.read(descriptor, 1)
                        if prefix in (b"[", b"O"):
                            for _ in range(32):
                                if not select.select([descriptor], [], [], 0.05)[0]:
                                    break
                                end = os.read(descriptor, 1)
                                if not end or 0x40 <= end[0] <= 0x7E:
                                    break
                            continue
                    return "\x1b"
                if key in (b"\x7f", b"\x08"):
                    if value:
                        value.pop()
                        console.print("\b \b", end="")
                elif key == b"\x15":
                    console.print("\b \b" * len(value), end="")
                    value.clear()
                elif 0x20 <= key[0] <= 0x7E:
                    character = key.decode("ascii")
                    value.append(character)
                    console.print(character, end="")
        finally:
            termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)
            console.print()
