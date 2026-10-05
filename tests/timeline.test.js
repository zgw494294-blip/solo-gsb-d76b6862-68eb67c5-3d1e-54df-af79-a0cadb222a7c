"use strict";

// 提交时间线的前端渲染：状态轮询中的 timeline 字段渲染到底部列表，
// 新建实验清空，旧实验的迟到响应不得覆盖新实验的时间线。
// 响应顺序完全可控，不依赖真实时间与真实网络。
// 运行：node tests/timeline.test.js

const { loadApp, flush, statusBody, settleNewCreated } = require("./harness");

let passed = 0;
let failed = 0;

function assert(cond, message) {
  if (!cond) throw new Error(message);
}
function assertEq(actual, expected, message) {
  if (actual !== expected) {
    throw new Error(`${message}（期望 ${expected}，实际 ${actual}）`);
  }
}
async function test(name, fn) {
  try {
    await fn();
    passed++;
    console.log("  ✓ " + name);
  } catch (e) {
    failed++;
    console.error("  ✗ " + name);
    console.error("      " + e.message);
  }
}

const REC_A = { seq: 1, side: "a", before: 100, after: 112, deltas: [5, 7] };
const REC_B = { seq: 2, side: "b", before: 112, after: 115, deltas: [3] };

// 启动一个带时间线记录的 OLD 实验（?exp=OLD，初次 status / committed 放行后进入轮询）
async function bootWithTimeline(ctx, timeline) {
  await flush();
  const initStatus = ctx.calls.find((c) => c.path.includes("/OLD/status"));
  if (!initStatus) throw new Error("启动后未见 OLD status 请求");
  initStatus.settled = true;
  initStatus.resolve({
    status: 200,
    ok: true,
    json: async () => statusBody("OLD", { timeline }),
  });
  await flush();
  const initCommitted = ctx.calls.find((c) => c.path.includes("/OLD/committed"));
  if (!initCommitted) throw new Error("OLD status 放行后未见 committed 请求");
  initCommitted.settled = true;
  initCommitted.resolve({
    status: 200,
    ok: true,
    json: async () => ({ committed: 100 }),
  });
  await flush();
}

function timelineTexts(ctx) {
  return ctx.el("log-timeline")._children.map((li) => li.textContent);
}

async function main() {
  await test("状态中的时间线渲染到底部列表（栏位、前后值、增量）", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootWithTimeline(ctx, [REC_A]);
    const texts = timelineTexts(ctx);
    assertEq(texts.length, 1, "应渲染一条记录");
    assert(texts[0].includes("#1"), "记录应带编号");
    assert(texts[0].includes("甲"), "记录应带栏位");
    assert(texts[0].includes("100 → 112"), "记录应带前后值");
    assert(texts[0].includes("[5, 7]"), "记录应带增量列表");
  });

  await test("轮询带来新记录时按提交顺序追加渲染", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootWithTimeline(ctx, [REC_A]);
    ctx.tickTimers();
    await flush();
    ctx.settle("/OLD/status", 200, statusBody("OLD", { timeline: [REC_A, REC_B] }));
    await flush();
    ctx.settle("/OLD/committed", 200, { committed: 115 });
    await flush();
    const texts = timelineTexts(ctx);
    assertEq(texts.length, 2, "应渲染两条记录");
    assert(texts[1].includes("#2"), "第二条应带编号 2");
    assert(texts[1].includes("乙"), "第二条应为乙栏");
    assert(texts[1].includes("112 → 115"), "第二条应带前后值");
    assert(texts[1].includes("[3]"), "第二条应带增量");
  });

  await test("大整数字符串原样渲染，不丢精度", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootWithTimeline(ctx, [
      {
        seq: 1,
        side: "a",
        before: 100,
        after: "9007199254741093",
        deltas: ["9007199254740993"],
      },
    ]);
    const texts = timelineTexts(ctx);
    assertEq(texts.length, 1, "应渲染一条记录");
    assert(
      texts[0].includes("100 → 9007199254741093"),
      "大整数提交后的值应完整显示"
    );
    assert(texts[0].includes("[9007199254740993]"), "大整数增量应完整显示");
  });

  await test("新建实验清空时间线；旧实验迟到响应不覆盖新实验时间线", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootWithTimeline(ctx, [REC_A, REC_B]);
    assertEq(timelineTexts(ctx).length, 2, "前置：OLD 时间线已渲染");

    // 新建前触发一次旧轮询，使其 status 请求在途
    ctx.tickTimers();
    await flush();
    const staleStatus = ctx.findCall("/OLD/status", "GET");
    assert(staleStatus, "前置：旧轮询 status 在途");

    ctx.el("new-exp").click();
    await flush();
    await settleNewCreated(ctx);
    assertEq(ctx.app().expId, "NEW", "创建成功后切到 NEW");
    assertEq(timelineTexts(ctx).length, 0, "新实验时间线应为空");

    // 旧实验的迟到 status（含两条时间线记录）此刻才返回
    staleStatus.settled = true;
    staleStatus.resolve({
      status: 200,
      ok: true,
      json: async () => statusBody("OLD", { timeline: [REC_A, REC_B] }),
    });
    await flush();
    assertEq(timelineTexts(ctx).length, 0, "旧迟到响应不得覆盖新实验时间线");
  });

  await test("410 冻结后时间线区域一并冻结", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootWithTimeline(ctx, [REC_A]);
    ctx.tickTimers();
    await flush();
    ctx.settle("/OLD/status", 410, {});
    await flush();
    ctx.settle("/OLD/committed", 410, {});
    await flush();
    assertEq(ctx.app().expFrozen, true, "410 后应冻结");
    assert(
      ctx.el("timeline").classList.contains("exp-frozen"),
      "时间线区域应带冻结样式"
    );
  });

  console.log(`\n${passed} 通过，${failed} 失败`);
  process.exit(failed === 0 ? 0 : 1);
}

main();
