#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


@dataclass(frozen=True)
class Task:
    question_id: str
    title: str
    description: str
    attachment_filenames: list[str]
    score_criteria: list[dict[str, Any]]

# - browser_screenshot saves files as browser_screenshot_XXX.png in the code execution environment; use these exact paths.

DEFAULT_SYSTEM_PROMPT = """
Execution policy:
- Do not fabricate, simulate, or placeholder any result.
- Never rename plain text files to binary extensions like .xlsx/.png/.jpg/.pdf/.doc.
- If a required website blocks simple HTTP requests, use browser tools first (browser_*), then retry.
- For anti-bot stability, collect all required data from one site in as few navigations as possible before switching sites.
- If a page shows Cloudflare/security verification, wait and retry; do not keep scrolling/chasing text on the challenge page.
- The browser is only one possible way to obtain information. If accessing the website becomes difficult or time-consuming, switch strategy immediately and complete the deliverables using other available knowledge.
- Always call finish before max turns and include all produced output file paths.
- If you still cannot access required sources after retries, report failure clearly and do not claim completion.
- Produce real files with valid binary formats for requested outputs.
""".strip()


def _read_tasks(path: Path) -> list[Task]:
    tasks: list[Task] = []
    with path.open("r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at line {lineno}: {exc}") from exc

            qid = str(item.get("question_id", "")).strip()
            if not qid:
                raise ValueError(f"Missing question_id at line {lineno}")

            attachments_raw = item.get("attachment_filenames") or []
            attachments = [str(x) for x in attachments_raw if str(x).strip()]
            score_criteria_raw = item.get("score_criteria") or []
            score_criteria = [x for x in score_criteria_raw if isinstance(x, dict)]

            tasks.append(
                Task(
                    question_id=qid,
                    title=str(item.get("title", "")).strip(),
                    description=str(item.get("description", "")).strip(),
                    attachment_filenames=attachments,
                    score_criteria=score_criteria,
                )
            )
    return tasks


def _parse_question_ids(raw: str) -> list[str]:
    if not raw.strip():
        return []
    return [token.strip() for token in raw.split(",") if token.strip()]


def _select_tasks(tasks: list[Task], question_ids: list[str], limit: int) -> list[Task]:
    if question_ids:
        by_id = {task.question_id: task for task in tasks}
        selected: list[Task] = []
        missing: list[str] = []
        for qid in question_ids:
            task = by_id.get(qid)
            if task is None:
                missing.append(qid)
                continue
            selected.append(task)
        if missing:
            raise ValueError(f"Unknown question_id(s): {', '.join(missing)}")
        return selected

    if limit <= 0:
        raise ValueError("--limit must be >= 1 when --question-ids is empty")
    return tasks[:limit]


def _build_prompt(task: Task, include_score_criteria: bool) -> str:
    lines: list[str] = [
        f"Task ID: {task.question_id}",
        f"Title: {task.title}",
        "",
        "Instruction:",
        task.description,
        "",
        "Requirements:",
        "- Complete the task end-to-end.",
        "- Use provided attachments when available.",
        "- Generate all required deliverables with exact filenames requested by the task.",
    ]

    if include_score_criteria and task.score_criteria:
        lines.extend(["", "Scoring hints:"])
        for idx, criterion in enumerate(task.score_criteria, start=1):
            content = str(criterion.get("content", "")).strip()
            score = criterion.get("score")
            lines.append(f"{idx}. [{score}] {content}")

    return "\n".join(lines).strip()


def _infer_required_outputs(task: Task) -> list[str]:
    text = "\n".join(
        [
            task.title or "",
            task.description or "",
            " ".join(str(c.get("content", "")) for c in task.score_criteria),
        ]
    )
    names: list[str] = []

    # Bracketed file names like [foo.xlsx].
    for m in re.finditer(r"\[([^\]\n]+\.[A-Za-z0-9]{2,5})\]", text):
        names.append(m.group(1).strip())

    # Phrases like "save as foo.png" / "named foo.xlsx" / "format foo.doc".
    for pat in [
        r"save(?:d)?(?:\s+(?:the\s+\w+|it))?\s+as\s+([A-Za-z0-9 _().\-]+\.[A-Za-z0-9]{2,5})",
        r"name(?:d)?\s+(?:the\s+file\s+)?as\s+([A-Za-z0-9 _().\-]+\.[A-Za-z0-9]{2,5})",
        r"format\s+([A-Za-z0-9 _().\-]+\.[A-Za-z0-9]{2,5})",
    ]:
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            names.append(m.group(1).strip(" .,\"'"))

    dedup: list[str] = []
    seen: set[str] = set()
    for name in names:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        dedup.append(name)
    return dedup


