import re
from dataclasses import dataclass
from pathlib import Path

from configs import WORKDIR

@dataclass
class SkillManifest:
    name: str
    description: str
    path: Path


@dataclass
class SkillDocument:
    manifest: SkillManifest
    body: str


class SkillManager:
    def __new__(cls, skills_dir: Path | None = None):
        if not hasattr(cls, "instance"):
            cls.instance = super(SkillManager, cls).__new__(cls)
        return cls.instance

    def __init__(self, skills_dir: Path | None = None):
        self.skills_dir = skills_dir or (WORKDIR / "skills")
        self.documents: dict[str, SkillDocument] = {}

        self._load_all()


    def _load_all(self) -> None:
        if not self.skills_dir.exists():
            return

        for path in sorted(self.skills_dir.rglob("SKILL.md")):
            meta, body = self._parse_frontmatter(path.read_text())
            name = meta.get("name", path.parent.name)
            description = meta.get("description", "No description")
            manifest = SkillManifest(name=name, description=description, path=path)
            self.documents[name] = SkillDocument(manifest=manifest, body=body.strip())


    def _parse_frontmatter(self, text: str) -> tuple[dict, str]:
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if not match:
            return {}, text
        meta = {}
        for line in match.group(1).strip().splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
        return meta, match.group(2)


    def skill_describe_available(self) -> str:
        if not self.documents:
            return "(no skills available)"
        lines = []
        for name in sorted(self.documents):
            manifest = self.documents[name].manifest
            lines.append(f"- {manifest.name}: {manifest.description}")
        return "# Available skills:\n" + "\n".join(lines)


    def load_full_skill_body(self, name: str) -> str:
        """
            这个作为工具调用结果输出, 实际上skill只是一个说明书
            当Agent想要调用skill的时候, 实际上它得到的是个说明书

        Args:
            name (str): skill name

        Returns:
            str: skill body
        """
        document = self.documents.get(name)
        if not document:
            known = ", ".join(sorted(self.documents)) or "(none)"
            return f"Error: Unknown skill '{name}'. Available skills: {known}"
        return (
            f"<skill name=\"{document.manifest.name}\">\n"
            f"{document.body}\n"
            "</skill>"
        )

skill_manager = SkillManager()
