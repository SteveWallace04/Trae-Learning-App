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
    recover_pending(root)
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


def initialize(root):
    """Only a genuinely empty data directory is a new installation."""
    recover_pending(root)
    if (root / 'data/learning-source.json').exists():
        return
    data = root / 'data'
    entries = [p for p in data.iterdir() if p.name != '.gitkeep'] if data.exists() else []
    if entries:
        # Keep pre-material legacy installations in their explicit basic mode.
        if (data / 'learning-materials').exists():
            raise ValueError('材料目录存在但来源配置缺失，请恢复配置；未覆盖材料。')
        return
    defaults = Path(__file__).resolve().parent.parent / 'defaults'
    files = {f'core/{key}.md': (defaults / f'{key}.md').read_text(encoding='utf-8') for key in EDITABLE}
    # Staging plus rename prevents a half-written source directory from being used.
    data.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.initialize-', dir=data) as temporary:
        staged = Path(temporary) / 'materials'
        for relative, text in files.items():
            storage.atomic_write(staged / relative, text)
        staged.rename(data / 'learning-materials')
    storage.atomic_write(data / 'learning-source.json', json.dumps({
        'root': 'data/learning-materials', 'managed': True, 'materials': list(files),
    }, ensure_ascii=False))


def transaction_path(root):
    return root / 'data/material-change-pending.json'


def recover_pending(root):
    """Roll forward a durable accepted change before any material read/write."""
    path = transaction_path(root)
    if not path.exists():
        return
    record = json.loads(path.read_text(encoding='utf-8'))
    changes = record['changes']
    if not changes or any(key not in EDITABLE for key in changes):
        raise ValueError('材料变更记录损坏，请检查本地文件。')
    # Do not silently clobber edits made outside the app during recovery.
    for key, change in changes.items():
        current = owned_path(root, key).read_bytes().decode('utf-8')
        if current not in (change['before'], change['after']):
            raise HTTPException(409, '待恢复变更与材料不一致，请保留文件并检查；未覆盖外部修改。')
    for key, change in changes.items():
        target = owned_path(root, key)
        if target.read_bytes().decode('utf-8') != change['after']:
            storage.atomic_write(target, change['after'])
    storage.atomic_write(root / 'data/material-change-last.json', json.dumps(record, ensure_ascii=False))
    path.unlink()


def last_change(root):
    recover_pending(root)
    path = root / 'data/material-change-last.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None


def apply_changes(root, identifier, changes, *, undo_of=None):
    """No await: version checks, journal and writes are one service operation."""
    previous = last_change(root)
    if previous and previous['id'] == identifier:
        return previous  # Retrying a successful request after a lost response.
    actual = {}
    for key, change in changes.items():
        current = read(root, key)
        if current['revision'] != change['revision']:
            raise HTTPException(409, '材料已变化，请重新整理建议；未覆盖新编辑。')
        if not change['content'].strip():
            raise HTTPException(400, '材料不能为空。')
        actual[key] = {'before': current['content'], 'after': change['content']}
    record = {'id': identifier, 'undo_of': undo_of, 'changes': actual}
    storage.atomic_write(transaction_path(root), json.dumps(record, ensure_ascii=False))
    recover_pending(root)
    return record


def undo(root, identifier):
    from uuid import uuid4
    previous = last_change(root)
    if previous and previous.get('undo_of') == identifier:
        return previous
    if not previous or previous['id'] != identifier or previous.get('undo_of'):
        raise HTTPException(409, '可撤销的变更已变化，请重新读取。')
    changes = {}
    for key, change in previous['changes'].items():
        current = read(root, key)
        if current['content'] != change['after']:
            raise HTTPException(409, '应用后材料已有编辑，不能直接撤销覆盖。')
        changes[key] = {'revision': current['revision'], 'content': change['before']}
    return apply_changes(root, str(uuid4()), changes, undo_of=identifier)
