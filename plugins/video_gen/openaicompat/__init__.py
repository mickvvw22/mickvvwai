"""通用视频生成后端：任何「OpenAI 兼容视频 API」的服务。

适用对象（以后接新模型/新站，不用改代码，只改配置）：

* New API / one-api 系中转站（本机在用的就是这种）
* Sora 反代、DeepInfra、任何实现了
  ``POST /videos`` → ``GET /videos/{id}`` → ``GET /videos/{id}/content`` 的服务

## 配置（全部可选，先命中先用）

```yaml
video_gen:
  provider: openaicompat
  endpoint: gbccc              # 复用 providers: 里已配好的供应商（拿它的 base_url + key）
  # base_url: https://xxx/v1   # 或者直接写地址（优先于 endpoint）
  # key_env: MY_API_KEY        # 或者指定环境变量名；也可写 key: sk-xxx
  # models: [wan2.2-t2v-plus]  # 可选：跳过自动发现，手动钉死模型清单
  model: wan2.2-t2v-plus       # 默认模型；不给则用目录里的第一个
```

## 通用适配（重点）

上游对「分辨率 / 时长」的取值各家不同（本机那个站只认 ``480P``/``1080P`` 大写；
别家认 ``720p``、``1280x720``，时长有的只认 5/10）。所以这里不写死任何枚举：

1. 先按用户给的参数原样发一次；
2. 上游 400 拒绝时，**从它的报错里解析出允许值**（形如
   ``resolution must be one of 480P, 1080P`` / ``duration must be one of 5, 10``），
   挑最接近的那个重试；
3. 最多重试 2 次（分辨率一次、时长一次）。

这样接新站不需要改代码，也会把「到底允许什么」写进日志，方便排查。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from agent.video_gen_provider import OpenAICompatibleVideoGenProvider

logger = logging.getLogger(__name__)

# 聚合站会把聊天/图像模型也列出来，靠这些特征挑出视频模型
_VIDEO_HINTS = ("t2v", "i2v", "video", "wan2", "kling", "veo", "sora", "hailuo", "seedance", "pika", "luma")
_DEFAULT_BASE_URL = "https://api.openai.com/v1"

# "resolution must be one of 480P, 1080P" / "duration must be one of 5, 10"
_ALLOWED_RE = re.compile(
    r"(resolution|size|duration|seconds)[^.]{0,40}?must be one of\s*([0-9A-Za-zxX,，、\s]+)", re.I)


def parse_allowed(message: str) -> dict[str, list[str]]:
    """从上游报错里解析 ``{"resolution": ["480P","1080P"], "duration": [...]}``（拿不到就是空）。"""
    out: dict[str, list[str]] = {}
    for field, raw in _ALLOWED_RE.findall(message or ""):
        key = "duration" if field.lower() in ("duration", "seconds") else "resolution"
        # 只留「像取值」的 token（必须以数字开头：480P / 1080 / 1280x720），
        # 否则报错尾部的英文词（"...1080P at conversion"）会被当成允许值混进来。
        values = [v for v in re.split(r"[,，、\s]+", raw)
                  if v.strip() and re.match(r"^\d", v.strip())]
        if values:
            out[key] = values
    return out


def pick_closest(requested: str, allowed: list[str]) -> Optional[str]:
    """在允许值里挑最接近 *requested* 的（数字相等 → 数字最近 → 第一个）。"""
    if not allowed:
        return None
    req = str(requested or "").strip()
    if req in allowed:
        return req
    numbered: list[tuple[int, str]] = []
    for value in allowed:
        digits = re.findall(r"\d+", value)
        if digits:
            numbered.append((int(digits[0]), value))
    if numbered:
        want_digits = re.findall(r"\d+", req)
        if want_digits:
            want = int(want_digits[0])
            exact = [v for d, v in numbered if d == want]
            if exact:
                return exact[0]
            return min(numbered, key=lambda dv: abs(dv[0] - want))[1]
        return numbered[0][1]
    return allowed[0]


class OpenAICompatVideoGenProvider(OpenAICompatibleVideoGenProvider):
    """配置驱动的 OpenAI 兼容视频后端（换站/换模型只改配置）。"""

    name = "openaicompat"
    display_name = "OpenAI-compatible Video"
    _env_key = "VIDEO_GEN_API_KEY"
    _default_base_url = _DEFAULT_BASE_URL
    _poll_interval_s = 10.0
    _poll_deadline_s = 1800.0

    # ---------------------------------------------------------------- 配置读取

    @staticmethod
    def _cfg(key: str) -> Any:
        try:
            from hermes_cli.config import cfg_get, load_config
            return cfg_get(load_config(), "video_gen", key)
        except Exception:
            return None

    def _named_entry(self) -> dict[str, Any]:
        """``video_gen.endpoint`` 指向 providers:/custom_providers: 里的某个名字。"""
        name = self._cfg("endpoint")
        if not isinstance(name, str) or not name.strip():
            return {}
        try:
            from hermes_cli.runtime_provider import _get_named_custom_provider
            return _get_named_custom_provider(name.strip()) or {}
        except Exception as exc:
            logger.warning("video_gen.endpoint=%r 解析失败: %s", name, exc)
            return {}

    def _base_url(self) -> str:
        cfg_url = self._cfg("base_url")
        if isinstance(cfg_url, str) and cfg_url.strip():
            return cfg_url.strip().rstrip("/")
        entry = self._named_entry()
        for key in ("base_url", "api", "url"):
            val = entry.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip().rstrip("/")
        from_secret = self._env(f"{self.name.upper()}_BASE_URL") or self._env("VIDEO_GEN_BASE_URL")
        return (from_secret or self._default_base_url).rstrip("/")

    def _env(self, name: str) -> str:
        import os
        try:
            from utils import get_secret_str  # 走 profile 密钥作用域
            val = get_secret_str(name)
        except Exception:
            val = ""
        return (val or os.environ.get(name, "") or "").strip()

    def _api_key(self) -> str:
        key_env = self._cfg("key_env")
        if isinstance(key_env, str) and key_env.strip():
            val = self._env(key_env.strip())
            if val:
                return val
        literal = self._cfg("key")
        if isinstance(literal, str) and literal.strip():
            return literal.strip()
        entry = self._named_entry()
        key_env2 = str(entry.get("key_env") or "").strip()
        if key_env2 and (val := self._env(key_env2)):
            return val
        literal2 = str(entry.get("api_key") or "").strip()
        if literal2:
            return literal2
        return self._env("VIDEO_GEN_API_KEY")

    # ---------------------------------------------------------------- 模型目录

    def list_models(self) -> list[dict[str, Any]]:
        """``video_gen.models`` 手动指定 → 否则探 ``/models`` 按特征过滤 → 否则空目录。"""
        manual = self._cfg("models")
        if isinstance(manual, str):
            manual = [m.strip() for m in manual.split(",") if m.strip()]
        if isinstance(manual, (list, tuple)) and manual:
            return [{"id": str(m), "display": str(m)} for m in manual if str(m).strip()]
        ids: list[str] = []
        try:
            import httpx
            resp = httpx.get(
                f"{self._base_url()}/models",
                headers={"Authorization": f"Bearer {self._api_key()}"} if self._api_key() else {},
                timeout=20.0,
            )
            resp.raise_for_status()
            ids = [str(m.get("id")) for m in (resp.json().get("data") or []) if m.get("id")]
        except Exception as exc:
            logger.debug("%s: /models 探测失败: %s", self.name, exc)
        return [{"id": mid, "display": mid,
                 "strengths": "文生视频" if "t2v" in mid.lower() else ("图生视频" if "i2v" in mid.lower() else "视频生成")}
                for mid in ids if any(h in mid.lower() for h in _VIDEO_HINTS)]

    def default_model(self) -> Optional[str]:
        configured = self._cfg("model")
        if isinstance(configured, str) and configured.strip():
            return configured.strip()
        return super().default_model()

    def capabilities(self) -> dict[str, Any]:
        # 保守声明：真正的可用取值由上游报错驱动（parse_allowed + 重试），这里不下断言
        return {
            "modalities": ["text", "image"],
            "aspect_ratios": ["16:9", "9:16", "1:1"],
            "resolutions": ["480P", "720p", "1080P"],
            "max_duration": 10, "min_duration": 1,
            "supports_audio": False, "supports_negative_prompt": False,
            "supports_seed": False, "supports_upscale": False, "max_reference_images": 0,
        }

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": "OpenAI-compatible Video",
            "badge": "self-hosted",
            "tag": "任意 OpenAI 兼容视频服务：配 video_gen.base_url / endpoint 即可（New API、one-api、Sora 反代…）",
            "env_vars": [{"key": "VIDEO_GEN_API_KEY", "prompt": "（可选）服务 API key", "url": ""}],
        }

    # ---------------------------------------------------------------- 生成 + 自适应

    def generate(self, prompt: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """先按原参数跑；上游因分辨率/时长取值不合规拒绝时，按它给的允许值重试。"""
        result = super().generate(prompt, *args, **kwargs)
        attempted = {"resolution": kwargs.get("resolution"), "duration": kwargs.get("duration")}
        for _ in range(2):
            if result.get("success"):
                return result
            err = str(result.get("error") or "")
            allowed = parse_allowed(err)
            retried = False
            for field, values in allowed.items():
                if field == "resolution":
                    better = pick_closest(attempted["resolution"], values)
                    if better and better != attempted["resolution"]:
                        logger.info("%s: 分辨率 %r 不被接受，改用上游允许的 %r（允许值 %s）",
                                    self.name, attempted["resolution"], better, values)
                        attempted["resolution"] = better
                        kwargs["resolution"] = better
                        retried = True
                        break
                elif field == "duration":
                    better = pick_closest(attempted["duration"], values)
                    if better and str(better) != str(attempted["duration"]):
                        logger.info("%s: 时长 %r 不被接受，改用上游允许的 %r（允许值 %s）",
                                    self.name, attempted["duration"], better, values)
                        attempted["duration"] = int(better) if str(better).isdigit() else attempted["duration"]
                        kwargs["duration"] = attempted["duration"]
                        retried = True
                        break
            if not retried:
                return result  # 不是取值问题，别重试（免得白烧钱）
            result = super().generate(prompt, *args, **kwargs)
        return result


def register(ctx) -> None:
    """插件入口。"""
    ctx.register_video_gen_provider(OpenAICompatVideoGenProvider())