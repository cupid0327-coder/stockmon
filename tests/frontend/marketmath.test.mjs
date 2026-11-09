/* 前端纯函数测试：从 webapp/static/app.js 抽取 /* == marketmath == *​/ 标记
   区间（时段判定 / 分时轴 / 对称带宽），在 node:vm 里求值后断言。
   运行：node --test "tests/frontend/*.test.mjs" */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const APP = join(here, "..", "..", "webapp", "static", "app.js");

function loadMarketMath() {
  const src = readFileSync(APP, "utf8");
  const start = src.indexOf("/* == marketmath == */");
  const end = src.indexOf("/* == /marketmath == */");
  assert.ok(start >= 0 && end > start, "app.js 缺少 marketmath 标记区间");
  const body = src.slice(start, end) +
    "\n;({ marketPulse, clockIn, slotOf, slotTime, priceBand, niceStep, " +
    "sessionAxis, axisLabels });";
  return vm.runInNewContext(body, {});
}

const M = loadMarketMath();

/* 2026-11-01 起美国东部为 EST（UTC-5），北京 = UTC+8 */
const at = iso => new Date(iso);

test("marketPulse：北京周六凌晨仍是周五美股盘中（旧实现按周末直接关掉）", () => {
  // 北京 2026-11-07 00:30 = 纽约 2026-11-06 11:30（周五盘中）
  assert.equal(M.marketPulse(at("2026-11-06T16:30:00Z")), true);
  // 北京 2026-11-07 05:30 = 纽约 2026-11-06 16:30（周五收盘后）
  assert.equal(M.marketPulse(at("2026-11-06T21:30:00Z")), false);
});

test("marketPulse：A股时段（含午休）", () => {
  assert.equal(M.marketPulse(at("2026-11-09T02:00:00Z")), true);   // 北京 10:00 周一
  assert.equal(M.marketPulse(at("2026-11-09T04:00:00Z")), false);  // 北京 12:00 午休
  assert.equal(M.marketPulse(at("2026-11-09T06:00:00Z")), true);   // 北京 14:00
  assert.equal(M.marketPulse(at("2026-11-09T08:00:00Z")), false);  // 北京 16:00 收盘
});

test("marketPulse：美股时段（北京夜间）", () => {
  // 北京 2026-11-09 22:30 = 纽约 09:30（周一开盘）
  assert.equal(M.marketPulse(at("2026-11-09T14:30:00Z")), true);
  // 北京 2026-11-10 05:00 = 纽约 2026-11-09 16:00（收盘）
  assert.equal(M.marketPulse(at("2026-11-09T21:00:00Z")), true);
  // 北京 2026-11-10 05:30 = 纽约 16:30（已收盘）
  assert.equal(M.marketPulse(at("2026-11-09T21:30:00Z")), false);
});

test("marketPulse：周末两个市场都闭市", () => {
  assert.equal(M.marketPulse(at("2026-11-08T02:00:00Z")), false);  // 北京周日 10:00
  assert.equal(M.marketPulse(at("2026-11-07T04:00:00Z")), false);  // 北京周六 12:00
});

test("clockIn：按交易所本地时钟取星期与分钟", () => {
  const cn = M.clockIn("Asia/Shanghai", at("2026-11-06T16:30:00Z"));
  assert.deepEqual([cn.day, cn.mins], [6, 30]);       // 周六 00:30
  const us = M.clockIn("America/New_York", at("2026-11-06T16:30:00Z"));
  assert.deepEqual([us.day, us.mins], [5, 690]);      // 周五 11:30
});

test("slotOf：A股午休折叠到同一刻度", () => {
  const cn = M.sessionAxis("cn_stock");
  assert.equal(cn.total, 240);
  assert.equal(M.slotOf("09:30", cn.sess), 0);
  assert.equal(M.slotOf("10:30", cn.sess), 60);
  assert.equal(M.slotOf("11:30", cn.sess), 120);
  assert.equal(M.slotOf("12:00", cn.sess), 120);      // 午休钳制
  assert.equal(M.slotOf("13:00", cn.sess), 120);
  assert.equal(M.slotOf("15:00", cn.sess), 240);
  assert.equal(M.slotOf("09:00", cn.sess), 0);        // 盘前钳制
  assert.equal(M.slotOf("16:00", cn.sess), 240);      // 盘后钳制
});

test("slotOf / sessionAxis：美股 390 格", () => {
  const us = M.sessionAxis("us_index");
  assert.equal(us.total, 390);
  assert.equal(M.slotOf("09:30", us.sess), 0);
  assert.equal(M.slotOf("16:00", us.sess), 390);
  assert.equal(M.slotOf("12:45", us.sess), 195);
});

test("slotTime / axisLabels", () => {
  assert.equal(M.slotTime(570), "09:30");
  assert.equal(M.slotTime(900), "15:00");
  assert.equal(M.slotTime(1200), "20:00");
  const labels = M.axisLabels(M.sessionAxis("cn_stock").sess);
  // vm 里造的对象原型不同，deepStrictEqual 会因跨 realm 失败：展开成当前 realm 的普通对象
  assert.deepEqual({ ...labels },
                   { left: "09:30", mid: "11:30/13:00", right: "15:00" });
});

test("priceBand：以昨收为中心对称，平盘时至少 ±0.5%", () => {
  const flat = M.priceBand([100, 100.1], 100);
  assert.equal(flat.lo, 99.5);
  assert.equal(flat.hi, 100.5);
  assert.equal(flat.lo + flat.hi, 200);              // 昨收虚线恒在正中
  const moved = M.priceBand([100, 103], 100);
  assert.ok(Math.abs(moved.lo + moved.hi - 200) < 1e-9);
  assert.ok(moved.hi - 100 > 3);                     // 上浮 12% 后向上取整
  assert.ok(moved.lo < 100 && moved.hi > 100);
});

test("niceStep：向上取整到 10^(⌊lg v⌋-1) 一档，非法输入兜底", () => {
  assert.equal(M.niceStep(0.5), 0.5);                // 平盘下限 ±0.5% 原样保留
  assert.equal(M.niceStep(10), 10);
  assert.equal(M.niceStep(11), 11);                  // 量级 10^1 → 一档 = 1
  assert.equal(M.niceStep(0), 1);                    // 非法输入兜底
  assert.equal(M.niceStep(-5), 1);
  for (const v of [0.51, 3.2, 3.36, 0.616, 123.4]) { // 必须 ≥ 输入，且不超过一档
    const s = M.niceStep(v);
    assert.ok(s >= v, `${v} -> ${s} 不该变小`);
    assert.ok(s < v + Math.pow(10, Math.floor(Math.log10(v)) - 1) + 1e-9,
              `${v} -> ${s} 超出向上取整的幅度`);
  }
});
