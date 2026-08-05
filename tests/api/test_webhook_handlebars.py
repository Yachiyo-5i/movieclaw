"""最小 Handlebars 渲染器单测：变量、5 个官方助手、空白控制、错误报中文。"""

from __future__ import annotations

import pytest

from movieclaw_api.services.webhook.handlebars import TemplateError, render

VALUES = {
    "Name": "权力的游戏",
    "NotificationType": "PlaybackStop",
    "SeasonNumber00": "02",
    "PlayedToCompletion": True,
    "Overview": "",
    "ServerUrl": "http://mc.lan:3000",
    "Quote": 'He said "hi" <b>',
}


def test_variable_and_missing():
    assert render("{{Name}} S{{SeasonNumber00}}", VALUES) == "权力的游戏 S02"
    assert render("[{{NoSuchVar}}]", VALUES) == "[]"


def test_escape_and_raw():
    assert render("{{Quote}}", VALUES) == 'He said "hi" &lt;b&gt;'
    assert render("{{{Quote}}}", VALUES) == 'He said "hi" <b>'


def test_bool_renders_dotnet_style():
    assert render("{{PlayedToCompletion}}", VALUES) == "True"


def test_if_equals_with_else_case_insensitive():
    template = "{{#if_equals NotificationType 'playbackstop'}}停了{{else}}别的{{/if_equals}}"
    assert render(template, VALUES) == "停了"
    assert render(template, {**VALUES, "NotificationType": "PlaybackStart"}) == "别的"


def test_if_exist():
    template = "{{#if_exist Overview}}有简介{{else}}无简介{{/if_exist}}"
    assert render(template, VALUES) == "无简介"
    assert render(template, {**VALUES, "Overview": "x"}) == "有简介"


def test_nested_blocks():
    template = (
        "{{#if_equals NotificationType 'PlaybackStop'}}"
        "{{#if_equals PlayedToCompletion 'True'}}看完{{else}}中断{{/if_equals}}"
        "{{/if_equals}}"
    )
    assert render(template, VALUES) == "看完"
    assert render(template, {**VALUES, "PlayedToCompletion": False}) == "中断"


def test_inline_helpers():
    assert render("{{url_encode Name}}", VALUES) == "%E6%9D%83%E5%8A%9B%E7%9A%84%E6%B8%B8%E6%88%8F"
    assert render("{{json_encode Quote}}", VALUES) == '"He said \\"hi\\" <b>"'
    assert (
        render("{{link_to ServerUrl Name}}", VALUES)
        == '<a href="http://mc.lan:3000">权力的游戏</a>'
    )


def test_whitespace_control_and_comment():
    assert render("a  {{~Name~}}  b", VALUES) == "a权力的游戏b"
    assert render("a{{! 注释 }}b", VALUES) == "ab"


def test_json_template_end_to_end():
    template = (
        '{"event": "{{NotificationType}}", "title": {{json_encode Name}},'
        ' "done": "{{PlayedToCompletion}}"}'
    )
    import json

    parsed = json.loads(render(template, VALUES))
    assert parsed == {"event": "PlaybackStop", "title": "权力的游戏", "done": "True"}


def test_tilde_scoped_to_adjacent_source_text():
    """{{~ 只修剪紧邻标签的源文本段，不越过前一个标签（对齐真 Handlebars）。"""
    assert (
        render("Hello {{#if_exist Name}}{{~Name}}{{/if_exist}} World", VALUES)
        == "Hello 权力的游戏 World"
    )


def test_tilde_in_hidden_branch_keeps_visible_output():
    """隐藏分支里的 {{~ 不得吃掉块外已产出的可见文本。"""
    assert (
        render("Hello {{#if_exist NoSuchVar}}{{~NoSuchVar}}{{/if_exist}}World", VALUES)
        == "Hello World"
    )


def test_duplicate_else_raises():
    with pytest.raises(TemplateError, match="重复的"):
        render("{{#if_exist Name}}t{{else}}f1{{else}}f2{{/if_exist}}", VALUES)


def test_mismatched_close_raises():
    with pytest.raises(TemplateError, match="不匹配"):
        render("{{#if_equals Name Name}}x{{/if_exist}}", VALUES)


def test_errors_are_chinese():
    with pytest.raises(TemplateError, match="不支持的助手"):
        render("{{substring Name 0 3}}", VALUES)
    with pytest.raises(TemplateError, match="不支持的块助手"):
        render("{{#if Name}}x{{/if}}", VALUES)
    with pytest.raises(TemplateError, match="未闭合"):
        render("{{#if_exist Name}}x", VALUES)
