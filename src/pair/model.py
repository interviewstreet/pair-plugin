from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Edit:
    path: str
    ts: datetime
    added: list[str] = field(default_factory=list)


@dataclass
class Command:
    cmd: str
    ts: datetime
    output: str
    failed: bool


@dataclass
class Question:
    question: str
    options: list[str]
    answer: str | None
    ref: str
    ts: datetime


@dataclass
class Plan:
    text: str
    ref: str
    path: str | None = None


@dataclass
class Message:
    """One transcript entry, kept only when a reader runs with messages=True (excerpts and search)."""
    role: str  # user, assistant, or tool
    text: str
    ts: datetime
    uid: str | None = None


@dataclass
class Turn:
    agent: str
    session_id: str
    turn_id: str
    started: datetime
    prompt: str
    subagent: bool = False
    branch: str | None = None
    assistant_text: list[str] = field(default_factory=list)
    edits: list[Edit] = field(default_factory=list)
    commands: list[Command] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    plans: list[Plan] = field(default_factory=list)
    interrupted: bool = False
    messages: list[Message] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return f"{self.agent}:{self.session_id}:{self.turn_id}"


@dataclass
class Session:
    agent: str
    session_id: str
    path: str
    title: str | None
    cwd: str | None
    turns: list[Turn]
    pr_links: list[str] = field(default_factory=list)
    subagent: bool = False
    first_command: str | None = None

    @property
    def key(self) -> str:
        return f"{self.agent}:{self.session_id}"

    @property
    def started_at(self) -> datetime | None:
        return self.turns[0].started if self.turns else None

    @property
    def is_pair_run(self) -> bool:
        """Sessions started by /pair itself (including the background hook's runs)."""
        first = self.first_command or (self.turns[0].prompt if self.turns else "")
        return first.strip().split(" ")[0] in {"/pair", "/pair:pair"}
