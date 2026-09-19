"""DeepSeek's streaming chat API, with no automatic paid retries."""

import json

import httpx

API_URL = "https://api.deepseek.com/chat/completions"
SYSTEM_PROMPT = (
    "你是编程学习助手。用清楚、连贯的中文解释，根据学生的实际回答调整讲解。"
    "学生不理解时补足中间步骤，不把一次答对当成长期掌握。"
    "你没有代码运行工具，不得声称执行了代码；未执行的结果只能称为预测。"
    "不要代写学生需要独立完成的作业。"
)


class ModelError(Exception):
    """A safe, user-facing failure without provider payloads or credentials."""


async def stream_reply(settings, messages):
    payload = {
        "model": settings["model"],
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *messages],
        "stream": True,
        "thinking": {"type": "disabled"},
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15)) as client:
            async with client.stream("POST", API_URL, headers={"Authorization": f"Bearer {settings['api_key']}"}, json=payload) as response:
                if response.status_code != 200:
                    errors = {
                        400: "模型拒绝了请求，请检查模型设置或对话是否过长。",
                        401: "密钥无效，请在设置中更换 DeepSeek API Key。",
                        402: "DeepSeek 账户余额不足，请检查账户。",
                        403: "当前账户无法访问所选模型。",
                        413: "对话超过服务允许的长度，当前不会自动删减前文。",
                        429: "请求过于频繁，请稍后手动重试。",
                    }
                    raise ModelError(errors.get(response.status_code, "DeepSeek 服务暂时不可用，请稍后手动重试。"))
                finished = False
                has_text = False
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    if "error" in chunk:
                        raise ModelError("模型返回了错误，已保留收到的文字。")
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    choice = choices[0]
                    text = choice.get("delta", {}).get("content")
                    if text:
                        has_text = True
                        yield text
                    reason = choice.get("finish_reason")
                    if reason == "stop":
                        finished = True
                    elif reason:
                        raise ModelError("回答因长度限制或服务限制未能完整生成，已保留收到的文字。")
                if not finished or not has_text:
                    raise ModelError("回答没有完整结束，请检查已收到的内容后重试。")
    except httpx.TimeoutException as exc:
        raise ModelError("等待 DeepSeek 超时，问题已保留，可以手动重试。") from exc
    except httpx.HTTPError as exc:
        raise ModelError("无法连接 DeepSeek，问题已保留，请检查网络后重试。") from exc
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ModelError("模型返回的内容格式异常，已保留收到的文字。") from exc
