// 验证 vendored 物理内核与演示配置：10 个演示全部可构建、自动校验通过。
// 用法：cd study-platform && node scripts/verify_physics_assets.mjs
import { buildSim } from "../web/vendor/physics-in-motion/physics-core.js";
import { DEMOS } from "../web/vendor/physics-in-motion/demos.js";

const EXPECTED = 10;
let failed = 0;
const rows = [];
for (const demo of DEMOS) {
  if (!demo.visualization) {
    failed += 1;
    rows.push({ id: demo.id, status: "FAIL(no visualization)" });
    continue;
  }
  try {
    const sim = buildSim(demo.visualization.builder, demo.visualization.params);
    const passed = sim.checks.filter((check) => check.passed).length;
    const ok = sim.checks.length > 0 && passed === sim.checks.length;
    if (!ok) failed += 1;
    rows.push({
      id: demo.id,
      builder: demo.visualization.builder,
      frames: sim.frames.length,
      duration: Number(sim.duration.toFixed(3)),
      checks: `${passed}/${sim.checks.length}`,
      status: ok ? "ok" : "FAIL",
    });
  } catch (error) {
    failed += 1;
    rows.push({ id: demo.id, status: `ERROR ${error.message}` });
  }
}
console.table(rows);
if (DEMOS.length !== EXPECTED) {
  console.error(`演示数量应为 ${EXPECTED}，实际 ${DEMOS.length}`);
  failed += 1;
}
if (failed) {
  console.error(`验证失败：${failed} 项`);
  process.exit(1);
}
console.log(`验证通过：${DEMOS.length} 个演示，全部自动校验通过。`);
