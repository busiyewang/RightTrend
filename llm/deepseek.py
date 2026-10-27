"""DeepSeek Chat Completions 客户端(OpenAI 兼容)。

- 模型 ID 只从配置读取(DEEPSEEK_MODEL),不内置默认型号;`list_models()` 调官方 /models 核对。
- JSON 模式:response_format={"type":"json_object"},提示词里同时要求 JSON 并给示例。
- 有限次重试:429/5xx/网络错误;尊重 Retry-After。超时视为"不确定"(请求可能已计费)。
- 空响应、finish_reason=length(截断)、非 JSON 一律抛 LLMError,由调用方记为失败。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests

from app.config import Settings


class LLMError(RuntimeError):
    def __init__(self, code: str, msg: str, attempts: Optional[List[Dict]] = None):
        super().__init__(f"{code}: {msg}")
        self.code = code
        self.msg = msg
        self.attempts = attempts or []


@dataclass
class LLMResult:
    data: Dict
    raw_text: str
    model: str
    usage: Dict
    attempts: List[Dict] = field(default_factory=list)
    finish_reason: Optional[str] = None


class DeepSeekClient:
    def __init__(self, settings: Settings, session: Optional[requests.Session] = None,
                 sleep=time.sleep):
        self.s = settings
        self.sess = session or requests.Session()
        self.sleep = sleep

    @property
    def configured(self) -> bool:
        return self.s.llm_configured

    def _headers(self):
        return {"Authorization": f"Bearer {self.s.deepseek_api_key}", "Content-Type": "application/json"}

    def list_models(self) -> List[str]:
        if not self.s.deepseek_api_key:
            raise LLMError("not_configured", "未配置 DEEPSEEK_API_KEY")
        r = self.sess.get(self.s.deepseek_base_url + "/models", headers=self._headers(), timeout=20)
        if r.status_code != 200:
            raise LLMError(f"http_{r.status_code}", r.text[:200])
        return [m.get("id") for m in (r.json().get("data") or [])]

    def chat_json(self, system: str, user: str, max_tokens: Optional[int] = None) -> LLMResult:
        if not self.configured:
            raise LLMError("not_configured", "未配置 DEEPSEEK_API_KEY 或 DEEPSEEK_MODEL")
        body = {
            "model": self.s.deepseek_model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": self.s.deepseek_temperature,
            "max_tokens": max_tokens or self.s.deepseek_max_tokens,
            "stream": False,
        }
        if self.s.deepseek_json_mode:
            body["response_format"] = {"type": "json_object"}
        attempts: List[Dict] = []
        url = self.s.deepseek_base_url + "/chat/completions"
        for i in range(self.s.deepseek_max_retries + 1):
            t0 = time.time()
            att = {"n": i + 1, "started_at": int(t0 * 1000)}
            attempts.append(att)
            try:
                r = self.sess.post(url, json=body, headers=self._headers(),
                                   timeout=(10, self.s.deepseek_timeout))
            except requests.Timeout as e:
                # 读超时:请求可能已被服务端处理并计费 → 记为不确定
                att.update(outcome="uncertain_timeout", error=str(e)[:200])
                if i < self.s.deepseek_max_retries:
                    self.sleep(2 ** i)
                    continue
                raise LLMError("timeout", "DeepSeek 请求超时(结果不确定,可能已计费)", attempts)
            except requests.RequestException as e:
                att.update(outcome="network_error", error=str(e)[:200])
                if i < self.s.deepseek_max_retries:
                    self.sleep(2 ** i)
                    continue
                raise LLMError("network_error", str(e)[:200], attempts)
            att["status"] = r.status_code
            att["elapsed_ms"] = int((time.time() - t0) * 1000)
            if r.status_code == 429 or r.status_code >= 500:
                att["outcome"] = "retryable_http"
                if i < self.s.deepseek_max_retries:
                    ra = r.headers.get("Retry-After")
                    try:
                        delay = min(60.0, float(ra)) if ra else 2.0 ** (i + 1)
                    except ValueError:
                        delay = 2.0 ** (i + 1)
                    self.sleep(delay)
                    continue
                raise LLMError("rate_limited" if r.status_code == 429 else f"http_{r.status_code}",
                               r.text[:200], attempts)
            if r.status_code != 200:
                att["outcome"] = "http_error"
                raise LLMError(f"http_{r.status_code}", r.text[:300], attempts)
            try:
                j = r.json()
                choice = j["choices"][0]
                text = (choice.get("message") or {}).get("content") or ""
                finish = choice.get("finish_reason")
                usage = j.get("usage") or {}
                model = j.get("model") or self.s.deepseek_model
            except (ValueError, KeyError, IndexError, TypeError) as e:
                att["outcome"] = "bad_response"
                raise LLMError("bad_response", f"响应结构异常: {e}", attempts)
            att.update(outcome="ok", finish_reason=finish, usage=usage)
            if finish == "length":
                raise LLMError("truncated", "模型输出被截断(finish_reason=length)", attempts)
            if not text.strip():
                raise LLMError("empty", "模型返回空内容", attempts)
            try:
                data = json.loads(text)
            except ValueError:
                raise LLMError("invalid_json", f"模型输出不是合法 JSON: {text[:200]}", attempts)
            if not isinstance(data, dict):
                raise LLMError("invalid_json", "模型输出不是 JSON 对象", attempts)
            return LLMResult(data, text, model, usage, attempts, finish)
        raise LLMError("exhausted", "重试次数耗尽", attempts)
