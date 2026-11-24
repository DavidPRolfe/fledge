from dataclasses import dataclass

from fledge.core import StepContext


@dataclass(frozen=True)
class AgentStep:
    """Run agent turns until the agent yields with no guidance waiting.

    `prompt` is a str.format template over `title`, `body` and `url`.
    """

    name: str
    harness: str
    prompt: str

    def run(self, ctx: StepContext) -> dict[str, str]:
        agent = ctx.harness(self.harness)
        prompt = self.prompt.format(
            title=ctx.work.title, body=ctx.work.body, url=ctx.work.url
        )
        session_id: str | None = None
        while True:
            turn = agent.run_turn(ctx.workspace, prompt, session_id, ctx.agent_event)
            session_id = turn.session_id
            guidance = ctx.claim_guidance()
            if not guidance:
                return {"summary": turn.final_message}
            ctx.log(f"delivering {len(guidance)} guidance message(s) to the agent")
            prompt = _guidance_prompt(guidance)


def _guidance_prompt(bodies: list[str]) -> str:
    messages = "\n\n".join(
        f"Message {number}:\n{body}" for number, body in enumerate(bodies, start=1)
    )
    return (
        "A person overseeing this work sent the following guidance. "
        "Take it into account and continue.\n\n" + messages
    )
