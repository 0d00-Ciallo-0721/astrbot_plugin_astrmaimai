from __future__ import annotations

import subprocess
import sys
import textwrap


def test_installed_host_runner_allows_three_outer_agents_to_run_nested_tools():
    script = textwrap.dedent(
        r'''
        import asyncio
        from types import SimpleNamespace

        import astrbot.api
        from astrbot.core.agent.tool import FunctionTool, ToolSet
        from astrbot.core.astr_agent_context import AstrAgentContext
        from astrbot.core.provider.entities import LLMResponse

        from astrmai.infrastructure.gateway.model_gateway import GlobalModelGateway


        class Event:
            def __init__(self, origin):
                self.unified_msg_origin = origin
                self.extras = {}

            def get_extra(self, key, default=None):
                return self.extras.get(key, default)

            def set_extra(self, key, value):
                self.extras[key] = value


        class Provider:
            def __init__(self, barrier):
                self.barrier = barrier
                self.calls = 0
                self.provider_config = {"max_context_tokens": 0}

            async def text_chat(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    await self.barrier()
                    return LLMResponse(
                        role="assistant",
                        tools_call_name=["nested_agent"],
                        tools_call_args=[{}],
                        tools_call_ids=["nested-call"],
                    )
                return LLMResponse(role="assistant", completion_text="outer-ok")


        class ProviderManager:
            def __init__(self, providers):
                self.providers = providers

            def get_provider_by_id(self, provider_id):
                return self.providers[provider_id]


        class NestedAgentTool(FunctionTool):
            async def call(self, context, **kwargs):
                event = context.context.event

                async def nested_request():
                    async with gateway._concurrency_slot(
                        True,
                        event=event,
                        stage="gateway.tool_semaphore_wait",
                        propagate_queue_timeout_status=False,
                    ):
                        await asyncio.sleep(0)
                        return "nested-ok"

                return await asyncio.create_task(nested_request())


        async def main():
            ready = 0
            all_ready = asyncio.Event()

            async def barrier():
                nonlocal ready
                ready += 1
                if ready == 3:
                    all_ready.set()
                await asyncio.wait_for(all_ready.wait(), timeout=1.0)

            providers = {f"provider-{index}": Provider(barrier) for index in range(3)}
            context = SimpleNamespace(provider_manager=ProviderManager(providers))
            config = SimpleNamespace(
                infra=SimpleNamespace(
                    max_concurrent_llm_calls=3,
                    llm_retries=0,
                    backoff_factor=1.5,
                    api_timeout=10,
                    semaphore_wait_timeout_sec=0.2,
                ),
                provider=SimpleNamespace(fallback_models=[]),
            )
            global gateway
            gateway = GlobalModelGateway(context, config)
            tool = NestedAgentTool(
                name="nested_agent",
                description="run a nested agent request",
                parameters={"type": "object", "properties": {}},
            )

            async def run_outer(index):
                event = Event(f"host:GroupMessage:{index}")
                agent_context = object.__new__(AstrAgentContext)
                object.__setattr__(agent_context, "context", context)
                object.__setattr__(agent_context, "event", event)
                return await gateway._run_tool_loop_agent_with_provider_slots(
                    event=event,
                    chat_provider_id=f"provider-{index}",
                    prompt="delegate",
                    system_prompt="use nested_agent, then answer",
                    contexts=[],
                    image_urls=None,
                    tools=ToolSet([tool]),
                    max_steps=3,
                    tool_call_timeout=2,
                    agent_context=agent_context,
                )

            results = await asyncio.wait_for(
                asyncio.gather(*(run_outer(index) for index in range(3))),
                timeout=5.0,
            )
            assert [item.completion_text for item in results] == ["outer-ok"] * 3
            assert gateway._global_semaphore._value == 3


        asyncio.run(main())
        '''
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
