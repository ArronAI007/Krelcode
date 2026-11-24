"""LLM provider construction."""

from __future__ import annotations

from ._deps import *  # noqa: F401,F403


def _default_log(message: str) -> None:
    print(message)


def build_llm(
    args: argparse.Namespace,
    config: Dict[str, Any],
    log: Callable[[str], None] = _default_log,
) -> LLMProvider:
    if args.provider == "mock" or args.mock:
        # 演示脚本：第一轮 LLM 决定调用 get_time，拿到结果后给出最终回复
        return MockProvider(
            scripted=[
                Message.assistant(
                    tool_calls=[ToolCall(id="call_demo_1", name="get_time", arguments={})]
                ),
                "（mock 演示）我刚通过 get_time 工具查到了当前时间——这就是完整的 "
                "tool-call 循环：LLM 请求调用 → 审批 → 执行 → 结果回填 → LLM 总结。",
            ],
            fallback="[mock] 脚本已用完，但历史与工具循环依然正常工作。",
            chunk_size=12,  # 模拟流式分片
        )
    settings = resolve_settings(
        provider=args.provider,
        model=args.model,
        base_url=args.base_url,
        config=config,
    )
    provider = create_provider(settings)
    if args.retries > 0:
        def on_retry(attempt: int, delay: float, error: BaseException) -> None:
            log(f"[retry] LLM 调用失败（{type(error).__name__}: {error}），"
                f"第 {attempt} 次重试，{delay:g}s 后…")
        provider = RetryingProvider(
            provider, RetryPolicy(max_retries=args.retries), on_retry=on_retry)
    return provider

