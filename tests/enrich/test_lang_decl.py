"""lang_decl 微解析器单测：判例表全覆盖（docs/design/subtitle-audio-ner.md）。

输入是模型圈出的声明 span 文本（圈定由 NER 负责，不在本测试范围），
断言语言映射、载体解析、否定裁决、泛称政策四类行为。
"""

from movieclaw_enrich.lang_decl import parse_declarations


def sub_langs(*decls: str) -> list[str]:
    return parse_declarations(list(decls), [])["subtitle_languages"]


def audio_langs(*decls: str) -> list[str]:
    return parse_declarations([], list(decls))["audio_languages"]


class TestSubtitleAxis:
    def test_full_declaration_multi_language(self):
        out = parse_declarations(["内封简繁英SUP特效字幕"], [])
        assert out["subtitle_languages"] == ["zh-Hans", "zh-Hant", "en"]
        assert out["subtitle_carriers"] == ["embedded"]

    def test_latin_and_variants(self):
        assert sub_langs("CHS") == ["zh-Hans"]
        assert sub_langs("簡繁英SUB字幕") == ["zh-Hans", "zh-Hant", "en"]
        assert sub_langs("简日双语字幕") == ["zh-Hans", "ja"]
        assert sub_langs("简体中文硬字幕") == ["zh-Hans"]

    def test_generic_zh_emitted_without_guessing_script(self):
        # 泛称输出 zh，不猜简繁——选种规则按 BCP 47 前缀匹配消费
        assert sub_langs("中字") == ["zh"]
        assert sub_langs("中文字幕") == ["zh"]
        # 泛称与显式声明并存时两者都出（zh 与 zh-Hans 是不同观测）
        assert sub_langs("中字", "内封简中") == ["zh", "zh-Hans"]

    def test_carriers(self):
        assert parse_declarations(["外挂简中"], [])["subtitle_carriers"] == ["external"]
        assert parse_declarations(["内嵌简英字幕"], [])["subtitle_carriers"] == ["hardcoded"]

    def test_bare_negation_kills_all_cross_span(self):
        # 裸否定跨 span 全局生效（标题 CHS + 副标题「无字幕」的模型产出形态）
        assert sub_langs("CHS", "无字幕") == []
        assert sub_langs("简体中文字幕：无") == []
        assert sub_langs("NO SUBS", "CHS") == []

    def test_carrier_scoped_negation_keeps_other_declarations(self):
        # "无内嵌字幕，外挂简中"：只压内嵌载体，外挂简中的语言保留
        out = parse_declarations(["无内嵌字幕", "外挂简中"], [])
        assert out["subtitle_languages"] == ["zh-Hans"]
        assert out["subtitle_carriers"] == ["external"]


class TestAudioAxis:
    def test_dub_and_tags(self):
        assert audio_langs("国语配音") == ["cmn"]
        assert audio_langs("粤语音轨") == ["yue"]
        assert audio_langs("国配") == ["cmn"]

    def test_pipe_compound_segment(self):
        assert audio_langs("国|英音轨") == ["cmn", "en"]
        assert audio_langs("国粤英三语") == ["cmn", "yue", "en"]

    def test_metadata_language_list(self):
        assert audio_langs("日语", "英语", "粤语", "汉语普通话") == ["ja", "en", "yue", "cmn"]

    def test_taiwan_dub_and_multi(self):
        assert audio_langs("台配/粤语", "国日3声轨") == ["cmn", "yue", "ja"]


class TestThreeState:
    def test_empty_input_empty_output(self):
        out = parse_declarations([], [])
        assert out == {
            "subtitle_languages": [],
            "subtitle_carriers": [],
            "audio_languages": [],
        }
