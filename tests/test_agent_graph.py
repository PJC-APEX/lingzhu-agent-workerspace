import json
from types import SimpleNamespace

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.graph.graph import build_agent_graph
from app.graph.nodes import route_turn
from app.services.storyboard import StoryboardResult


def stub_intents(monkeypatch, *intents):
    from app.graph import nodes

    replies = iter(intents)
    monkeypatch.setattr(nodes, "model_available", lambda model_id: True)

    def complete(*args, **kwargs):
        response = next(replies)
        if isinstance(response, dict):
            payload = response
        else:
            intent, source = response
            payload = {"intent": intent, "source": source}
        return SimpleNamespace(text=json.dumps(payload, ensure_ascii=False))

    monkeypatch.setattr(nodes, "complete_chat", complete)


def test_route_turn_maps_intent_and_preserves_workflow_operations():
    assert route_turn({"intent": "chat"}) == "rewrite"
    assert route_turn({"intent": "rewrite"}) == "rewrite"
    assert route_turn({"intent": "finalize_text"}) == "prepare_final"
    assert route_turn({"intent": "finalize_and_generate_storyboard"}) == "prepare_final"
    assert route_turn({"intent": "confirm_text"}) == "prepare_final"
    assert route_turn({"intent": "generate_storyboard"}) == "prepare_final"
    assert route_turn({"intent": "generate_storyboard", "final_text": "新文案", "rewrite_source": "message"}) == "prepare_final"
    assert route_turn({"intent": "generate_storyboard", "final_text": "已有稿", "rewrite_source": "saved_draft"}) == "storyboard"
    assert route_turn({"intent": "regenerate_storyboard"}) == "storyboard"
    assert route_turn({"intent": "revise_storyboard"}) == "revise_storyboard"


def test_storyboard_request_does_not_extract_message_into_draft(monkeypatch):
    from app.graph.nodes import intent_node

    copy = "森林深处有一间小小的月光邮局，邮局的屋顶铺满银白色的月光，每当月亮升起，草叶就会轻轻发光。" * 2
    message = f"{copy}\n把这一段生成分镜"
    stub_intents(monkeypatch, {"intent": "generate_storyboard", "source": "message", "copy_text": copy})

    update = intent_node({"message": message, "draft_text": "旧草稿"})

    assert update["intent"] == "generate_storyboard"
    assert update["final_text"] == copy
    assert update["working_text"] == copy
    assert "draft_text" not in update


def test_invalid_message_copy_does_not_fall_back_to_old_draft_for_storyboard(monkeypatch):
    from app.graph.nodes import intent_node

    stub_intents(monkeypatch, {"intent": "generate_storyboard", "source": "message", "copy_text": "误提取的内容"})

    update = intent_node({
        "message": "请帮我把新内容做成分镜",
        "draft_text": "旧定稿。",
    })

    assert update["intent"] == "chat"
    assert update["rewrite_source"] == "message"
    assert update["final_text"] == ""
    assert "draft_text" not in update


def test_intent_fallback_is_chat_and_does_not_change_existing_draft(monkeypatch):
    from app.graph.nodes import intent_node

    monkeypatch.setattr("app.graph.nodes.model_available", lambda model_id: False)
    update = intent_node({"message": "帮我重新生成分镜", "draft_text": "原来已经确认的完整文案"})

    assert update["intent"] == "chat"
    assert "draft_text" not in update


def test_new_intent_turn_clears_previous_jianying_draft_path(monkeypatch):
    from app.graph.nodes import intent_node

    monkeypatch.setattr("app.graph.nodes.model_available", lambda model_id: False)

    update = intent_node({"message": "继续聊这个文案", "draft_path": r"C:\old\draft"})

    assert update["intent"] == "chat"
    assert update["draft_path"] == ""


