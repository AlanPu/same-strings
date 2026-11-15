/* 同弦 · 出题与调度逻辑（纯函数，无 DOM 依赖）
 *
 * 这份文件同时被浏览器内联和 tests/player_sim.js 直接 require，
 * 所以规格 §10 里「抽样 100 题 / 模拟 1000 轮」的自动验收跑的就是它。
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.SSLogic = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var BOX_WEIGHT = { 1: 3, 2: 2, 3: 1 };

  function shuffle(arr, rnd) {
    var r = rnd || Math.random;
    for (var i = arr.length - 1; i > 0; i--) {
      var j = Math.floor(r() * (i + 1));
      var t = arr[i]; arr[i] = arr[j]; arr[j] = t;
    }
    return arr;
  }

  function weightedPick(items, weightOf, rnd) {
    var r = rnd || Math.random;
    var total = 0;
    var ws = items.map(function (it) { var w = weightOf(it); total += w; return w; });
    if (total <= 0) return items[Math.floor(r() * items.length)];
    var x = r() * total;
    for (var i = 0; i < items.length; i++) {
      x -= ws[i];
      if (x <= 0) return items[i];
    }
    return items[items.length - 1];
  }

  function emptyStore() {
    return { v: 1, seg: {}, total: { asked: 0, correct: 0, uncertain: 0 }, sound: true };
  }

  function recOf(store, segId) {
    return store.seg[segId] || null;
  }

  function accuracyOf(store, segId) {
    var rec = recOf(store, segId);
    if (!rec || !rec.asked) return null;
    return rec.correct / rec.asked;
  }

  /* 练习模式按 Leitner 盒子加权；答得越多权重越低，避免反复磨同一段 */
  function segmentWeight(store, seg, mode, usedInRound) {
    if (mode === 'exam') return usedInRound ? 0.25 : 1;
    var rec = recOf(store, seg.id);
    var box = rec && rec.box ? rec.box : 1;
    var w = BOX_WEIGHT[box] || 1;
    if (rec) w /= 1 + 0.3 * rec.asked;
    if (usedInRound) w *= 0.15;
    return w;
  }

  /* 造一道题：正确项 + 出现次数最少的若干干扰项（§7.2 均衡） */
  function makeQuestion(seg, correct, ids, appear, rnd, index) {
    var want = Math.min(4, ids.length);
    var others = ids.filter(function (x) { return x !== correct; });
    var byCount = others.slice().sort(function (a, b) {
      if (appear[a] !== appear[b]) return appear[a] - appear[b];
      return rnd() - 0.5;
    });
    var distract = byCount.slice(0, want - 1);
    var options = shuffle([correct].concat(distract), rnd);
    options.forEach(function (o) { appear[o] += 1; });

    var optionClips = {};
    options.forEach(function (o) { optionClips[o] = seg.clips[o]; });

    return {
      index: index,
      segmentId: seg.id,
      segmentLabel: seg.label,
      movement: seg.movement,
      difficulty: seg.difficulty,
      correct: correct,
      options: options,
      clipId: seg.clips[correct],
      optionClips: optionClips
    };
  }

  function zeroCounts(ids) {
    var appear = {};
    ids.forEach(function (id) { appear[id] = 0; });
    return appear;
  }

  /* 一套题的完整规划：正确答案序列 + 每题的段落 + 选项
   *
   * 均衡约束（§7.2 / §10-14）：
   *   正确答案序列由「洗牌后循环取用」生成，10 题内每人被抽中次数相差必然 ≤1；
   *   干扰项从「本轮出现次数最少」的池里挑，保证进选项的次数也大致均衡。
   */
  function buildRound(opt) {
    var performers = opt.performers || [];
    var segments = opt.segments || [];
    var store = opt.store || emptyStore();
    var mode = opt.mode || 'practice';
    var count = opt.count || 10;
    var rnd = opt.rnd || Math.random;

    var ids = performers.map(function (p) { return p.id; });
    if (ids.length === 2) { /* 正常不会走到，保留 N 选 1 的最小实现 */ }

    var usable = segments.filter(function (s) { return s.clips && Object.keys(s.clips).length >= ids.length; });
    if (!usable.length) return [];

    /* --- 1. 正确答案序列 --- */
    var seq = [];
    while (seq.length < count) {
      seq = seq.concat(shuffle(ids.slice(), rnd));
    }
    seq = seq.slice(0, count);

    /* --- 2. 逐题组卷 --- */
    var appear = {};
    ids.forEach(function (id) { appear[id] = 0; });
    var usedSeg = {};
    var lastSeg = null;
    var questions = [];

    for (var i = 0; i < count; i++) {
      var correct = seq[i];
      var pool = usable.filter(function (s) { return s.clips[correct]; });

      if (mode === 'review') {
        var review = pool.filter(function (s) {
          var a = accuracyOf(store, s.id);
          return a !== null && a < 0.6;
        });
        if (review.length) pool = review;
      }

      var noRepeat = pool.filter(function (s) { return s.id !== lastSeg; });
      if (noRepeat.length) pool = noRepeat;

      var seg = weightedPick(pool, function (s) {
        return segmentWeight(store, s, mode, !!usedSeg[s.id]);
      }, rnd);

      usedSeg[seg.id] = true;
      lastSeg = seg.id;

      questions.push(makeQuestion(seg, correct, ids, appear, rnd, i + 1));
    }
    return questions;
  }

  /* 顺序模式：按段落时间顺序，一段一题（「从头开始」） */
  /* 顺序模式：一段一题，按段的时间顺序往后走。
   * opt.from 给某一段的 id 时，从它开始（含它）往后；不给则从第 1 段开始。
   * 题号重新从 1 计 —— 别让用户看到「第 21 / 53 题」这种跟当前这轮对不上的数字。
   */
  function buildSequential(opt) {
    var ids = opt.performers.map(function (p) { return p.id; });
    var rnd = opt.rnd || Math.random;
    var segs = opt.segments.slice();
    if (opt.from) {
      for (var k = 0; k < segs.length; k++) {
        if (segs[k].id === opt.from) { segs = segs.slice(k); break; }
      }
    }
    var order = [];
    while (order.length < segs.length) order = order.concat(shuffle(ids.slice(), rnd));
    var appear = zeroCounts(ids);
    return segs.map(function (seg, i) {
      return makeQuestion(seg, order[i], ids, appear, rnd, i + 1);
    });
  }

  /* 单段模式：只练指定的一段，可反复「再来一题」 */
  function buildSingle(opt) {
    var ids = opt.performers.map(function (p) { return p.id; });
    var rnd = opt.rnd || Math.random;
    var seg = opt.segment;
    var pool = ids.filter(function (id) { return id !== opt.avoid; });
    if (!pool.length) pool = ids;
    var correct = pool[Math.floor(rnd() * pool.length)];
    return [makeQuestion(seg, correct, ids, zeroCounts(ids), rnd, 1)];
  }

  /* 记一次作答，返回该段的记录（§7.3）
   * result: 'correct' | 'wrong' | 'uncertain'
   * 不确定不计入正确率，只记录为未掌握（盒子不动）。
   */
  function applyAnswer(store, segmentId, result, replays) {
    var rec = store.seg[segmentId];
    if (!rec) {
      rec = store.seg[segmentId] = {
        asked: 0, correct: 0, uncertain: 0, last5: [], replays: [], box: 1
      };
    }
    if (result === 'correct') {
      rec.asked += 1;
      rec.correct += 1;
      rec.box = Math.min(3, (rec.box || 1) + 1);
      store.total.asked += 1;
      store.total.correct += 1;
    } else if (result === 'wrong') {
      rec.asked += 1;
      rec.box = 1;
      store.total.asked += 1;
    } else {
      rec.uncertain += 1;
      store.total.uncertain += 1;
    }
    rec.last5.push(result === 'correct' ? 1 : result === 'wrong' ? 0 : -1);
    if (rec.last5.length > 5) rec.last5.shift();
    rec.replays.push(replays || 0);
    if (rec.replays.length > 10) rec.replays.shift();
    rec.avgReplays = rec.replays.reduce(function (a, b) { return a + b; }, 0) / rec.replays.length;
    return rec;
  }

  function overall(store) {
    var t = store.total;
    return {
      asked: t.asked,
      correct: t.correct,
      uncertain: t.uncertain,
      accuracy: t.asked ? t.correct / t.asked : null
    };
  }

  function masteredCount(store, segments) {
    var n = 0;
    segments.forEach(function (s) {
      var rec = recOf(store, s.id);
      if (rec && rec.box === 3) n += 1;
    });
    return n;
  }

  return {
    shuffle: shuffle,
    buildRound: buildRound,
    buildSequential: buildSequential,
    buildSingle: buildSingle,
    applyAnswer: applyAnswer,
    emptyStore: emptyStore,
    accuracyOf: accuracyOf,
    overall: overall,
    masteredCount: masteredCount,
    segmentWeight: segmentWeight
  };
});
