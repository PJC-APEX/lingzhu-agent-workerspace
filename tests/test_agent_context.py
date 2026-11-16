from app.agents.context import estimate_tokens, select_recent_messages


def test_estimator_is_more_conservative_for_chinese_text():
    assert estimate_tokens("中文文案") > estimate_tokens("text")


def test_recent_messages_are_selected_by_budget_not_fixed_message_count(monkeypatch):
    from app.agents import context

    monkeypatch.setattr(context, "AGENT_INPUT_TOKEN_BUDGET", 32)
    messages = [
        {"role": "user", "content": "oldest-message"},
        {"role": "assistant", "content": "older-reply"},
        {"role": "user", "content": "recent-message"},
        {"role": "assistant", "content": "latest-reply"},
    ]

    selected, used, budget = select_recent_messages(messages, "", "system", "current request")

    assert [item["content"] for item in selected][-1] == "latest-reply"
    assert used <= budget
    assert len(selected) < len(messages)


def test_agent_rewrite_prompt_contains_canonical_draft_and_pending_partial_edit(monkeypatch):
    from app.services.agent_rewrite import service as agent_rewrite

    monkeypatch.setattr(agent_rewrite, "model_available", lambda model_id: False)

    original = "森林深处有一间小小的月光邮局，屋顶铺满银白色的月光。"
    prepared = agent_rewrite.prepare_agent_rewrite(
        "prompt-context-test",
        "把刚才改好的内容补回我的原文",
        None,
        "deepseek-v4-flash",
        draft_text=original,
        pending_revisions=[{"instruction": "原文前面加两个字", "reply": "夜间森林深处"}],
        output_scope="full",
    )

    system_prompt = prepared.prompt_messages[0]["content"]
    user_prompt = prepared.prompt_messages[-1]["content"]
    assert "本轮明确要求完整文案" in system_prompt
    assert original in user_prompt
    assert "夜间森林深处" in user_prompt
