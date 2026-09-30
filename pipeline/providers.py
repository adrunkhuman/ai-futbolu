"""One adapter per provider, all exposing complete(system, user) -> str.

Every call is stateless: the room re-sends the full transcript each turn, so
any provider (including one-shot `claude -p`) can be a seat.
"""

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path


class Provider:
    def __init__(self, model: str, search: bool = True):
        self.model = model
        self.search = search

    def complete(self, system: str, user: str) -> str:
        raise NotImplementedError


class ClaudeCLI(Provider):
    """Runs on the Claude Code subscription login via `claude -p`.

    Runs from an empty temp dir with project/local settings ignored so the
    coding-agent context doesn't leak into the pundit.
    """

    def __init__(self, model: str, search: bool = True):
        super().__init__(model, search)
        self.cwd = tempfile.mkdtemp(prefix="ai-futbolu-claude-")

    def complete(self, system: str, user: str) -> str:
        cmd = [
            "claude", "-p",
            "--model", self.model,
            "--system-prompt", system,
            "--tools", "WebSearch" if self.search else "",
            "--setting-sources", "",
            "--no-session-persistence",
            "--output-format", "text",
        ]
        if self.search:
            cmd += ["--allowedTools", "WebSearch"]
        r = subprocess.run(cmd, input=user, capture_output=True, text=True, cwd=self.cwd, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"claude -p failed: {r.stderr.strip()[:500]}")
        return r.stdout.strip()


class CodexCLI(Provider):
    """Runs on the ChatGPT subscription login via `codex exec`.

    Codex has no system-prompt flag, so the system text is prepended to the
    prompt. Runs read-only from an empty temp dir, ignoring user config/rules.
    """

    def __init__(self, model: str, search: bool = True):
        super().__init__(model, search)
        self.cwd = tempfile.mkdtemp(prefix="ai-futbolu-codex-")

    def complete(self, system: str, user: str) -> str:
        out = Path(self.cwd) / "last.txt"
        out.unlink(missing_ok=True)
        cmd = ["codex"] + (["--search"] if self.search else []) + [
            "exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "--ignore-rules",
            "-s", "read-only", "-m", self.model, "-o", str(out),
        ]
        prompt = f"{system}\n\n---\n\n{user}"
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=self.cwd, timeout=300)
        if r.returncode != 0 or not out.exists():
            raise RuntimeError(f"codex exec failed: {r.stderr.strip()[-500:]}")
        return out.read_text().strip()


class GrokCLI(Provider):
    """Runs on the Grok login via `grok -p`; JSON output avoids leaked tool narration.

    Each call carries ~44k tokens of agent overhead, so keep episodes lean.
    """

    def __init__(self, model: str, search: bool = True):
        super().__init__(model, search)
        self.cwd = tempfile.mkdtemp(prefix="ai-futbolu-grok-")

    def complete(self, system: str, user: str) -> str:
        cmd = [
            "grok", "-p", user,
            "--system-prompt-override", system,
            "--output-format", "json",
            "--max-turns", "4",
            "--permission-mode", "dontAsk",
        ]
        if self.model != "default":
            cmd += ["-m", self.model]
        if not self.search:
            cmd.append("--disable-web-search")
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=self.cwd, timeout=300, stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            raise RuntimeError(f"grok failed: {r.stderr.strip()[-500:]}")
        return json.loads(r.stdout)["text"].strip()


class OpenRouter(Provider):
    """OpenAI-compatible endpoint; the ':online' suffix enables OpenRouter's web search."""

    def __init__(self, model: str, search: bool = True):
        super().__init__(model, search)
        from openai import OpenAI as Client

        self.client = Client(base_url="https://openrouter.ai/api/v1", api_key=os.environ["OPENROUTER_API_KEY"])

    def complete(self, system: str, user: str) -> str:
        model = self.model + ":online" if self.search else self.model
        r = self.client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        return (r.choices[0].message.content or "").strip()


class Stub(Provider):
    """Free stand-in for --dry-run."""

    def complete(self, system: str, user: str) -> str:
        if "PRODUCENT" in user:
            return "Dobra, słyszę w słuchawce, że kończymy. Zaraz podsumujemy."
        if "POŻEGNANIE" in user:
            return "Dziękujemy za uwagę, do zobaczenia za tydzień."
        return "Moim zdaniem to jest kwestia formy, a forma przychodzi i odchodzi, proszę państwa."


REGISTRY = {"claude": ClaudeCLI, "codex": CodexCLI, "grok": GrokCLI, "openrouter": OpenRouter, "stub": Stub}


def make(provider: str, model: str, search: bool = True) -> Provider:
    p = REGISTRY[provider](model, search)
    p.key = provider
    return p


# Search-enabled models leak citations ("sport.pl") and, via `grok -p`, their pre-search narration
# ("Sprawdzę tabelę...") glued to the real answer without a space.
NARRATION = re.compile(r"\b(?:sprawdz\w*|sprawdź\w*|wyszuk\w*|poszukam|zerknę)\b", re.I)


def clean(text: str) -> str:
    text = re.sub(r"\s*\[([^\]]*)\]\(https?://[^)]*\)", r" \1", text)
    text = re.sub(r"\s*\(?\b[\w-]+(?:\.[\w-]+)*\.(?:com|pl|net|org|eu|tv)\b(?:/\S*)?\)?", "", text)
    m = re.search(r"(?<=[a-zęóąśłżźćń][.!?])(?=[A-ZŻŹĆĄŚĘŁÓŃ])", text)
    if m and NARRATION.search(text[:m.start()]):
        text = text[m.end():]
    return re.sub(r"\s+([,.;:!?])", r"\1", re.sub(r"[ \t]{2,}", " ", text)).strip()


def clip_words(text: str, limit: int) -> str:
    """Keep whole sentences up to `limit` words (at least the first sentence)."""
    if len(text.split()) <= limit:
        return text
    out, n = [], 0
    for sent in re.split(r"(?<=[.!?…])\s+", text):
        w = len(sent.split())
        if out and n + w > limit:
            break
        out.append(sent)
        n += w
    return " ".join(out)


def complete_with_retry(p: Provider, system: str, user: str, tries: int = 3) -> str:
    for i in range(tries):
        try:
            text = re.split(r"\n+\s*(?:Sources|Źródła):", p.complete(system, user))[0]
            text = clean(text)
            if text:
                return text
            raise RuntimeError("empty reply")
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))
    raise AssertionError("unreachable")
