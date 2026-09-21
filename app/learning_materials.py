"""Explicit migration and editing of app-owned teaching materials."""
import hashlib
import json
from pathlib import Path
import tempfile

from fastapi import HTTPException
from pydantic import BaseModel, Field

from app import context, storage

EDITABLE = {
    'profile': ('core/profile.md', '个人背景'),
    'teaching': ('core/teaching.md', '教学约定'),
    'goals': ('core/goals.md', '学习目标'),
}


class MaterialInput(BaseModel):
    revision: str = Field(pattern=r'^[0-9a-f]{64}$')
    content: str = Field(min_length=1, max_length=200_000)


def owned_path(root, material_id):
    if material_id not in EDITABLE:
        raise HTTPException(404, '这份材料不支持网页编辑。')
    try:
        source, owned = context.source_config(root)
    except context.ContextError as exc:
        raise HTTPException(503, str(exc)) from exc
    if not owned:
        raise HTTPException(409, '当前材料尚未迁入应用，原目录保持只读。')
    path = source / EDITABLE[material_id][0]
    if not path.resolve().is_relative_to(source):
        raise HTTPException(409, '材料路径指向应用目录之外，已停止操作。')
    return path


def read(root, material_id):
    path = owned_path(root, material_id)
    try:
        raw = path.read_bytes()
        content = raw.decode('utf-8')
    except (OSError, UnicodeError) as exc:
        raise HTTPException(503, '无法读取材料，请检查文件；未修改内容。') from exc
    return {'id': material_id, 'title': EDITABLE[material_id][1], 'content': content,
            'revision': hashlib.sha256(raw).hexdigest()}


def save(root, material_id, body):
    current = read(root, material_id)
    if not body.content.strip():
        raise HTTPException(400, '材料不能为空，请保留有效内容。')
    if current['content'].replace('\r\n', '\n') == body.content.replace('\r\n', '\n'):
        return current  # Includes retry after a lost successful response.
    if current['revision'] != body.revision:
        raise HTTPException(409, '材料已在其他窗口或文件中修改。当前编辑仍保留，请先复制，再重新读取新版；未覆盖文件。')
    storage.atomic_write(owned_path(root, material_id), body.content)
    return read(root, material_id)


def migrate(root):
    root = root.resolve()
    source, owned = context.source_config(root)
    if owned:
        context.prepare_materials(root)  # Ensure an existing migration is readable, never overwrite it.
        return {'status': 'already_migrated', 'files': len(context.MATERIALS)}
    if source is None:
        raise ValueError('尚未配置原材料来源，无法迁移。')
    destination = root / 'data/learning-materials'
    if destination.exists():
        raise FileExistsError('应用材料目录已存在；为保护已有编辑，不覆盖，请检查迁移状态。')
    contents = {}
    for relative, _ in context.MATERIALS:
        raw = (source / relative).read_bytes()
        if not raw.decode('utf-8').strip():
            raise ValueError(f'材料为空：{relative}')
        contents[relative] = raw
    # Publish only a complete byte-for-byte copy, then atomically change the source config.
    with tempfile.TemporaryDirectory(prefix='.materials-migration-', dir=root / 'data') as temporary:
        staged = Path(temporary) / 'learning-materials'
        for relative, raw in contents.items():
            path = staged / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            if path.read_bytes() != raw:
                raise OSError(f'复制核对失败：{relative}')
        staged.rename(destination)
    storage.atomic_write(root / 'data/learning-source.json', json.dumps(
        {'root': 'data/learning-materials', 'managed': True}, ensure_ascii=False, indent=2) + '\n')
    return {'status': 'migrated', 'files': len(contents), 'byte_identical': True}


if __name__ == '__main__':
    print(json.dumps(migrate(Path(__file__).resolve().parent.parent), ensure_ascii=False))
