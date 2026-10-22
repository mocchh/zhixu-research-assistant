"""Server-only model configuration; secrets are never included in API responses."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse


class ModelConfig:
    def __init__(self, directory: Path):
        self.path = directory / "settings.json"
        self.lock = threading.RLock()

    def get(self):
        values = {
            "base_url": os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            "model": os.getenv("LLM_MODEL", "qwen-plus"),
            "api_key": os.getenv("LLM_API_KEY", ""),
            "timeout_seconds": 90,
        }
        if self.path.exists():
            with self.lock:
                try:
                    saved = json.loads(self.path.read_text())
                    if not isinstance(saved, dict) or any(
                            name in saved and not isinstance(saved[name], str)
                            for name in ("model", "base_url", "api_key")):
                        raise ValueError("Invalid saved model settings")
                    values.update({key: saved[key] for key in values if key in saved})
                    # An existing local file owns credential state, including a
                    # missing/empty key. It cannot inherit a different env key.
                    values["api_key"] = saved.get("api_key", "")
                except (OSError, ValueError, TypeError):
                    # A corrupt/unreadable override must never reactivate an
                    # environment credential after the user disconnected.
                    values["api_key"] = ""
        return values

    def public(self):
        values = self.get()
        return {"model": values["model"], "base_url": values["base_url"], "has_api_key": bool(values["api_key"]), "timeout_seconds": values["timeout_seconds"]}

    @staticmethod
    def service_identity(base_url):
        """Bind credentials to the service path as well as its network origin."""
        parsed = urlparse(base_url.strip())
        return (parsed.scheme.lower(), (parsed.hostname or "").lower(),
                parsed.port or (443 if parsed.scheme == "https" else 80), parsed.path.rstrip("/"))

    def save(self, model, base_url, api_key=None, *, clear_api_key=False):
        try:
            parsed = urlparse(base_url.strip())
            invalid_port = parsed.port is not None and not 1 <= parsed.port <= 65535
        except ValueError:
            raise ValueError("请输入合法的模型服务地址。") from None
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("请输入合法的模型服务地址，例如 https://example.com/v1")
        if invalid_port:
            raise ValueError("模型服务端口必须在 1–65535 之间。")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("远程模型服务请使用 HTTPS；HTTP 仅用于本机模型")
        key = (api_key or "").strip()
        if clear_api_key and key:
            raise ValueError("清除密钥时不能同时填写新密钥。")
        if not model.strip():
            raise ValueError("模型名称不能为空")
        with self.lock:
            values = self.get()
            try:
                same_service = self.service_identity(values["base_url"]) == self.service_identity(base_url)
            except (ValueError, TypeError, AttributeError):
                same_service = False
            values.update(model=model.strip(), base_url=base_url.strip().rstrip("/"))
            # Persist the empty override too, so environment credentials cannot
            # reappear after disconnecting or switching provider and restarting.
            if clear_api_key or not same_service:
                values["api_key"] = ""
            if key:
                values["api_key"] = key
            fd, temp = tempfile.mkstemp(dir=self.path.parent, prefix="settings-", suffix=".tmp")
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w") as f:
                    json.dump(values, f, ensure_ascii=False, indent=2)
                os.replace(temp, self.path)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
            return self.public()
