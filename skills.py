"""Skills: folders of instructions the agent loads on demand (the SKILL.md format shared by Claude
Code, Hermes Agent and OpenClaw - agentskills.io).

    <skills dir>/<name>/SKILL.md      ---\\nname: ...\\ndescription: ...\\n---\\n<instructions>
                         other files   scripts, references, templates the instructions point to

Only names and descriptions go into the system prompt; the agent reads a skill's full
instructions with the `skill` tool when a task calls for it (progressive disclosure).
Searched, first match wins: ./.agent/skills, ~/.config/sysadmin-agent/skills, ~/.claude/skills.
"""
from dataclasses import dataclass
from pathlib import Path

USER_SKILLS = Path.home() / ".config/sysadmin-agent/skills"
CLAUDE_SKILLS = Path.home() / ".claude/skills"


@dataclass
class Skill:
    name: str
    description: str
    path: Path            # the skill's folder


def _frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    meta = {}
    for line in text[3:end].splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() and not key.startswith((" ", "\t")):
            meta[key.strip()] = value.strip().strip("'\"")
    return meta, text[end + 4:].lstrip("\n")


class Skills:
    def __init__(self, dirs: list[Path]) -> None:
        self.skills: dict[str, Skill] = {}
        for root in dirs:
            if not root.is_dir():
                continue
            for skill_md in sorted(root.glob("*/SKILL.md")):
                meta, _ = _frontmatter(skill_md.read_text(errors="replace"))
                name = meta.get("name") or skill_md.parent.name
                if name not in self.skills:
                    self.skills[name] = Skill(name, meta.get("description", "")[:500], skill_md.parent)

    @classmethod
    def discover(cls, cwd: str) -> "Skills":
        return cls([Path(cwd) / ".agent/skills", USER_SKILLS, CLAUDE_SKILLS])

    def describe(self) -> str:
        return "\n".join(f"- {s.name}: {s.description}" for s in self.skills.values())

    def load(self, name: str) -> tuple[str, bool]:
        skill = self.skills.get(name)
        if not skill:
            return f"No skill named {name!r}. Available: {', '.join(self.skills) or 'none'}.", True
        _, body = _frontmatter((skill.path / "SKILL.md").read_text(errors="replace"))
        others = sorted(str(p) for p in skill.path.rglob("*") if p.is_file() and p.name != "SKILL.md")[:50]
        listing = ("\n\nFiles in this skill (read them with read_file when the instructions refer to them):\n"
                   + "\n".join(others)) if others else ""
        return f"# Skill: {skill.name}\n(folder: {skill.path})\n\n{body}{listing}", False
