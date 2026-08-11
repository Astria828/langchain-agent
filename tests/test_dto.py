"""阶段 1A 的 Pydantic DTO 与前端 camelCase 契约测试。"""

import pytest
from app.schemas.dto import (
    ChatReplyOutput,
    ConfirmWorldBookDraftsPayload,
    DataResponse,
    SendMessagePayload,
    Session,
    UpdateCharacterPayload,
    UpdateSessionPayload,
    UpdateUserIdentityPayload,
    UpdateWorldBookEntryPayload,
    UpdateWorldBookPayload,
    UserIdentity,
    WorldBookEntry,
)
from pydantic import ValidationError


def test_identity_accepts_and_outputs_camel_case() -> None:
    """Python 内部 snake_case，对外只输出前端使用的 camelCase。"""

    identity = UserIdentity.model_validate(
        {
            "id": "identity-1",
            "name": "  林舟  ",
            "personaName": "  旅人  ",
            "bio": "设定",
        }
    )

    assert identity.name == "林舟"
    assert identity.model_dump(by_alias=True) == {
        "id": "identity-1",
        "name": "林舟",
        "personaName": "旅人",
        "bio": "设定",
    }


def test_unknown_dto_fields_are_rejected() -> None:
    """更新接口不能静默接受契约外字段。"""

    with pytest.raises(ValidationError):
        UserIdentity.model_validate(
            {
                "id": "identity-1",
                "name": "用户",
                "personaName": "默认身份",
                "bio": "",
                "unexpected": True,
            }
        )


def test_identity_update_requires_one_non_null_contract_field() -> None:
    """身份部分更新拒绝空请求、null 和契约外字段。"""

    for payload in ({}, {"bio": None}, {"id": "identity-1"}):
        with pytest.raises(ValidationError):
            UpdateUserIdentityPayload.model_validate(payload)

    update = UpdateUserIdentityPayload.model_validate({"name": "  林舟  "})
    assert update.model_dump(exclude_unset=True) == {"name": "林舟"}


def test_character_update_validates_fields_and_preserves_example_order() -> None:
    """角色卡更新只接受四个字段，并保持对话示例顺序。"""

    update = UpdateCharacterPayload.model_validate(
        {
            "name": "  归舟  ",
            "dialogueExamples": [
                {"user": "第一问", "assistant": "第一答"},
                {"user": "第二问", "assistant": "第二答"},
            ],
        }
    )

    assert update.name == "归舟"
    assert [example.user for example in update.dialogue_examples or []] == [
        "第一问",
        "第二问",
    ]

    for payload in ({}, {"name": "  "}, {"systemPrompt": None}, {"createdAt": "now"}):
        with pytest.raises(ValidationError):
            UpdateCharacterPayload.model_validate(payload)


def test_world_book_keywords_are_trimmed_and_stably_deduplicated() -> None:
    """关键词清理后保持首次出现顺序。"""

    entry = WorldBookEntry.model_validate(
        {
            "id": "entry-1",
            "worldBookId": "book-1",
            "name": "地点",
            "content": "内容",
            "keywords": [" 星港 ", "", "星港", "档案馆"],
            "category": "地点",
            "resident": False,
            "enabled": True,
        }
    )

    assert entry.keywords == ["星港", "档案馆"]
    assert entry.model_dump(by_alias=True)["indexStale"] is False


def test_world_book_update_payloads_reject_empty_null_and_unknown_fields() -> None:
    """世界书及条目部分更新只接受契约字段和非 null 值。"""

    book_update = UpdateWorldBookPayload.model_validate(
        {"name": "  北境设定集  ", "rawContent": "完整原文"}
    )
    assert book_update.model_dump(exclude_unset=True) == {
        "name": "北境设定集",
        "raw_content": "完整原文",
    }

    entry_update = UpdateWorldBookEntryPayload.model_validate(
        {
            "keywords": [" 北境 ", "", "北境", "雾海"],
            "resident": True,
        }
    )
    assert entry_update.keywords == ["北境", "雾海"]

    for payload in ({}, {"name": None}, {"id": "book-1"}):
        with pytest.raises(ValidationError):
            UpdateWorldBookPayload.model_validate(payload)

    for payload in ({}, {"content": "  "}, {"enabled": None}, {"indexStale": True}):
        with pytest.raises(ValidationError):
            UpdateWorldBookEntryPayload.model_validate(payload)


def test_world_book_draft_confirmation_requires_valid_non_empty_drafts() -> None:
    """草稿确认至少包含一项，且名称和正文不能为空。"""

    confirmed = ConfirmWorldBookDraftsPayload.model_validate(
        {
            "drafts": [
                {
                    "name": "  银港  ",
                    "category": "地点",
                    "content": "  海雾终年不散。  ",
                }
            ]
        }
    )
    assert confirmed.drafts[0].name == "银港"
    assert confirmed.drafts[0].content == "海雾终年不散。"

    for payload in (
        {"drafts": []},
        {"drafts": [{"name": "", "category": "地点", "content": "内容"}]},
    ):
        with pytest.raises(ValidationError):
            ConfirmWorldBookDraftsPayload.model_validate(payload)


def test_session_rejects_consolidated_round_ahead_of_round_count() -> None:
    """DTO 与数据库使用相同的轮次边界。"""

    with pytest.raises(ValidationError):
        Session.model_validate(
            {
                "id": "session-1",
                "title": "测试",
                "characterId": "character-1",
                "worldBookId": None,
                "identitySnapshotId": "snapshot-1",
                "roundCount": 9,
                "consolidatedRound": 10,
                "summary": "",
                "createdAt": "2026-08-09T00:00:00Z",
                "updatedAt": "2026-08-09T00:00:00Z",
            }
        )


def test_chat_payloads_require_non_blank_text_and_ordered_blocks() -> None:
    """会话标题、用户输入和模型回复块必须包含有效文本。"""

    assert (
        UpdateSessionPayload.model_validate({"title": "  新标题  "}).title == "新标题"
    )
    assert (
        SendMessagePayload.model_validate({"content": "  继续。  "}).content == "继续。"
    )
    output = ChatReplyOutput.model_validate(
        {
            "blocks": [
                {"type": "action", "content": "  她抬起头。  "},
                {"type": "dialogue", "content": "  你来了。  "},
            ]
        }
    )
    assert [block.type for block in output.blocks] == ["action", "dialogue"]
    assert output.blocks[0].content == "她抬起头。"

    for model, payload in (
        (UpdateSessionPayload, {"title": "  "}),
        (SendMessagePayload, {"content": ""}),
        (ChatReplyOutput, {"blocks": []}),
        (ChatReplyOutput, {"blocks": [{"type": "unknown", "content": "正文"}]}),
    ):
        with pytest.raises(ValidationError):
            model.model_validate(payload)


def test_success_response_uses_request_id_alias() -> None:
    """统一成功响应壳与 API 文档一致。"""

    response = DataResponse[str](data="ok", request_id="req_test")
    assert response.model_dump(by_alias=True) == {"data": "ok", "requestId": "req_test"}