def _build_attachment_index(search_root: Path) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    if not search_root.exists():
        return index

    for path in search_root.rglob("*"):
        if not path.is_file():
            continue
        index.setdefault(path.name, []).append(path.resolve())
    return index


def _resolve_input_files(
    task: Task,
    attachment_dir: Path,
    attachment_index: dict[str, list[Path]],
) -> list[Path]:
    files: list[Path] = []
    missing: list[str] = []
    for name in task.attachment_filenames:
        p = (attachment_dir / name).resolve()
        if p.exists():
            files.append(p)
            continue

        candidates = attachment_index.get(name, [])
        if len(candidates) == 1:
            files.append(candidates[0])
            continue
        if len(candidates) > 1:
            preferred = [c for c in candidates if task.question_id in c.name]
            files.append((preferred or candidates)[0])
            continue

        missing.append(name)

    if missing:
        raise FileNotFoundError(
            f"Missing attachments for {task.question_id}: " + ", ".join(missing)
        )
    return files


def _to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(v) for v in value]

    for method_name in ("model_dump", "dict", "to_dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                return _to_jsonable(method())
            except Exception:
                pass

    annotations = getattr(value, "__annotations__", None)
    if isinstance(annotations, dict) and annotations:
        out: dict[str, Any] = {}
        for key in annotations:
            if hasattr(value, key):
                out[key] = _to_jsonable(getattr(value, key))
        if out:
            return out

    return str(value)


def _parse_bool(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    v = raw.strip().lower()
    if v in {"1", "true", "yes", "on"}:
        return True
    if v in {"0", "false", "no", "off"}:
        return False
    return default


def _build_browser_extra_args(
    *,
    profile_dir: Path | None,
    browser_user_agent: str | None,
    browser_timezone: str | None,
) -> list[str]:
    args = [
        "--disable-blink-features=AutomationControlled",
        "--window-size=1920,1080",
        "--lang=en-US,en",
    ]
    if profile_dir is not None:
        args.append(f"--user-data-dir={profile_dir}")
    if browser_user_agent:
        args.append(f"--user-agent={browser_user_agent}")
    if browser_timezone:
        args.append(f"--timezone={browser_timezone}")
    proxy = (
        os.getenv("HTTPS_PROXY")
        or os.getenv("HTTP_PROXY")
        or os.getenv("ALL_PROXY")
        or ""
    ).strip()
    if proxy:
        args.append(f"--proxy-server={proxy}")
    return args


async def _run_task_with_stirrup(
    *,
    model: str,
    api_key: str | None,
    base_url: str | None,
    prompt: str,
    input_files: list[Path],
    output_dir: Path,
    max_turns: int,
    client_timeout_seconds: int,
    web_timeout_seconds: int,
    brave_api_key: str | None,
    system_prompt: str | None,
    browser_headless: bool,
    browser_cdp_url: str | None,
    browser_profile_dir: Path | None,
    browser_user_agent: str | None,
    browser_timezone: str | None,
    cf_retry_attempts: int,
    cf_retry_wait_seconds: int,
) -> dict[str, Any]:
    from stirrup import Agent
    from stirrup.clients.chat_completions_client import ChatCompletionsClient
    from stirrup.core.models import ImageContentBlock, Tool, ToolResult
    from stirrup.tools.browser_use import BrowserUseToolProvider
    from stirrup.tools.code_backends.local import LocalCodeExecToolProvider
    from stirrup.tools.view_image import ViewImageToolProvider
    from stirrup.tools.web import WebToolProvider

    class PersistingBrowserUseToolProvider(BrowserUseToolProvider):
        """Wrap browser_screenshot so screenshots are persisted as real files."""

        def __init__(
            self,
            *,
            code_provider: LocalCodeExecToolProvider,
            drop_search_tool: bool = True,
            cf_retry_attempts: int = 2,
            cf_retry_wait_seconds: int = 8,
            **kwargs: Any,
        ) -> None:
            super().__init__(**kwargs)
            self._code_provider = code_provider
            self._drop_search_tool = drop_search_tool
            self._cf_retry_attempts = max(cf_retry_attempts, 0)
            self._cf_retry_wait_seconds = max(cf_retry_wait_seconds, 1)
            self._screenshot_idx = 0
            self.cf_challenge_detected = False
            self.cf_challenge_unresolved = False

        @staticmethod
        def _content_to_text(content: Any) -> str:
            if isinstance(content, str):
                return content.lower()
            if isinstance(content, list):
                return "\n".join(str(x) for x in content).lower()
            return str(content).lower()

        @classmethod
        def _looks_like_cloudflare(cls, content: Any) -> bool:
            text = cls._content_to_text(content)
            markers = (
                "performing security verification",
                "cloudflare security challenge",
                "cf-chl-widget",
                "ray id",
                "checking your browser",
            )
            return any(m in text for m in markers)

        async def _maybe_recover_cloudflare(
            self,
            *,
            current_result: ToolResult[Any],
            snapshot_tool: Tool[Any, Any] | None,
            wait_tool: Tool[Any, Any] | None,
            get_url_tool: Tool[Any, Any] | None,
            navigate_tool: Tool[Any, Any] | None,
        ) -> ToolResult[Any]:
            if not self._looks_like_cloudflare(current_result.content):
                return current_result

            self.cf_challenge_detected = True
            result = current_result

            for _ in range(self._cf_retry_attempts):
                if wait_tool is not None:
                    wait_params = wait_tool.parameters(seconds=self._cf_retry_wait_seconds)
                    wait_ret = wait_tool.executor(wait_params)
                    if asyncio.iscoroutine(wait_ret):
                        await wait_ret

                url: str | None = None
                if get_url_tool is not None:
                    url_ret = get_url_tool.executor(get_url_tool.parameters())
                    if asyncio.iscoroutine(url_ret):
                        url_ret = await url_ret
                    text = self._content_to_text(url_ret.content)  # type: ignore[union-attr]
                    m = re.search(r"current url:\s*(\S+)", text, flags=re.IGNORECASE)
                    if m:
                        url = m.group(1)

                if url and navigate_tool is not None and url != "about:blank":
                    nav_params = navigate_tool.parameters(url=url, new_tab=False)
                    nav_ret = navigate_tool.executor(nav_params)
                    if asyncio.iscoroutine(nav_ret):
                        await nav_ret

                if snapshot_tool is not None:
                    snap_ret = snapshot_tool.executor(snapshot_tool.parameters())
                    if asyncio.iscoroutine(snap_ret):
                        snap_ret = await snap_ret
                    result = snap_ret
                    if not self._looks_like_cloudflare(result.content):
                        self.cf_challenge_unresolved = False
                        return result

            self.cf_challenge_unresolved = True
            return ToolResult(
                content=(
                    f"{current_result.content}\n[cloudflare] challenge still active after "
                    f"{self._cf_retry_attempts} auto-retries; continue with other sources or finish with failure."
                ),
                success=current_result.success,
                metadata=current_result.metadata,
            )

        async def __aenter__(self) -> list[Tool[Any, Any]]:
            tools = await super().__aenter__()
            screenshot_tool_name = self._tool_name("screenshot")
            search_tool_name = self._tool_name("search")
            snapshot_tool_name = self._tool_name("snapshot")
            wait_tool_name = self._tool_name("wait")
            get_url_tool_name = self._tool_name("get_url")
            navigate_tool_name = self._tool_name("navigate")
            tool_map = {t.name: t for t in tools}
            wrapped_tools: list[Tool[Any, Any]] = []

            for tool in tools:
                if self._drop_search_tool and tool.name == search_tool_name:
                    continue
                if tool.name == snapshot_tool_name:
                    async def snapshot_with_cf_recover(
                        params: Any,
                        _tool: Tool[Any, Any] = tool,
                    ) -> ToolResult[Any]:
                        result = _tool.executor(params)
                        if asyncio.iscoroutine(result):
                            result = await result
                        return await self._maybe_recover_cloudflare(
                            current_result=result,
                            snapshot_tool=_tool,
                            wait_tool=tool_map.get(wait_tool_name),
                            get_url_tool=tool_map.get(get_url_tool_name),
                            navigate_tool=tool_map.get(navigate_tool_name),
                        )

                    wrapped_tools.append(
                        Tool(
                            name=tool.name,
                            description=tool.description + " Includes Cloudflare auto-recovery retries.",
                            parameters=tool.parameters,
                            executor=snapshot_with_cf_recover,
                        )
                    )
                    continue

                if tool.name != screenshot_tool_name:
                    wrapped_tools.append(tool)
                    continue

                async def screenshot_with_persist(params: Any, _tool: Tool[Any, Any] = tool) -> ToolResult[Any]:
                    result = _tool.executor(params)
                    if asyncio.iscoroutine(result):
                        result = await result
                    image_bytes: bytes | None = None
                    if isinstance(result.content, list):
                        for block in result.content:
                            if isinstance(block, ImageContentBlock):
                                image_bytes = block.data
                                break
                    if image_bytes is None:
                        return result

                    self._screenshot_idx += 1
                    filename = f"browser_screenshot_{self._screenshot_idx:03d}.png"
                    await self._code_provider.write_file_bytes(filename, image_bytes)
                    return ToolResult(
                        content=(
                            f"Screenshot captured and saved as {filename} in the code execution environment. "
                            "Use this exact path with view_image or code_exec."
                        ),
                        success=result.success,
                        metadata=result.metadata,
                    )

                wrapped_tools.append(
                    Tool(
                        name=tool.name,
                        description=(
                            tool.description
                            + " Screenshot bytes are also persisted to a PNG file in code execution environment."
                        ),
                        parameters=tool.parameters,
                        executor=screenshot_with_persist,
                    )
                )

            return wrapped_tools

    code_provider = LocalCodeExecToolProvider()
    if browser_profile_dir is not None:
        browser_profile_dir.mkdir(parents=True, exist_ok=True)
    browser_provider = PersistingBrowserUseToolProvider(
        code_provider=code_provider,
        headless=browser_headless,
        cdp_url=browser_cdp_url,
        extra_args=_build_browser_extra_args(
            profile_dir=browser_profile_dir,
            browser_user_agent=browser_user_agent,
            browser_timezone=browser_timezone,
        ),
        drop_search_tool=True,
        cf_retry_attempts=cf_retry_attempts,
        cf_retry_wait_seconds=cf_retry_wait_seconds,
    )
    tools = [
        # Browser tool can solve JS/challenge pages where simple HTTP fetch fails.
        browser_provider,
        WebToolProvider(timeout=float(max(web_timeout_seconds, 1)), brave_api_key=brave_api_key),
        code_provider,
        ViewImageToolProvider(exec_env=code_provider),
    ]
    client = ChatCompletionsClient(
        model=model,
        api_key=api_key,
        base_url=base_url,
        timeout=float(max(client_timeout_seconds, 1)),
    )
    agent = Agent(
        name="agentif-stirrup",
        client=client,
        max_turns=max(max_turns, 1),
        tools=tools,
        system_prompt=system_prompt,
    )

    async with agent.session(output_dir=output_dir, input_files=input_files) as session:
        finish_params, full_msg_history, run_metadata = await session.run(prompt)

    finish_payload = _to_jsonable(finish_params)
    metadata_payload = _to_jsonable(run_metadata)
    message_group_count = len(full_msg_history)
    last_group_message_count = len(full_msg_history[-1]) if full_msg_history else 0

    return {
        "finish": finish_payload,
        "run_metadata": metadata_payload,
        "message_group_count": message_group_count,
        "last_group_message_count": last_group_message_count,
        "cloudflare_challenge_detected": browser_provider.cf_challenge_detected,
        "cloudflare_challenge_unresolved": browser_provider.cf_challenge_unresolved,
    }


def _looks_like_simulation(response_payload: dict[str, Any]) -> bool:
    finish = response_payload.get("finish")
    if isinstance(finish, dict):
        reason = str(finish.get("reason", "")).lower()
        flags = ("simulat", "placeholder", "dummy", "cannot access", "could not access")
        if any(x in reason for x in flags):
            return True
    return False


def _is_valid_binary_file(path: Path) -> tuple[bool, str]:
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".docx", ".pptx"}:
        if not zipfile.is_zipfile(path):
            return False, f"{path.name} is not a valid ZIP-based Office file"
        try:
            with zipfile.ZipFile(path) as zf:
                names = set(zf.namelist())
            if "[Content_Types].xml" not in names:
                return False, f"{path.name} missing [Content_Types].xml"
            if suffix == ".xlsx" and "xl/workbook.xml" not in names:
                return False, f"{path.name} missing xl/workbook.xml"
            if suffix == ".docx" and "word/document.xml" not in names:
                return False, f"{path.name} missing word/document.xml"
            if suffix == ".pptx" and "ppt/presentation.xml" not in names:
                return False, f"{path.name} missing ppt/presentation.xml"
        except Exception as exc:
            return False, f"{path.name} invalid office archive: {exc}"
        return True, ""

    try:
        raw = path.read_bytes()
    except Exception as exc:
        return False, f"failed to read {path.name}: {exc}"

    if suffix == ".png":
        if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            return False, f"{path.name} is not a valid PNG"
    elif suffix in {".jpg", ".jpeg"}:
        if not raw.startswith(b"\xff\xd8"):
            return False, f"{path.name} is not a valid JPEG"
    elif suffix == ".pdf":
        if not raw.startswith(b"%PDF"):
            return False, f"{path.name} is not a valid PDF"
    elif suffix == ".doc":
        # Legacy Office Compound File Binary (CFBF)
        if not raw.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
            return False, f"{path.name} is not a valid binary .doc file"
    return True, ""


def _validate_outputs(
    task: Task,
    task_out: Path,
    finish_paths: list[str],
    generated_files: list[str],
    response_payload: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    required = _infer_required_outputs(task)
    generated_lower = {p.lower() for p in generated_files}

    for req in required:
        if req.lower() not in generated_lower:
            errors.append(f"missing required output: {req}")

    for rel in generated_files:
        p = task_out / rel
        if not p.exists():
            errors.append(f"reported output missing on disk: {rel}")
            continue
        ok, msg = _is_valid_binary_file(p)
        if not ok:
            errors.append(msg)

    # Detect obvious fake-completion language.
    if _looks_like_simulation(response_payload):
        errors.append("finish reason indicates simulated/placeholder completion")

    # If finish_paths were given, they should exist.
    for fp in finish_paths:
        resolved = Path(fp) if Path(fp).is_absolute() else (task_out / fp)
        if not resolved.exists():
            errors.append(f"finish path not found: {fp}")

    return errors


def _list_generated_files(task_out: Path) -> list[str]:
    ignored = {"stirrup_payload.json", "stirrup_response.json"}
    files: list[str] = []
    for p in task_out.rglob("*"):
        if not p.is_file():
            continue
        if p.name in ignored:
            continue
        files.append(str(p.relative_to(task_out)))
    files.sort()
    return files


def _extract_finish_paths(response_payload: dict[str, Any]) -> list[str]:
    finish = response_payload.get("finish")
    if not isinstance(finish, dict):
        return []
    paths = finish.get("paths")
    if not isinstance(paths, list):
        return []
    return [str(x) for x in paths]


def _require_stirrup_sdk_installed() -> None:
    try:
        import stirrup  # noqa: F401
    except Exception as exc:
        raise RuntimeError(
            "Python package 'stirrup' is not installed. Install it first: python -m pip install stirrup"
        ) from exc


def main() -> int:
    load_dotenv()

    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent
    default_attachment_search_root = (project_root / "../agentif_oneday_data").resolve()
    default_output_dir = (project_root / "runs_stirrup_generic").resolve()

    parser = argparse.ArgumentParser(description="Run selected AgentIF-OneDay tasks on Stirrup (generic)")
    parser.add_argument("--task-jsonl", required=True, help="Path to task jsonl")
    parser.add_argument("--attachment-dir", required=True, help="Attachment directory")
    parser.add_argument(
        "--attachment-search-root",
        default=str(default_attachment_search_root),
        help="Fallback recursive search root when file is not in --attachment-dir",
    )
    parser.add_argument("--output-dir", default=str(default_output_dir), help="Output root directory")
    parser.add_argument(
        "--question-ids",
        default="",
        help="Comma-separated task ids, e.g. taskif_83,taskif_88. If empty, use --limit.",
    )
    parser.add_argument("--limit", type=int, default=3, help="How many tasks to run when --question-ids is empty")
    parser.add_argument(
        "--model",
        default=os.getenv("STIRRUP_MODEL") or os.getenv("MODEL_NAME") or "",
        help="Model id used by Stirrup ChatCompletionsClient",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("STIRRUP_API_KEY") or os.getenv("MODEL_API_KEY") or "",
        help="API key used by Stirrup ChatCompletionsClient",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("STIRRUP_BASE_URL") or os.getenv("MODEL_BASE_URL") or "",
        help="Base URL used by Stirrup ChatCompletionsClient",
    )
    parser.add_argument("--max-turns", type=int, default=30, help="Stirrup max_turns")
    parser.add_argument(
        "--client-timeout-seconds",
        type=int,
        default=1800,
        help="LLM request timeout in seconds",
    )
    parser.add_argument(
        "--web-timeout-seconds",
        type=int,
        default=180,
        help="Web tool timeout in seconds",
    )
    parser.add_argument(
        "--browser-headless",
        default=os.getenv("STIRRUP_BROWSER_HEADLESS", "false"),
        help="Browser headless mode: true/false. Default false for Cloudflare stability.",
    )
    parser.add_argument(
        "--browser-cdp-url",
        default=os.getenv("STIRRUP_BROWSER_CDP_URL", ""),
        help="Optional existing Chrome CDP URL, e.g. http://127.0.0.1:9222",
    )
    parser.add_argument(
        "--browser-profile-dir",
        default=os.getenv("STIRRUP_BROWSER_PROFILE_DIR", str((project_root / ".browser_profile").resolve())),
        help="Persistent browser profile dir for cookies/session reuse.",
    )
    parser.add_argument(
        "--browser-user-agent",
        default=os.getenv(
            "STIRRUP_BROWSER_USER_AGENT",
            (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
        ),
        help="Browser User-Agent override.",
    )
    parser.add_argument(
        "--browser-timezone",
        default=os.getenv("STIRRUP_BROWSER_TIMEZONE", "America/Los_Angeles"),
        help="Browser timezone identifier.",
    )
    parser.add_argument(
        "--cf-retry-attempts",
        type=int,
        default=int(os.getenv("STIRRUP_CF_RETRY_ATTEMPTS", "2")),
        help="Auto-retry times when Cloudflare verification page is detected.",
    )
    parser.add_argument(
        "--cf-retry-wait-seconds",
        type=int,
        default=int(os.getenv("STIRRUP_CF_RETRY_WAIT_SECONDS", "8")),
        help="Wait seconds per Cloudflare auto-retry.",
    )
    parser.add_argument(
        "--brave-api-key",
        default=os.getenv("BRAVE_API_KEY", ""),
        help="Optional Brave API key for WebToolProvider",
    )
    parser.add_argument(
        "--system-prompt",
        default=DEFAULT_SYSTEM_PROMPT,
        help="Optional additional system prompt",
    )
    parser.add_argument(
        "--include-score-criteria",
        action="store_true",
        help="Append score_criteria to prompt",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not call Stirrup API; only print and save mapped payloads",
    )
    args = parser.parse_args()

    task_jsonl = Path(args.task_jsonl).resolve()
    attachment_dir = Path(args.attachment_dir).resolve()
    attachment_search_root = Path(args.attachment_search_root).resolve()
    output_root = Path(args.output_dir).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    browser_headless = _parse_bool(args.browser_headless, default=True)
    browser_cdp_url = args.browser_cdp_url.strip() or None
    browser_profile_dir = Path(args.browser_profile_dir).resolve() if args.browser_profile_dir.strip() else None
    browser_user_agent = args.browser_user_agent.strip() or None
    browser_timezone = args.browser_timezone.strip() or None

    tasks = _read_tasks(task_jsonl)
    selected = _select_tasks(tasks, _parse_question_ids(args.question_ids), args.limit)

    if not args.dry_run and not args.model.strip():
        raise SystemExit("--model is required (or set STIRRUP_MODEL / MODEL_NAME)")

    if not args.dry_run:
        _require_stirrup_sdk_installed()

    run_summary_path = output_root / "run_summary.jsonl"
    run_manifest = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "task_jsonl": str(task_jsonl),
        "attachment_dir": str(attachment_dir),
        "attachment_search_root": str(attachment_search_root),
        "output_dir": str(output_root),
        "question_ids": _parse_question_ids(args.question_ids),
        "limit": args.limit,
        "model": args.model,
        "base_url": args.base_url,
        "dry_run": args.dry_run,
        "include_score_criteria": args.include_score_criteria,
        "max_turns": args.max_turns,
        "client_timeout_seconds": args.client_timeout_seconds,
        "web_timeout_seconds": args.web_timeout_seconds,
        "browser_headless": browser_headless,
        "browser_cdp_url": browser_cdp_url,
        "browser_profile_dir": str(browser_profile_dir) if browser_profile_dir else "",
        "browser_user_agent": browser_user_agent,
        "browser_timezone": browser_timezone,
        "cf_retry_attempts": args.cf_retry_attempts,
        "cf_retry_wait_seconds": args.cf_retry_wait_seconds,
        "python": {"executable": sys.executable, "version": sys.version},
    }
    (output_root / "run_manifest.json").write_text(
        json.dumps(run_manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    attachment_index = _build_attachment_index(attachment_search_root)

    for task in selected:
        prompt = _build_prompt(task, include_score_criteria=args.include_score_criteria)
        input_files = _resolve_input_files(task, attachment_dir, attachment_index)
        task_out = output_root / task.question_id
        if task_out.exists():
            shutil.rmtree(task_out)
        task_out.mkdir(parents=True, exist_ok=True)

        mapped_payload = {
            "model": args.model,
            "base_url": args.base_url,
            "prompt": prompt,
            "input_files": [str(p) for p in input_files],
            "output_dir": str(task_out),
            "max_turns": args.max_turns,
            "client_timeout_seconds": args.client_timeout_seconds,
            "web_timeout_seconds": args.web_timeout_seconds,
        }
        (task_out / "stirrup_payload.json").write_text(
            json.dumps(mapped_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        if args.dry_run:
            row = {
                "question_id": task.question_id,
                "status": "dry_run",
                "output_dir": str(task_out),
                "input_files": mapped_payload["input_files"],
            }
        else:
            try:
                response_payload = asyncio.run(
                    _run_task_with_stirrup(
                        model=args.model,
                        api_key=args.api_key.strip() or None,
                        base_url=args.base_url.strip() or None,
                        prompt=prompt,
                        input_files=input_files,
                        output_dir=task_out,
                        max_turns=args.max_turns,
                        client_timeout_seconds=args.client_timeout_seconds,
                        web_timeout_seconds=args.web_timeout_seconds,
                        brave_api_key=args.brave_api_key.strip() or None,
                        system_prompt=args.system_prompt.strip() or None,
                        browser_headless=browser_headless,
                        browser_cdp_url=browser_cdp_url,
                        browser_profile_dir=browser_profile_dir,
                        browser_user_agent=browser_user_agent,
                        browser_timezone=browser_timezone,
                        cf_retry_attempts=args.cf_retry_attempts,
                        cf_retry_wait_seconds=args.cf_retry_wait_seconds,
                    )
                )

                (task_out / "stirrup_response.json").write_text(
                    json.dumps(response_payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

                finish_paths = _extract_finish_paths(response_payload)
                generated_files = _list_generated_files(task_out)
                validation_errors = _validate_outputs(
                    task=task,
                    task_out=task_out,
                    finish_paths=finish_paths,
                    generated_files=generated_files,
                    response_payload=response_payload,
                )
                if response_payload.get("cloudflare_challenge_unresolved"):
                    validation_errors.append("cloudflare challenge unresolved during run")
                required = _infer_required_outputs(task)
                generated_lower = {p.lower() for p in generated_files}
                required_ready = bool(required) and all(req.lower() in generated_lower for req in required)
                if validation_errors:
                    row = {
                        "question_id": task.question_id,
                        "status": "error",
                        "error": "; ".join(validation_errors),
                        "finish_paths": finish_paths,
                        "generated_files": generated_files,
                        "output_dir": str(task_out),
                    }
                else:
                    row = {
                        "question_id": task.question_id,
                        "status": "completed" if (finish_paths or required_ready) else "unfinished",
                        "finish_paths": finish_paths,
                        "generated_files": generated_files,
                        "output_dir": str(task_out),
                    }
            except Exception as exc:
                row = {
                    "question_id": task.question_id,
                    "status": "error",
                    "error": str(exc),
                    "output_dir": str(task_out),
                }

        with run_summary_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

        print(json.dumps(row, ensure_ascii=False))

    print(f"Summary saved to: {run_summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
