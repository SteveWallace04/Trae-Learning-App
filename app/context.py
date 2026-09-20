"""Read original learning documents; keep an exact, local request snapshot."""

import hashlib
import json
from pathlib import Path

from app.storage import atomic_write

BASIC_PROMPT = (
    "你是编程学习助手。用清楚、连贯的中文解释，根据学生的实际回答调整讲解。"
    "学生不理解时补足中间步骤，不把一次答对当成长期掌握。"
    "你没有代码运行工具，不得声称执行了代码；未执行的结果只能称为预测。"
    "不要代写学生需要独立完成的作业。"
)

# Explicit selection, not recursive file discovery or automatic link following.
MATERIALS = (
    ("core/teaching.md", "教学约定与已有词表"),
    ("core/learner-model.md", "解释表现与证据的方法"),
    ("templates/probe-template.md", "教学活动参考"),
    ("docs/examples/excellent-probability-dialogue.md", "历史教学示例"),
    ("core/profile.md", "个人背景与偏好"),
    ("core/goals.md", "个人目标与约束"),
    ("roadmap/main.md", "学习方向"),
    ("progress/calibration.md", "历史学习证据"),
    ("progress/mastery.md", "已有掌握判断"),
    ("progress/status.md", "原项目记录的断点"),
)

ADAPTATION = """你是这位学生的学习助手。以下原项目材料按原文提供，不是当前对话的新消息。
教学约定、能力观察方法和活动参考指导教学；画像、目标、路线和学习记录提供背景。
历史教学示例只示范教学决策，不是本次学生的回答或本次学习证据，不机械复刻回合结构；示例中的错误与后续纠正要结合理解。
本应用的实际能力边界（原文涉及工具操作时以此为准）：
- 只能根据本次提供的材料和对话回答，没有文件读写、代码执行或浏览工具。文件中的链接不代表你已读到链接目标；未附上的文件不能声称已读取。
- 可以展示供学生自行运行的代码和命令；未执行的结果只能称为预测，学生贴回的输出需注明来源。
- 程序会保存聊天，但目前不会更新画像、目标、断点、calibration 或 mastery。不要说“已记录/已更新断点”；如需整理只能说明这是对话中的草稿。
- progress/status.md 是原项目保存时的断点，不随网页聊天自动更新。若本次对话已继续推进或学生提出新问题，以本次实际对话为准，不反复拉回旧断点。
- 新对话不含其他聊天的消息。学生提出具体问题时直接围绕该问题教学；只有表达续学意图且当前对话没有更近的线索时，才从原项目断点接上。新建对话不等于忘记个人背景，也不强制沿用旧主题。
- 个人材料中的自报、历史判断与目标不是经过本次验证的事实；不能把历史记录冒充本轮新证据。
本次提供的文件如下（文件名仅用于辨认来源）：
"""


class ContextError(Exception):
    """A local material problem that must stop the request before billing."""


def prepare(root: Path):
    config = root / "data" / "learning-source.json"
    if not config.exists():
        return {"mode": "basic", "source": "", "materials": [], "system_prompt": BASIC_PROMPT}
    try:
        settings = json.loads(config.read_text(encoding="utf-8"))
        source = Path(settings["root"])
        if not source.is_absolute():
            source = root / source
        source = source.resolve()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ContextError("学习资料位置配置无效，请检查 data/learning-source.json；尚未调用模型。") from exc
    materials = []
    for path, title in MATERIALS:
        try:
            content = (source / path).read_bytes().decode("utf-8")
            if not content.strip():
                raise ValueError("empty material")
        except (OSError, ValueError) as exc:
            raise ContextError(f"无法完整读取学习材料 {path}，请检查文件；尚未调用模型。") from exc
        materials.append({"path": path, "title": title, "content": content})
    prompt = ADAPTATION + "\n".join(path for path, _ in MATERIALS)
    for material in materials:
        prompt += f"\n\n--- 原文开始：{material['path']} ---\n{material['content']}\n--- 原文结束：{material['path']} ---"
    return {"mode": "linked", "source": str(source), "materials": materials, "system_prompt": prompt}


def save_snapshot(root: Path, prepared: dict):
    text = json.dumps(prepared, ensure_ascii=False, indent=2) + "\n"
    identifier = hashlib.sha256(text.encode("utf-8")).hexdigest()
    path = root / "data" / "contexts" / f"{identifier}.json"
    if not path.exists():
        atomic_write(path, text)
    return identifier
