import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home()


def pair_home() -> Path:
    return Path(os.environ.get("PAIR_HOME") or HOME / ".pair")


def briefs_dir() -> Path:
    return pair_home() / "briefs"


def worktrees_dir() -> Path:
    return pair_home() / "worktrees"


def logs_dir() -> Path:
    return pair_home() / "logs"


def claude_dir() -> Path:
    return Path(os.environ.get("PAIR_CLAUDE_DIR") or HOME / ".claude/projects")


def codex_dir() -> Path:
    return Path(os.environ.get("PAIR_CODEX_DIR") or HOME / ".codex/sessions")


def clone_roots() -> list[Path]:
    """The plain-clone root first, then workspace roots laid out as <root>/<repo>/<workspace>."""
    if os.environ.get("PAIR_CLONE_ROOTS"):
        return [Path(p) for p in os.environ["PAIR_CLONE_ROOTS"].split(os.pathsep)]
    return [HOME / "dev", HOME / "awt/workspaces", HOME / "orca/workspaces"]


def config_path() -> Path:
    return Path(os.environ.get("PAIR_CONFIG") or HOME / ".config/pair/config.toml")


@dataclass
class Config:
    anthropic_api_key_env: str = "ANTHROPIC_API_KEY"
    pair_agent_model: str = "sonnet"
    host: str = "127.0.0.1"
    port: int = 7788
    allowed_repos: list[str] = field(default_factory=lambda: ["five-sh/awt"])
    github_accounts: dict[str, str] = field(
        default_factory=lambda: {"five-sh": "five-sh", "interviewstreet": "kousik-sh"})

    def account_for(self, repo: str) -> str | None:
        return self.github_accounts.get(repo.split("/")[0].lower())

    def can_post(self, repo: str) -> bool:
        return repo.lower() in {r.lower() for r in self.allowed_repos}

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    cfg = Config()
    if not path.exists():
        return cfg
    data = tomllib.loads(path.read_text())
    # Configs written before the rename name the model reviewer_model.
    if "reviewer_model" in data:
        data.setdefault("pair_agent_model", data["reviewer_model"])
    for key in ("anthropic_api_key_env", "pair_agent_model", "host"):
        if key in data:
            setattr(cfg, key, str(data[key]))
    if "port" in data:
        cfg.port = int(data["port"])
    if "allowed_repos" in (data.get("post_comments") or {}):
        cfg.allowed_repos = [str(r) for r in data["post_comments"]["allowed_repos"]]
    if "github_accounts" in data:
        cfg.github_accounts = {str(k).lower(): str(v) for k, v in data["github_accounts"].items()}
    return cfg
