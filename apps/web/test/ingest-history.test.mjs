import assert from "node:assert/strict";
import test from "node:test";

import { buildIngestHistoryDetail } from "../lib/ingest-history.ts";

function ingestJob({ message, details = {}, readyFiles = [] }) {
  return {
    job_type: "library.ingest",
    status: "succeeded",
    input_data: { ready_files: readyFiles.map((path) => ({ path })) },
    progress: { message, details },
  };
}

test("精确展示本次实际新增的剧集和文件", () => {
  const file = "Mystic.Nine.S01E26.2026.2160p.WEB-DL.mkv";
  const detail = buildIngestHistoryDetail(
    ingestJob({
      message: "已识别为《九门》，入库到订阅指定的「大陆剧」，复制 1 个文件",
      details: { imported_files: [file] },
      readyFiles: [file],
    }),
  );

  assert.equal(detail?.summary, "新增 S01E26 · 复制 1 个文件");
  assert.equal(detail?.context, "已入库到「大陆剧」");
  assert.deepEqual(detail?.fileGroups, [{ label: "本次新增", files: [file] }]);
});

test("全部在库时展示本次检查的集数而不是暗示整季完成", () => {
  const files = [
    "Mystic.Nine.S01E19.2026.mkv",
    "Mystic.Nine.S01E22.2026.mkv",
    "Mystic.Nine.S01E23.2026.mkv",
  ];
  const detail = buildIngestHistoryDetail(
    ingestJob({
      message: "《九门》的内容已全部在库，无需搬运",
      readyFiles: files,
    }),
  );

  assert.equal(detail?.summary, "检查 S01E19、E22–E23 · 均已在库，未复制");
  assert.deepEqual(detail?.fileGroups, [{ label: "已在库", files }]);
});

test("同时保留本次新增与已在库文件的不同语义", () => {
  const imported = "Show.S02E04.mkv";
  const existing = "Show.S02E03.mkv";
  const detail = buildIngestHistoryDetail(
    ingestJob({
      message: "复制 1 个文件",
      details: {
        imported_files: [imported],
        already_present_files: [existing],
      },
    }),
  );

  assert.equal(detail?.summary, "新增 S02E04 · 复制 1 个文件");
  assert.deepEqual(detail?.fileGroups, [
    { label: "本次新增", files: [imported] },
    { label: "已在库", files: [existing] },
  ]);
});

test("旧复制记录没有精确文件快照时不从候选列表猜测", () => {
  const files = ["Show.S01E01.mkv", "Show.S01E03.mkv"];
  const detail = buildIngestHistoryDetail(
    ingestJob({
      message: "入库完成，复制 1 个文件",
      readyFiles: files,
    }),
  );

  assert.equal(
    detail?.summary,
    "检查 S01E01、E03 · 复制 1 个文件（旧记录未保存具体文件名）",
  );
  assert.deepEqual(detail?.fileGroups, [{ label: "本次检查", files }]);
});

test("电影文件没有季集号时仍明确新增数量", () => {
  const file = "Dune.Part.Two.2024.2160p.mkv";
  const detail = buildIngestHistoryDetail(
    ingestJob({
      message: "复制 1 个文件",
      details: { imported_files: [file] },
    }),
  );

  assert.equal(detail?.summary, "新增 1 个文件");
  assert.deepEqual(detail?.fileGroups, [{ label: "本次新增", files: [file] }]);
});
