"""Isolated browser acceptance fixture: python tests/preview_guidance.py."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import uvicorn
from app import storage
from app.main import create_app


async def reply(settings, messages):
    if '现在整理修改提案' in json.dumps(messages, ensure_ascii=False):
        discussion = json.loads(messages[-1]['content'].split('\n', 1)[1])
        originals = json.loads(messages[1]['content'].split('\n', 1)[1])
        yield json.dumps({'summary': '建议先解释，再在必要时提问。', 'changes': [{
            'material': 'teaching', 'content': originals['teaching'] + '\n- 解释请求先给出足够说明，再在必要时提问。\n',
            'reason': '用户明确表达长期讲解偏好。', 'evidence': [next(m['id'] for m in discussion if m['role'] == 'user')],
        }]}, ensure_ascii=False)
    else:
        for chunk in ('我理解，你希望先得到完整解释。', '这可以作为长期教学偏好，点击“整理修改建议”后核对，再决定是否应用。'):
            await asyncio.sleep(.15)
            yield chunk


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='trae-guidance-preview-') as temporary:
        root = Path(temporary)
        storage.save_settings(root, 'sk-synthetic-preview', storage.MODELS[0])
        uvicorn.run(create_app(root, reply), host='127.0.0.1', port=8766)
