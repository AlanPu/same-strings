/* 出题逻辑的自动验收（规格 §10 第 13、14 项）
 *
 * 用法：node tests/player_sim.js dist/bank.json [轮数]
 * 输出 JSON 摘要；任何一项不达标则退出码 1。
 */
'use strict';

var fs = require('fs');
var path = require('path');

var logicPath = path.join(__dirname, '..', 'web', 'logic.js');
var LOGIC = require(logicPath);

var bankPath = process.argv[2] || path.join(__dirname, '..', 'dist', 'bank.json');
var rounds = parseInt(process.argv[3] || '1000', 10);

if (!fs.existsSync(bankPath)) {
  console.error('缺少 ' + bankPath + '，先跑 S5');
  process.exit(1);
}
var bank = JSON.parse(fs.readFileSync(bankPath, 'utf8'));
var performers = bank.performers;
var segments = bank.segments;
var ids = performers.map(function (p) { return p.id; });

var fail = [];
var stats = {
  rounds: rounds,
  performers: ids.length,
  segments: segments.length,
  optionCounts: {},
  correctCounts: {},
  optionSpread: {},
  adjacentRepeat: 0,
  badOptions: 0,
  badBalance: 0
};
ids.forEach(function (id) { stats.optionCounts[id] = 0; stats.correctCounts[id] = 0; });

for (var r = 0; r < rounds; r++) {
  var store = LOGIC.emptyStore();
  var qs = LOGIC.buildRound({
    performers: performers,
    segments: segments,
    store: store,
    mode: r % 2 === 0 ? 'practice' : 'exam',
    count: 10
  });
  if (qs.length !== 10) { fail.push('第 ' + r + ' 轮只生成了 ' + qs.length + ' 题'); break; }

  var perCorrect = {};
  ids.forEach(function (id) { perCorrect[id] = 0; });

  for (var i = 0; i < qs.length; i++) {
    var q = qs[i];
    var opts = q.options;
    var uniq = {};
    opts.forEach(function (o) { uniq[o] = 1; });
    if (opts.length !== Math.min(4, ids.length) || Object.keys(uniq).length !== opts.length) {
      stats.badOptions += 1;
    }
    if (opts.indexOf(q.correct) < 0) stats.badOptions += 1;
    if (!q.clipId) stats.badOptions += 1;
    perCorrect[q.correct] += 1;
    stats.correctCounts[q.correct] += 1;
    opts.forEach(function (o) { stats.optionCounts[o] += 1; });
    if (i > 0 && qs[i - 1].segmentId === q.segmentId) stats.adjacentRepeat += 1;
  }

  var vals = ids.map(function (id) { return perCorrect[id]; });
  if (Math.max.apply(null, vals) - Math.min.apply(null, vals) > 1) {
    stats.badBalance += 1;
    if (stats.badBalance <= 2) fail.push('第 ' + r + ' 轮答案分布不均：' + JSON.stringify(perCorrect));
  }
}

var ov = ids.map(function (id) { return stats.optionCounts[id]; });
stats.optionSpread = { min: Math.min.apply(null, ov), max: Math.max.apply(null, ov) };

if (stats.badOptions > 0) fail.push('选项合法性失败 ' + stats.badOptions + ' 次');
if (stats.badBalance > 0) fail.push('答案均衡失败 ' + stats.badBalance + ' 轮');
if (stats.adjacentRepeat > 0) fail.push('出现相邻同段 ' + stats.adjacentRepeat + ' 次');

/* 每位演奏家都要在题库里至少有一个片段（§10-15），由 bank 结构直接判定 */
var covered = ids.filter(function (id) {
  return segments.every(function (s) { return !!s.clips[id]; });
});
if (covered.length !== ids.length) fail.push('覆盖不足：' + ids.filter(function (i) { return covered.indexOf(i) < 0; }));

/* ---- 新功能：从头顺序 / 自选单段 / 时长预设 ------------------------- */

/* 顺序模式：一段一题、按 bank 顺序、每段都对得上自己的 clip */
var seq = LOGIC.buildSequential({ performers: performers, segments: segments });
stats.seqCount = seq.length;
stats.seqOK = seq.length === segments.length && seq.every(function (q, i) {
  var uniq = {};
  q.options.forEach(function (o) { uniq[o] = 1; });
  return q.segmentId === segments[i].id
    && q.options.length === Math.min(4, ids.length)
    && Object.keys(uniq).length === q.options.length
    && q.options.indexOf(q.correct) >= 0
    && q.clipId === segments[i].clips[q.correct];
});
if (!stats.seqOK) fail.push('顺序模式异常');

/* 单段模式：只出一题；avoid 生效 → 换人；多跑几次能覆盖到多人 */
var seg0 = segments[Math.floor(segments.length / 2)];
var one = LOGIC.buildSingle({ performers: performers, segment: seg0 });
stats.singleCount = one.length;
var avoid = one.length ? one[0].correct : null;
var two = LOGIC.buildSingle({ performers: performers, segment: seg0, avoid: avoid });
var seen = {};
for (var k = 0; k < 60; k++) {
  var q1 = LOGIC.buildSingle({ performers: performers, segment: seg0 })[0];
  seen[q1.correct] = 1;
}
stats.singleOK = one.length === 1
  && one[0].segmentId === seg0.id
  && one[0].clipId === seg0.clips[one[0].correct]
  && one[0].options.indexOf(one[0].correct) >= 0
  && two.length === 1 && two[0].correct !== avoid
  && Object.keys(seen).length >= Math.min(ids.length, 3);
if (!stats.singleOK) fail.push('单段模式异常');

/* 时长预设：升序、都 ≤ maxDuration、必须包含 maxDuration */
var maxd = bank.maxDuration;
var presets = bank.durationPresets || [];
stats.presets = presets.join('/');
stats.presetOK = typeof maxd === 'number' && maxd >= 5
  && presets.length >= 2
  && presets.every(function (d, i) { return d <= maxd && (i === 0 || d > presets[i - 1]); })
  && presets.indexOf(maxd) >= 0;
if (!stats.presetOK) fail.push('时长预设异常：maxDuration=' + maxd + ' presets=' + stats.presets);

stats.ok = fail.length === 0;
stats.failures = fail.slice(0, 5);
console.log(JSON.stringify(stats, null, 2));
process.exit(stats.ok ? 0 : 1);