def test_structured_intent_call_requests_json_mode(monkeypatch):
    from app.graph.nodes import intent_node

    captured = {}
    monkeypatch.setattr("app.graph.nodes.model_available", lambda model_id: True)

    def fake_complete(messages, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(text=json.dumps({"intent": "chat", "source": "none", "copy_text": ""}))

    monkeypatch.setattr("app.graph.nodes.complete_chat", fake_complete)
    intent_node({"message": "你还记得我刚才说了什么吗？"})

    assert captured["response_format"] == {"type": "json_object"}


def test_pending_confirmation_call_requests_json_mode(monkeypatch):
    from app.graph.nodes import classify_pending_confirmation

    captured = {}
    monkeypatch.setattr("app.graph.nodes.model_available", lambda model_id: True)

    def fake_complete(messages, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(text=json.dumps({"kind": "approve"}))

    monkeypatch.setattr("app.graph.nodes.complete_chat", fake_complete)

    assert classify_pending_confirmation("确认", "confirm_final", "deepseek-v4-flash") == {"kind": "approve"}
    assert captured["response_format"] == {"type": "json_object"}


def test_storyboard_schema_failure_returns_recoverable_graph_message(monkeypatch):
    from app.graph import nodes

    monkeypatch.setattr(nodes, "get_session_factory", lambda: _NoDatabase)
    monkeypatch.setattr(
        nodes,
        "generate_storyboard",
        lambda *args, **kwargs: (_ for _ in ()).throw(nodes.StoryboardOutputError("invalid schema")),
    )
    result = nodes.storyboard_node({
        "thread_id": "storyboard-recoverable-error-test",
        "workspace_id": "workspace-test",
        "model_id": "deepseek-v4-flash",
        "message": "重新生成分镜",
        "intent": "regenerate_storyboard",
        "draft_text": "完整文案。",
        "final_text": "完整文案。",
        "storyboard": [{"sceneNumber": 1, "narration": "完整文案。", "imagePrompt": "旧画面"}],
        "storyboard_prompt_version_id": "agent_storyboard_default_v1",
    })

    assert result["stage"] == "storyboard_generation_failed"
    assert "已自动重试一次" in result["reply"]


def test_intent_node_does_not_create_draft_from_operation_only_message(monkeypatch):
    from app.graph.nodes import intent_node

    monkeypatch.setattr("app.graph.nodes.complete_chat", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("unavailable")))
    update = intent_node({"message": "帮我生成分镜"})

    assert update["intent"] == "chat"
    assert "draft_text" not in update


def test_intent_model_routes_rewrite_and_storyboard_operations(monkeypatch):
    from app.graph.nodes import intent_node

    original = "森林深处有一间小小的月光邮局。"
    stub_intents(
        monkeypatch,
        ("rewrite", "saved_draft"),
        ("revise_storyboard", "none"),
        ("regenerate_storyboard", "none"),
    )
    rewrite_update = intent_node({
        "stage": "revise_storyboard",
        "message": "文章里的‘把这一段’帮我拿掉",
        "draft_text": original,
    })
    assert rewrite_update["intent"] == "rewrite"
    assert rewrite_update["rewrite_source"] == "saved_draft"
    assert rewrite_update["rewrite_requested"] is True
    assert "draft_text" not in rewrite_update

    scene_update = intent_node({
        "stage": "revise_storyboard",
        "message": "第一分镜的画面调整成夜景",
        "draft_text": original,
    })
    assert scene_update["intent"] == "revise_storyboard"

    regenerate = intent_node({
        "stage": "revise_storyboard",
        "message": "重新生成分镜",
        "draft_text": original,
        "final_text": original,
    })
    assert regenerate["intent"] == "regenerate_storyboard"


def test_ordinary_question_is_chat_even_when_it_mentions_final_text(monkeypatch):
    from app.graph.nodes import intent_node

    stub_intents(monkeypatch, ("chat", "none"))
    update = intent_node({
        "message": "我前面一共问了你些什么",
        "draft_text": "已经定稿的文案。",
    })

    assert update["intent"] == "chat"
    assert "draft_text" not in update


def test_finalized_copy_is_cleaned_before_storyboard_confirmation(monkeypatch):
    from app.graph import nodes

    copy = "森林深处有一间小小的月光邮局，屋顶铺满银白色的月光。"
    message = f"{copy} 这篇作为最终文案，接着帮我生成分镜"
    stub_intents(monkeypatch, {
        "intent": "finalize_and_generate_storyboard",
        "source": "message",
        "copy_text": copy,
    })
    monkeypatch.setattr(
        nodes,
        "generate_storyboard",
        lambda text, model_id, **kwargs: StoryboardResult(
            scenes=[{"sceneNumber": 1, "narration": text, "imagePrompt": "月光下的邮局"}],
            model_id=model_id,
            mode="live",
        ),
    )

    graph = build_agent_graph(MemorySaver())
    config = {"configurable": {"thread_id": "explicit-storyboard-priority-test"}}
    graph.invoke({
        "thread_id": "explicit-storyboard-priority-test",
        "workspace_id": "workspace-test",
        "model_id": "deepseek-v4-flash",
        "message": message,
    }, config=config)

    paused = graph.get_state(config)
    assert paused.values["draft_text"] == copy
    assert paused.values["working_text"] == copy
    assert paused.values["final_text"] == copy
    assert paused.values["stage"] == "confirm_final"
    assert paused.tasks[0].interrupts[0].value["kind"] == "confirm_final"

    graph.invoke(Command(resume={"decision": "approve"}), config=config)
    approved = graph.get_state(config)
    assert approved.values["draft_text"] == copy
    assert approved.values["stage"] == "confirm_storyboard"


def test_storyboard_confirmation_question_without_pending_card_uses_chat_intent(monkeypatch):
    from app.graph import nodes

    original = "森林深处有一间小小的月光邮局。"
    stub_intents(monkeypatch, ("storyboard_confirmation_help", "none"))

    graph = build_agent_graph(MemorySaver())
    config = {"configurable": {"thread_id": "no-pending-storyboard-confirmation-test"}}
    graph.invoke({
        "thread_id": "no-pending-storyboard-confirmation-test",
        "draft_text": original,
        "stage": "chat",
        "message": "ok，再把所有的分镜给我进行确认",
    }, config=config)

    result = graph.get_state(config)
    assert result.values["draft_text"] == original
    assert result.values["stage"] == "chat"
    assert "没有待确认的分镜卡片" in result.values["reply"]
    assert result.next == ()


def test_cancelled_storyboard_regeneration_uses_selected_prompt_version(client, monkeypatch):
    from app.graph import nodes

    prompt = client.post("/api/v1/agent/prompts", json={
        "kind": "storyboard",
        "name": "电影镜头",
        "content": "采用克制的电影镜头语言。",
    }).json()
    monkeypatch.setattr(nodes, "get_session_factory", lambda: client.app.state.testing_session_factory)
    used = {}

    def fake_generate(text, model_id, **kwargs):
        used.update(kwargs)
        return StoryboardResult(
            scenes=[{"sceneNumber": 1, "narration": text, "imagePrompt": "定焦广角，夜色森林。"}],
            model_id=model_id,
            mode="live",
        )

    monkeypatch.setattr(nodes, "generate_storyboard", fake_generate)
    graph = build_agent_graph(MemorySaver())
    config = {"configurable": {"thread_id": "storyboard-prompt-version-test"}}
    final_text = "森林深处有一间小小的月光邮局。"
    stub_intents(monkeypatch, ("regenerate_storyboard", "none"))

    graph.invoke({
        "thread_id": "storyboard-prompt-version-test",
        "user_id": client.get("/api/v1/auth/me").json()["user"]["id"],
        "workspace_id": "workspace-test",
        "model_id": "deepseek-v4-flash",
        "stage": "revise_storyboard",
        "final_text": final_text,
        "draft_text": final_text,
        "storyboard_prompt_version_id": prompt["versionId"],
        "message": "重新生成分镜",
    }, config=config)

    result = graph.get_state(config)
    assert result.values["stage"] == "confirm_storyboard"
    assert result.values["final_text"] == final_text
    assert result.values["storyboard_used_prompt_id"] == prompt["promptId"]
    assert result.values["storyboard_used_prompt_version_id"] == prompt["versionId"]
    assert used["custom_instruction"] == "采用克制的电影镜头语言。"
    assert used["prompt_id"] == prompt["promptId"]


def test_copy_rewrite_uses_selected_prompt_version(client, monkeypatch):
    from types import SimpleNamespace

    from app.graph import nodes

    prompt = client.post("/api/v1/agent/prompts", json={
        "kind": "rewrite",
        "name": "口语精简",
        "content": "用更口语、更精简的方式表达。",
    }).json()
    stub_intents(monkeypatch, ("rewrite", "saved_draft"))
    monkeypatch.setattr(nodes, "get_session_factory", lambda: client.app.state.testing_session_factory)
    prepared_calls = []

    def fake_prepare(thread_id, message, context_text, model_id, **kwargs):
        prepared_calls.append(kwargs)
        return SimpleNamespace(
            prompt_messages=[{"role": "system", "content": "固定规则"}],
            estimated_tokens=1,
            token_budget=100,
            model_id=model_id,
            mode="live",
        )

    monkeypatch.setattr(nodes, "prepare_agent_rewrite", fake_prepare)
    monkeypatch.setattr(nodes, "stream_agent_rewrite", lambda prepared: iter(("改写后的完整文案。",)))
    graph = build_agent_graph(MemorySaver())
    config = {"configurable": {"thread_id": "rewrite-prompt-version-test"}}
    original = "森林深处有一间小小的月光邮局。"

    graph.invoke({
        "thread_id": "rewrite-prompt-version-test",
        "user_id": client.get("/api/v1/auth/me").json()["user"]["id"],
        "stage": "revise_storyboard",
        "draft_text": original,
        "final_text": original,
        "rewrite_prompt_version_id": prompt["versionId"],
        "message": "我想重新修改文案",
    }, config=config)

    result = graph.get_state(config).values
    assert prepared_calls[0]["custom_instruction"] == "用更口语、更精简的方式表达。"
    assert prepared_calls[0]["prompt_id"] == prompt["promptId"]
    assert result["rewrite_used_prompt_version_id"] == prompt["versionId"]
    assert result["draft_text"] == original
    assert result["working_text"] == "改写后的完整文案。"


def test_intent_fallback_never_treats_an_edit_instruction_as_a_draft(monkeypatch):
    from app.graph.nodes import intent_node

    original = "森林深处有一间小小的月光邮局。"
    monkeypatch.setattr("app.graph.nodes.model_available", lambda model_id: False)

    update = intent_node({
        "message": "把文案里面的‘分镜’这个词去掉",
        "draft_text": original,
    })

    assert update["intent"] == "chat"
    assert "draft_text" not in update


def test_rewrite_keeps_canonical_draft_until_user_finalizes_the_result(monkeypatch):
    from app.graph import nodes
    from contextlib import nullcontext

    original = "森林深处有一间小小的月光邮局，屋顶铺满银白色的月光。"
    full_revision = "夜间，森林深处有一间小小的月光邮局，屋顶铺满银白色的月光。"
    prepared_calls = []
    replies = iter((full_revision,))
    stub_intents(monkeypatch, ("rewrite", "saved_draft"), ("finalize_text", "working_text"))

    def fake_prepare(thread_id, message, context_text, model_id, **kwargs):
        prepared_calls.append({
            "message": message,
            "context_text": context_text,
            **kwargs,
        })
        return SimpleNamespace(
            prompt_messages=[{"role": "system", "content": ""}],
            estimated_tokens=1,
            token_budget=100,
            model_id=model_id,
            mode="live",
        )

    monkeypatch.setattr(nodes, "get_session_factory", lambda: nullcontext(None))
    # 这条用例走的是默认提示词版本，节点不会去解析提示词，因此不需要打桩解析函数。
    monkeypatch.setattr(nodes, "prepare_agent_rewrite", fake_prepare)
    monkeypatch.setattr(nodes, "stream_agent_rewrite", lambda prepared: iter((next(replies),)))

    graph = build_agent_graph(MemorySaver())
    config = {"configurable": {"thread_id": "rewrite-draft-preservation-test"}}
    graph.invoke({
        "thread_id": "rewrite-draft-preservation-test",
        "stage": "revise_storyboard",
        "draft_text": original,
        "final_text": original,
        "message": "在原文前面加两个字",
    }, config=config)

    after_partial = graph.get_state(config).values
    assert after_partial["reply"] == full_revision
    assert after_partial["draft_text"] == original
    assert after_partial["final_text"] == original
    assert after_partial["working_text"] == full_revision
    assert not after_partial.get("pending_revisions")
    assert not prepared_calls[0]["context_text"]
    assert prepared_calls[0]["draft_text"] == original
    assert prepared_calls[0]["pending_revisions"] == []

    graph.invoke({"message": "把刚才改好的版本定为最终文案"}, config=config)

    after_merge = graph.get_state(config).values
    assert len(prepared_calls) == 1
    assert after_merge["draft_text"] == full_revision
    assert after_merge["final_text"] == full_revision
    assert after_merge["working_text"] == full_revision
    assert after_merge["pending_revisions"] == []
    assert after_merge["stage"] == "confirm_final"


def test_intent_node_ignores_extracted_text_and_does_not_write_before_finalization(monkeypatch):
    from app.graph.nodes import intent_node

    copy = "森林里的月光邮局每晚都会亮起，刺猬邮差认真整理每一封信件。"
    message = "我前面一共问了你些什么"
    monkeypatch.setattr("app.graph.nodes.model_available", lambda model_id: True)
    monkeypatch.setattr(
        "app.graph.nodes.complete_chat",
        lambda *args, **kwargs: SimpleNamespace(
            text=json.dumps({"intent": "chat", "source": "none", "extracted_text": copy})
        ),
    )

    update = intent_node({"message": message, "draft_text": "旧的最终稿"})

    assert update["intent"] == "chat"
    assert "draft_text" not in update


def test_prepare_final_uses_existing_draft_not_assistant_text():
    from app.graph.nodes import prepare_final_node

    update = prepare_final_node({
        "thread_id": "draft-source-test",
        "draft_text": "用户之前确认的文案内容。",
        "latest_assistant_text": "收到，这段作为本次最终文案。",
    })

    assert update["final_text"] == "用户之前确认的文案内容。"
    assert update["pending"]["previewText"] == "用户之前确认的文案内容。"


def test_prepare_final_without_any_draft_does_not_use_assistant_reply():
    from app.graph.nodes import prepare_final_node

    update = prepare_final_node({"latest_assistant_text": "这是一条助手闲聊回复。"})

    assert "final_text" not in update
    assert update["stage"] == "chat"


def test_new_copy_requires_final_confirmation_before_storyboard_and_preserves_saved_draft_on_cancel(monkeypatch):
    from app.graph import nodes
    copy = "小林走进院子，月光落在石阶上。"
    stub_intents(monkeypatch, {"intent": "generate_storyboard", "source": "message", "copy_text": copy})

    def fake_storyboard(text, model_id, **kwargs):
        return StoryboardResult(
            scenes=[{"sceneNumber": 1, "narration": text, "imagePrompt": "清晨院子，电影构图。"}],
            model_id=model_id,
            mode="live",
        )

    monkeypatch.setattr(nodes, "generate_storyboard", fake_storyboard)
    graph = build_agent_graph(MemorySaver())
    config = {"configurable": {"thread_id": "graph-confirm-test"}}

    graph.invoke({
        "thread_id": "graph-confirm-test",
        "workspace_id": "workspace-test",
        "model_id": "qwen3.7-plus",
        "message": f"{copy}\n帮我生成分镜",
        "draft_text": "原来的定稿。",
    }, config=config)
    pause = graph.get_state(config)
    assert pause.values["stage"] == "confirm_final"
    assert pause.tasks[0].interrupts[0].value["kind"] == "confirm_final"
    assert pause.tasks[0].interrupts[0].value["previewText"] == copy
    assert pause.values["draft_text"] == "原来的定稿。"

    graph.invoke(Command(resume={"decision": "revise"}), config=config)
    finished = graph.get_state(config)
    assert finished.values["stage"] == "chat"
    assert finished.values["draft_text"] == "原来的定稿。"
    assert finished.values["working_text"] == "原来的定稿。"
    assert finished.values["final_text"] == "原来的定稿。"
    assert finished.next == ()


def test_approved_new_copy_is_saved_before_storyboard_generation(monkeypatch):
    from app.graph import nodes

    copy = "小林走进院子，月光落在石阶上。"
    stub_intents(monkeypatch, {"intent": "generate_storyboard", "source": "message", "copy_text": copy})
    monkeypatch.setattr(
        nodes,
        "generate_storyboard",
        lambda text, model_id, **kwargs: StoryboardResult(
            scenes=[{"sceneNumber": 1, "narration": text, "imagePrompt": "月光下的院子"}],
            model_id=model_id,
            mode="live",
        ),
    )
    graph = build_agent_graph(MemorySaver())
    config = {"configurable": {"thread_id": "new-copy-approval-test"}}

    graph.invoke({
        "thread_id": "new-copy-approval-test",
        "workspace_id": "workspace-test",
        "model_id": "qwen3.7-plus",
        "message": f"{copy}\n帮我生成分镜",
        "draft_text": "原来的定稿。",
    }, config=config)
    before_approval = graph.get_state(config)
    assert before_approval.values["draft_text"] == "原来的定稿。"
    assert before_approval.values["stage"] == "confirm_final"

    graph.invoke(Command(resume={"decision": "approve"}), config=config)
    after_approval = graph.get_state(config)
    assert after_approval.values["draft_text"] == copy
    assert after_approval.values["working_text"] == copy
    assert after_approval.values["storyboard"][0]["narration"] == copy
    assert after_approval.values["stage"] == "confirm_storyboard"


def test_typed_scene_revision_revises_only_selected_scene_and_repeats_confirmation(monkeypatch):
    from app.graph import nodes
    stub_intents(monkeypatch, ("generate_storyboard", "none"))

    original_scenes = [
        {"sceneNumber": 1, "narration": "第一幕。", "imagePrompt": "白天的院子。"},
        {"sceneNumber": 2, "narration": "第二幕。", "imagePrompt": "室内近景。"},
    ]
    monkeypatch.setattr(
        nodes,
        "generate_storyboard",
        lambda text, model_id, **kwargs: StoryboardResult(scenes=original_scenes, model_id=model_id, mode="live"),
    )
    monkeypatch.setattr(
        nodes,
        "revise_storyboard_scene",
        lambda scenes, scene_number, instruction, model_id: [
            {**scenes[0], "imagePrompt": "夜晚的院子，灯光明亮。"}, scenes[1]
        ],
    )
    graph = build_agent_graph(MemorySaver())
    config = {"configurable": {"thread_id": "graph-scene-revision-test"}}
    graph.invoke({
        "thread_id": "graph-scene-revision-test",
        "workspace_id": "workspace-test",
        "model_id": "qwen3.7-plus",
        "message": "请生成分镜",
        "draft_text": "第一幕。第二幕。",
    }, config=config)
    graph.invoke(Command(resume={"decision": "revise", "instruction": "第一分镜改成夜景"}), config=config)

    pause = graph.get_state(config)
    assert pause.values["stage"] == "confirm_storyboard"
    assert pause.values["storyboard"][0]["imagePrompt"] == "夜晚的院子，灯光明亮。"
    assert pause.values["storyboard"][1] == original_scenes[1]
    assert pause.tasks[0].interrupts[0].value["kind"] == "confirm_storyboard"


def test_unmatched_storyboard_revision_prompt_does_not_reopen_confirmation(monkeypatch):
    from app.graph import nodes
    stub_intents(
        monkeypatch,
        ("generate_storyboard", "none"),
        ("chat", "none"),
        ("revise_storyboard", "none"),
    )
    monkeypatch.setattr(
        nodes,
        "prepare_agent_rewrite",
        lambda *args, **kwargs: SimpleNamespace(
            prompt_messages=[{"role": "system", "content": ""}],
            estimated_tokens=1,
            token_budget=100,
            model_id="qwen3.7-plus",
            mode="live",
        ),
    )
    monkeypatch.setattr(nodes, "stream_agent_rewrite", lambda prepared: iter(("你好",)))

    original_scenes = [{"sceneNumber": 1, "narration": "第一幕。", "imagePrompt": "白天的院子。"}]
    monkeypatch.setattr(
        nodes,
        "generate_storyboard",
        lambda text, model_id, **kwargs: StoryboardResult(scenes=original_scenes, model_id=model_id, mode="live"),
    )
    monkeypatch.setattr(
        nodes,
        "revise_storyboard_scene",
        lambda scenes, scene_number, instruction, model_id: [
            {**scenes[0], "imagePrompt": "夜晚的院子。"}
        ],
    )
    graph = build_agent_graph(MemorySaver())
    config = {"configurable": {"thread_id": "graph-revision-prompt-test"}}
    graph.invoke({
        "thread_id": "graph-revision-prompt-test",
        "workspace_id": "workspace-test",
        "model_id": "qwen3.7-plus",
        "message": "请生成分镜",
        "draft_text": "第一幕。",
    }, config=config)
    graph.invoke(Command(resume={"decision": "revise"}), config=config)

    graph.invoke({"message": "你好"}, config=config)
    prompt_result = graph.get_state(config)
    assert prompt_result.values["stage"] == "chat"
    assert prompt_result.next == ()

    graph.invoke({"message": "把第一分镜画面改成夜景"}, config=config)
    revised = graph.get_state(config)
    assert revised.values["stage"] == "confirm_storyboard"
    assert revised.tasks[0].interrupts[0].value["kind"] == "confirm_storyboard"


def test_graph_resumes_from_image_job_result_and_pauses_for_asset_review(monkeypatch):
    from app.graph import nodes
    stub_intents(monkeypatch, ("finalize_and_generate_storyboard", "attachment"))

    monkeypatch.setattr(
        nodes,
        "generate_storyboard",
        lambda text, model_id, **kwargs: StoryboardResult(
            scenes=[{"sceneNumber": 1, "narration": text, "imagePrompt": "清晨院子"}],
            model_id=model_id,
            mode="live",
        ),
    )
    monkeypatch.setattr(nodes, "get_session_factory", lambda: _NoDatabase)
    monkeypatch.setattr(nodes, "record_job_dispatch", lambda db, job: None)
    monkeypatch.setattr(
        nodes,
        "prepare_agent_rewrite",
        lambda *args, **kwargs: SimpleNamespace(
            prompt_messages=[{"role": "system", "content": ""}],
            estimated_tokens=1,
            token_budget=100,
            model_id="deepseek-v4-flash",
            mode="live",
        ),
    )
    monkeypatch.setattr(nodes, "stream_agent_rewrite", lambda prepared: iter(("最终文案。",)))
    launched_jobs = []
    monkeypatch.setattr(nodes, "launch_job", lambda job_id: launched_jobs.append(job_id) or job_id)
    graph = build_agent_graph(MemorySaver())
    config = {"configurable": {"thread_id": "graph-image-test"}}

    graph.invoke({
        "thread_id": "graph-image-test",
        "workspace_id": "workspace-test",
        "model_id": "deepseek-v4-flash",
        "message": "确认文案后做分镜",
        "context_text": "最终文案。",
    }, config=config)
    graph.invoke(Command(resume={"decision": "approve"}), config=config)
    graph.invoke(Command(resume={"decision": "approve"}), config=config)

    preview_pause = graph.get_state(config)
    assert preview_pause.tasks[0].interrupts[0].value["kind"] == "image_preview_job"
    preview_job_id = preview_pause.tasks[0].interrupts[0].value["jobId"]
    assert launched_jobs == [preview_job_id]

    graph.invoke(Command(resume={
        "jobId": preview_job_id,
        "assets": [
            {"id": "preview-0", "jobId": preview_job_id, "sceneNumber": 1, "previewIndex": 0, "fileName": "preview-0.png", "objectPath": "D:/previews/preview-0.png", "url": "/media/previews/preview-0.png"},
            {"id": "preview-1", "jobId": preview_job_id, "sceneNumber": 1, "previewIndex": 1, "fileName": "preview-1.png", "objectPath": "D:/previews/preview-1.png", "url": "/media/previews/preview-1.png"},
        ],
    }), config=config)
    style_pause = graph.get_state(config)
    assert style_pause.tasks[0].interrupts[0].value["kind"] == "confirm_image_style"
    assert style_pause.values.get("image_assets", []) == []

    graph.invoke(Command(resume={"decision": "approve", "selectedPreviewIndex": 1}), config=config)
    image_pause = graph.get_state(config)
    assert image_pause.tasks[0].interrupts[0].value["kind"] == "image_job"
    job_id = image_pause.tasks[0].interrupts[0].value["jobId"]
    assert launched_jobs == [preview_job_id, job_id]

    graph.invoke(Command(resume={
        "jobId": job_id,
        "assets": [{
            "id": "asset-test",
            "jobId": job_id,
            "sceneNumber": 1,
            "fileName": "scene-1.png",
            "objectPath": "D:/assets/scene-1.png",
            "url": "/media/assets/scene-1.png",
            "status": "needs_review",
            "isDemo": False,
        }],
    }), config=config)
    review_pause = graph.get_state(config)
    assert review_pause.values["stage"] == "confirm_images"
    assert review_pause.tasks[0].interrupts[0].value["kind"] == "confirm_images"
    assert launched_jobs == [preview_job_id, job_id]


def test_graph_waits_for_srt_then_assembles_jianying_draft(client, monkeypatch, tmp_path):
    from types import SimpleNamespace

    from app.domain.models import SubtitleFile
    from app.graph import nodes

    monkeypatch.setattr(nodes, "get_session_factory", lambda: client.app.state.testing_session_factory)
    image_path = tmp_path / "scene-1.png"
    image_path.write_bytes(b"image")
    subtitle_path = tmp_path / "voice.srt"
    subtitle_path.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n小林走进院子。\n",
        encoding="utf-8",
    )
    audio_path = tmp_path / "voice.mp3"
    audio_path.write_bytes(b"audio")
    calls = []

    def fake_create_draft(alignment, assets, srt_path, audio_file_path, draft_dir):
        calls.append((alignment, assets, srt_path, audio_file_path, draft_dir))
        return SimpleNamespace(name="灵珠草稿-test", path=tmp_path / "灵珠草稿-test")

    monkeypatch.setattr(nodes, "create_jianying_draft", fake_create_draft)
    monkeypatch.setattr("app.core.config.JIANYING_DRAFT_DIR", str(tmp_path / "jianying"))
    workspace = client.post("/api/v1/workspaces", json={"name": "图流程 SRT 导出测试"}).json()
    thread = client.post("/api/v1/agent/threads", json={"workspaceId": workspace["id"]}).json()
    with client.app.state.testing_session_factory() as db:
        subtitle = SubtitleFile(
            workspace_id=workspace["id"],
            file_name="voice.srt",
            object_path=str(subtitle_path),
            audio_file_name="voice.mp3",
            audio_object_path=str(audio_path),
            cues=[{
                "start": "00:00:00,000",
                "end": "00:00:02,000",
                "text": "小林走进院子。",
            }],
        )
        db.add(subtitle)
        db.commit()
        db.refresh(subtitle)
        subtitle_id = subtitle.id

    graph = build_agent_graph(MemorySaver())
    config = {"configurable": {"thread_id": thread["id"]}}
    graph.invoke({
        "thread_id": thread["id"],
        "workspace_id": workspace["id"],
        "stage": "waiting_srt",
        "storyboard": [{"sceneNumber": 1, "narration": "小林走进院子。", "imagePrompt": "清晨院子"}],
        "image_assets": [{
            "sceneNumber": 1,
            "fileName": "scene-1.png",
            "objectPath": str(image_path),
            "status": "approved",
        }],
    }, config=config)
    waiting = graph.get_state(config)
    assert waiting.tasks[0].interrupts[0].value["kind"] == "wait_srt"

    graph.invoke(Command(resume={"subtitleFileId": subtitle_id}), config=config)
    completed = graph.get_state(config)
    assert completed.values["stage"] == "completed"
    assert completed.values["draft_path"] == str(tmp_path / "灵珠草稿-test")
    assert len(calls) == 1
    assert calls[0][1][0]["objectPath"] == str(image_path)
    assert calls[0][2] == str(subtitle_path)
    assert calls[0][3] == str(audio_path)
    assert "剪映草稿已生成" in completed.values["reply"]


class _NoDatabase:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def add(self, item):
        pass

    def commit(self):
        pass

    def refresh(self, item):
        item.id = "image-job-test"
