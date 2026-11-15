/* 同弦 · 播放器（原生 JS，零依赖）
 *
 * 两种入口（用户可自选）：
 *   ① 顺序：「从头开始」按段落时间顺序一段一题，可以一段段往后走；
 *   ② 随机：练习 / 考试 / 错题，10 题一组，按 Leitner 盒子加权抽段；
 *   ③ 单段：自己挑一段反复练，每次换一位演奏家当正确答案。
 *
 * 每段听多久也能自己在运行中调（presets 见 bank.durationPresets）。
 * 音频在构建期已按 maxDuration 编好，运行时的时长是「听到第几秒停」，
 * 所以调短不需要重新解码，也不额外占体积。
 *
 * 防作弊要点（§7.1）：无画面、无进度条、无时长、无文件名；作答前只显示姓名。
 * 注意：用户显式设置的「听几秒」会显示出来——那是他自己的旋钮，不是考题信息。
 * iOS 要点：所有程序化播放必须在一次用户手势「解锁」之后；长音频用 Blob URL 而非 data URI。
 */
(function () {
  'use strict';

  var LOGIC = window.SSLogic;
  var BANK = JSON.parse(document.getElementById('bank').textContent);
  var STORE_KEY = 'tongxian.v1';
  var TAGS = ['甲', '乙', '丙', '丁'];

  /* 运行时可选的单段时长（秒）。bank 没给就退化成保守默认值。 */
  var MAXD = Math.round(BANK.maxDuration || 25);
  var PRESETS = (function () {
    var raw = (BANK.durationPresets || []).filter(function (d) { return d >= 5 && d <= MAXD; });
    if (!raw.length) raw = [10, 15, 20, MAXD];
    if (raw.indexOf(MAXD) < 0) raw.push(MAXD);
    return raw.sort(function (a, b) { return a - b; })
      .filter(function (d, i, a) { return a.indexOf(d) === i; });
  })();

  var MODE_LABEL = {
    practice: '随机练习', exam: '考试', review: '错题', sequence: '顺序', single: '单段'
  };

  var audioMap = null;
  var urls = new Map();
  var player = null;
  var actx = null;
  var rafId = null;
  var app = document.getElementById('app');

  var S = {
    screen: 'home',
    mode: 'practice',
    questions: [],
    qi: 0,
    answered: false,
    chosen: null,
    replays: 0,
    results: [],
    abTarget: null,
    playing: false,
    status: 'idle',
    duration: MAXD,       // 每段听到第几秒停
    fixedSeg: null,       // 单段模式锁定的段落 id
    segMode: 'single',    // 选段页的动作：single=只练这一段；seqfrom=从这段往后走
    storageOK: true,
    store: null,
    toast: ''
  };

  /* ---------------------------------------------------------- 存储 */
  function probeStorage() {
    try {
      var k = '__tx_probe__';
      window.localStorage.setItem(k, '1');
      window.localStorage.removeItem(k);
      return true;
    } catch (e) {
      return false;
    }
  }

  function clampDur(v) {
    v = Math.round(Number(v) || 0);
    if (v < 5) v = 5;
    if (v > MAXD) v = MAXD;
    return v;
  }

  function loadStore() {
    var base = LOGIC.emptyStore();
    if (!S.storageOK) return base;
    try {
      var raw = window.localStorage.getItem(STORE_KEY);
      if (!raw) return base;
      var parsed = JSON.parse(raw);
      if (!parsed || parsed.v !== 1) return base;
      parsed.seg = parsed.seg || {};
      parsed.total = parsed.total || base.total;
      if (typeof parsed.sound !== 'boolean') parsed.sound = true;
      return parsed;
    } catch (e) {
      return base;
    }
  }

  function saveStore() {
    if (!S.storageOK) return;
    try {
      window.localStorage.setItem(STORE_KEY, JSON.stringify(S.store));
    } catch (e) {
      S.storageOK = false;
      S.toast = '存储不可用，本次记录不会保存';
    }
  }

  /* ---------------------------------------------------------- 音频 */
  function decodeB64(b64) {
    var bin = atob(b64);
    var n = bin.length;
    var bytes = new Uint8Array(n);
    for (var i = 0; i < n; i++) bytes[i] = bin.charCodeAt(i);
    return bytes;
  }

  /* 内联音频表：inline 模式是 {clipId: base64}；static 模式是空对象 {}。 */
  function loadAudioMap() {
    if (audioMap !== null) return audioMap;
    try {
      var el = document.getElementById('audio');
      var raw = el && el.textContent ? el.textContent.trim() : '';
      audioMap = raw ? (JSON.parse(raw) || {}) : {};
    } catch (e) {
      audioMap = {};
    }
    return audioMap;
  }

  function urlOf(clipId) {
    if (urls.has(clipId)) {
      var hit = urls.get(clipId);
      urls.delete(clipId);
      urls.set(clipId, hit);           // LRU：命中的挪到队尾
      return hit;
    }
    var b64 = loadAudioMap()[clipId];
    /* 静态托管（音频外置）：直接给相对 URL 让 Web 服务器发。
     * 一次只取当前这一段（几百 KB），服务器支持 Range 就能拖动，也不占内存。 */
    if (typeof b64 !== 'string') return 'audio/' + encodeURIComponent(clipId) + '.m4a';
    var url = URL.createObjectURL(new Blob([decodeB64(b64)], { type: 'audio/mp4' }));
    urls.set(clipId, url);
    if (urls.size > 8) {
      var oldest = urls.keys().next().value;
      URL.revokeObjectURL(urls.get(oldest));
      urls.delete(oldest);
    }
    return url;
  }

  /* 到 S.duration 秒自动停。rAF 保证精度（timeupdate 只有 4Hz 左右）。 */
  function tick() {
    rafId = null;
    if (!S.playing || !player) return;
    if (player.currentTime >= S.duration - 0.02) {
      endPlayback('end');
      return;
    }
    rafId = window.requestAnimationFrame(tick);
  }

  function startClock() {
    if (rafId) return;
    rafId = window.requestAnimationFrame(tick);
  }

  function endPlayback(status) {
    if (rafId) { window.cancelAnimationFrame(rafId); rafId = null; }
    if (player) {
      try { player.pause(); } catch (e) { /* 已卸载 */ }
      try { player.volume = 1; } catch (e) { /* iOS 上 volume 只读 */ }
    }
    S.playing = false;
    S.status = status || 'ended';
    syncPlayUI();
  }

  function ensurePlayer() {
    if (player) return player;
    player = document.createElement('audio');
    player.setAttribute('playsinline', '');
    player.setAttribute('preload', 'auto');
    player.style.display = 'none';
    document.body.appendChild(player);
    player.addEventListener('ended', function () { endPlayback('ended'); });
    /* timeupdate 兜底：切到后台时 rAF 会停，但 timeupdate 仍会来 */
    player.addEventListener('timeupdate', function () {
      if (S.playing && player.currentTime >= S.duration - 0.02) endPlayback('end');
    });
    return player;
  }

  /* 用一段极短的静音 wav 解锁 iOS 的播放权限（必须在用户手势里） */
  function silentWav() {
    var sr = 8000, n = 80;
    var buf = new ArrayBuffer(44 + n);
    var v = new DataView(buf);
    function str(off, s) { for (var i = 0; i < s.length; i++) v.setUint8(off + i, s.charCodeAt(i)); }
    str(0, 'RIFF'); v.setUint32(4, 36 + n, true); str(8, 'WAVE'); str(12, 'fmt ');
    v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
    v.setUint32(24, sr, true); v.setUint32(28, sr, true); v.setUint16(32, 1, true);
    v.setUint16(34, 8, true); str(36, 'data'); v.setUint32(40, n, true);
    for (var i = 0; i < n; i++) v.setUint8(44 + i, 128);
    return new Blob([buf], { type: 'audio/wav' });
  }

  function unlockAudio() {
    try {
      if (!actx) {
        var Ctx = window.AudioContext || window.webkitAudioContext;
        if (Ctx) actx = new Ctx();
      }
      if (actx && actx.state === 'suspended') actx.resume();
    } catch (e) { /* 无 WebAudio 也不影响播放 */ }
    var a = ensurePlayer();
    var silent = URL.createObjectURL(silentWav());
    a.muted = true;
    a.src = silent;
    a.dataset.clip = '';
    var p = a.play();
    if (p && p.then) p.then(function () { a.pause(); a.muted = false; }).catch(function () { a.muted = false; });
  }

  function beep(kind) {
    if (!S.store.sound || !actx) return;
    try {
      var now = actx.currentTime;
      var seq = kind === 'ok' ? [[660, 0], [880, 0.09]] : [[220, 0], [180, 0.1]];
      seq.forEach(function (pair) {
        var o = actx.createOscillator();
        var g = actx.createGain();
        o.type = kind === 'ok' ? 'sine' : 'triangle';
        o.frequency.value = pair[0];
        g.gain.setValueAtTime(0.0001, now + pair[1]);
        g.gain.exponentialRampToValueAtTime(0.12, now + pair[1] + 0.012);
        g.gain.exponentialRampToValueAtTime(0.0001, now + pair[1] + 0.13);
        o.connect(g).connect(actx.destination);
        o.start(now + pair[1]);
        o.stop(now + pair[1] + 0.15);
      });
    } catch (e) { /* 音效失败不影响答题 */ }
  }

  function playClip(clipId, countReplay) {
    var a = ensurePlayer();
    var url = urlOf(clipId);
    if (a.dataset.clip !== clipId) {
      a.src = url;
      a.dataset.clip = clipId;
    }
    try { a.currentTime = 0; } catch (e) { /* 尚未 ready 时忽略 */ }
    try { a.volume = 1; } catch (e) { /* iOS 上 volume 只读 */ }
    var p = a.play();
    if (p && p.then) {
      p.then(function () {
        S.playing = true;
        S.status = 'playing';
        if (countReplay) { S.replays += 1; }
        syncPlayUI();
        startClock();
      }).catch(function () {
        S.playing = false;
        S.status = 'error';
        syncPlayUI();
      });
    }
  }

  function stopAudio() {
    endPlayback('idle');
  }

  /* ---------------------------------------------------------- 视图 */
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function nameOf(pid) {
    for (var i = 0; i < BANK.performers.length; i++) {
      if (BANK.performers[i].id === pid) return BANK.performers[i].display;
    }
    return pid;
  }

  function segById(id) {
    for (var i = 0; i < BANK.segments.length; i++) {
      if (BANK.segments[i].id === id) return BANK.segments[i];
    }
    return null;
  }

  function currentQ() { return S.questions[S.qi] || null; }

  function boxDots(segId) {
    var rec = S.store.seg[segId];
    var box = rec && rec.box ? rec.box : 1;
    var out = '';
    for (var i = 1; i <= 3; i++) out += '<span class="box' + (i <= box ? ' on' : '') + '"></span>';
    return '<span class="boxes">' + out + '</span>';
  }

  function durationChips(small) {
    var items = PRESETS.map(function (d) {
      return '<button class="chip' + (d === S.duration ? ' on' : '') + '"' +
        ' data-act="duration" data-sec="' + d + '">' + d + '<span class="u">秒</span></button>';
    }).join('');
    return '<div class="chips' + (small ? ' chips-s' : '') + '">' + items + '</div>';
  }

  function renderHome() {
    var o = LOGIC.overall(S.store);
    var mastered = LOGIC.masteredCount(S.store, BANK.segments);
    var acc = o.accuracy === null ? '—' : Math.round(o.accuracy * 100) + '%';
    var hasReview = BANK.segments.some(function (s) {
      var a = LOGIC.accuracyOf(S.store, s.id);
      return a !== null && a < 0.6;
    });

    return '' +
      '<div class="screen">' +
        '<div class="hero">' +
          '<div class="work">' + esc(BANK.work) + '</div>' +
          '<h1 class="h1">同弦</h1>' +
          '<p class="sub">听一段，判断是谁在拉。' + BANK.performers.length + ' 位演奏家 · ' +
            BANK.segments.length + ' 段 · 每段最多 ' + MAXD + ' 秒</p>' +
        '</div>' +
        (S.storageOK ? '' : '<div class="banner">本次记录不会保存（浏览器禁用了本地存储）。练习本身不受影响。</div>') +
        (S.toast ? '<div class="banner">' + esc(S.toast) + '</div>' : '') +
        '<div class="card tight">' +
          '<div class="between" style="margin-bottom:10px">' +
            '<span class="lab">每段听多久</span>' +
            '<span class="tiny">越短越难</span>' +
          '</div>' +
          durationChips(false) +
        '</div>' +
        '<div class="row" style="flex-direction:column;gap:12px">' +
          '<button class="btn primary wide" data-act="start" data-mode="sequence">从头开始' +
            '<span class="hint">' + BANK.segments.length + ' 段顺次 · 一段一题</span></button>' +
          '<div class="row">' +
            '<button class="btn" data-act="start" data-mode="practice">随机练习<span class="hint">10 题</span></button>' +
            '<button class="btn" data-act="segments">自己选段<span class="hint">单练 / 从某段往后</span></button>' +
          '</div>' +
          '<div class="row">' +
            '<button class="btn ghost" data-act="start" data-mode="exam">考试<span class="hint">10 题不即时反馈</span></button>' +
            (hasReview ? '<button class="btn ghost" data-act="start" data-mode="review">错题</button>' : '') +
          '</div>' +
        '</div>' +
        '<p class="tiny">顺序＝按乐曲时间轴一段段往后走；随机＝按你的错误率抽段；'
          + '自己选段＝单练某一段，或从某一段起往后走。</p>' +
        '<div class="statgrid">' +
          '<div class="stat"><div class="num mono">' + o.asked + '</div><div class="lbl">已答题数</div></div>' +
          '<div class="stat"><div class="num mono">' + acc + '</div><div class="lbl">正确率</div></div>' +
          '<div class="stat"><div class="num mono">' + mastered + '</div><div class="lbl">已掌握段落</div></div>' +
        '</div>' +
        '<div class="row">' +
          '<button class="btn ghost" data-act="records">答题记录</button>' +
          '<button class="btn ghost" data-act="sound">提示音：' + (S.store.sound ? '开' : '关') + '</button>' +
        '</div>' +
        '<p class="tiny center">提示音 = 答对/答错各一声，只在练习时响（考试不出声）</p>' +
        '<p class="tiny center">离线可用 · 无网络请求 · 无进度条与文件名</p>' +
      '</div>';
  }

  /* label 形如「第一乐章 · 3:30」，列表行里只显示时间（乐章已经做了分组标题） */
  function shortLabel(s) {
    var parts = String(s.label || '').split(' · ');
    return parts.length > 1 ? parts[parts.length - 1] : String(s.label || '');
  }

  function renderSegments() {
    var lastMov = null;
    var rows = BANK.segments.map(function (s, i) {
      var rec = S.store.seg[s.id];
      var acc = LOGIC.accuracyOf(S.store, s.id);
      var meta = acc === null ? '还没做过'
        : '正确率 ' + Math.round(acc * 100) + '% · 作答 ' + rec.asked + ' 次';
      /* 几十段时平铺一长串没法定位，按乐章插分组标题 */
      var head = '';
      if (s.movement !== lastMov) {
        lastMov = s.movement;
        var mname = String(s.label || '').split(' · ')[0];
        head = '<div class="seghead">' + esc(mname || ('第 ' + s.movement + ' 乐章')) + '</div>';
      }
      return head + '<button class="segrow" data-act="seg" data-seg="' + s.id + '">' +
        '<span class="segno mono">' + (i + 1) + '</span>' +
        '<span class="segname"><span>' + esc(shortLabel(s)) + '</span>' +
          '<span class="segmeta">' + meta + '</span></span>' +
        boxDots(s.id) +
        '<span class="chev">›</span></button>';
    }).join('');

    var pickSeq = S.segMode === 'seqfrom';

    return '' +
      '<div class="screen">' +
        '<div class="between">' +
          '<h2 class="h2">' + (pickSeq ? '从哪一段往后走' : '选一段来练') + '</h2>' +
          '<button class="btn ghost sm" data-act="home">返回</button>' +
        '</div>' +
        '<div class="chips" style="margin-bottom:12px">' +
          '<button class="chip' + (pickSeq ? '' : ' on') +
            '" data-act="segmode" data-val="single">单练一段</button>' +
          '<button class="chip' + (pickSeq ? ' on' : '') +
            '" data-act="segmode" data-val="seqfrom">从这段往后走</button>' +
        '</div>' +
        '<p class="sub">' + (pickSeq
          ? '点一段当起点，从它开始按时间顺序往后走。一次一节，中途随时可以退出。'
          : '全曲 ' + BANK.segments.length + ' 段，按乐章分组。点任意一段单练，'
            + '每次随机换一位演奏家，答完可以「再来一题」。') + '</p>' +
        '<div class="card tight">' +
          '<div class="between" style="margin-bottom:10px">' +
            '<span class="lab">每段听多久</span><span class="tiny">越短越难</span>' +
          '</div>' +
          durationChips(false) +
        '</div>' +
        '<div class="list">' + rows + '</div>' +
        '<div class="spacer"></div>' +
        '<button class="btn ghost" data-act="start" data-mode="sequence">从头开始（第 1 段起）</button>' +
      '</div>';
  }

  function optionCls(pid) {
    var q = currentQ();
    if (!S.answered) return 'opt';
    /* 考试模式不得即时透露对错，全部置灰 */
    if (S.mode === 'exam') return 'opt dim';
    if (pid === q.correct) return 'opt correct';
    if (pid === S.chosen) return 'opt wrong';
    return 'opt dim';
  }

  function statusText() {
    if (S.status === 'playing') return '播放中…（听到 ' + S.duration + ' 秒自动停）';
    if (S.status === 'end') return '已到设定时长 · 可重听';
    if (S.status === 'ended') return '已播完 · 可重听';
    if (S.status === 'error') return '播放失败，点按重试';
    return '点按播放这段音乐';
  }

  function renderQuiz() {
    var q = currentQ();
    if (!q) return renderHome();
    var replayNote = S.replays > 1 ? ' · 已听 ' + S.replays + ' 次' : '';
    var single = S.mode === 'single';
    var counter = single
      ? '第 ' + (S.results.length + 1) + ' 次'
      : '第 ' + (S.qi + 1) + ' / ' + S.questions.length + ' 题';

    var opts = q.options.map(function (pid, i) {
      return '<button class="' + optionCls(pid) + '" data-act="choose" data-opt="' + pid + '"' +
        (S.answered ? ' disabled' : '') + '>' +
        '<span class="tag">' + TAGS[i] + '</span><span>' + esc(nameOf(pid)) + '</span></button>';
    }).join('');

    var footer = '';
    if (!S.answered) {
      footer = '<div class="footer"><button class="btn ghost" data-act="uncertain">不确定</button></div>';
    } else {
      var isRight = S.chosen === q.correct;
      var fb = S.mode === 'exam' ? '' :
        '<div class="feedback ' + (isRight ? 'ok' : S.chosen === 'uncertain' ? '' : 'bad') + '">' +
          '<div class="line1">' +
            (isRight ? '答对了' : S.chosen === 'uncertain' ? '记为未掌握' : '答错了') +
          '</div>' +
          '<div class="line2">' + (isRight ? '' : '这一段是 ' + esc(nameOf(q.correct)) + ' 拉的') + '</div>' +
        '</div>';
      var ab = (S.mode !== 'exam' && !isRight) ?
        '<button class="btn ghost" data-act="ab">' +
          (S.abTarget === 'correct' ? '对比听：正确项' :
           S.abTarget === 'chosen' ? '对比听：你的选择' : '对比听') +
        '</button>' : '';
      if (S.mode === 'exam') {
        footer = fb + '<div class="footer">' +
          '<button class="btn primary" data-act="next">' +
            (S.qi + 1 >= S.questions.length ? '看结果' : '下一题') + '</button>' +
          '</div>';
      } else if (single) {
        footer = fb + '<div class="footer">' + ab +
          '<button class="btn primary" data-act="again">再来一题</button>' +
          '</div>' +
          '<p class="tiny center"><button class="linkbtn" data-act="seqfrom" data-seg="' +
            q.segmentId + '">从这一段往后走 →</button></p>' +
          '<p class="tiny center"><button class="linkbtn" data-act="finish">结束本次练习</button></p>';
      } else {
        footer = fb + '<div class="footer">' + ab +
          '<button class="btn primary" data-act="next">' +
            (S.qi + 1 >= S.questions.length ? '看结果' : '下一题') + '</button>' +
          '</div>';
      }
    }

    /* 练习/顺序/单段模式允许边听边改时长；考试模式藏起来，避免分心 */
    var durRow = S.mode === 'exam' ? '' :
      '<div class="durrow"><span class="tiny">听</span>' + durationChips(true) + '</div>';

    return '' +
      '<div class="screen">' +
        '<div class="bar">' +
          '<span class="pill on">' + MODE_LABEL[S.mode] + '</span>' +
          '<span class="pill">' + counter + '</span>' +
          '<div class="spacer"></div>' +
          '<button class="btn ghost sm" data-act="quit">退出</button>' +
        '</div>' +
        '<div class="stage">' +
          '<div class="playwrap">' +
            '<button class="playbtn' + (S.playing ? ' playing' : '') + '" data-act="play" aria-label="播放">' +
              '<svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
                '<path d="M19 15l14 9-14 9z" fill="currentColor" stroke="none"/>' +
              '</svg>' +
            '</button>' +
            '<div class="status" id="status">' + statusText() + replayNote + '</div>' +
            durRow +
          '</div>' +
        '</div>' +
        '<div class="options">' + opts + '</div>' +
        footer +
      '</div>';
  }

  function renderSummary() {
    var asked = 0, right = 0, uncertain = 0;
    S.results.forEach(function (r) {
      if (r.result === 'uncertain') uncertain += 1;
      else { asked += 1; if (r.result === 'correct') right += 1; }
    });
    var rate = asked ? Math.round((right / asked) * 100) : 0;

    var items = S.results.map(function (r, i) {
      var tag = r.result === 'correct' ? '<span class="tag-ok">✓</span>'
        : r.result === 'uncertain' ? '<span class="tag-unc">—</span>'
        : '<span class="tag-bad">✗</span>';
      var note = r.result === 'correct' ? '' : ' → ' + esc(nameOf(r.correct));
      return '<div class="item"><span class="idx">' + (i + 1) + '</span>' +
        '<span class="grow">' + esc(r.segmentLabel) + note + '</span>' + tag + '</div>';
    }).join('');

    var seg = S.mode === 'single' && S.fixedSeg ? segById(S.fixedSeg) : null;

    return '' +
      '<div class="screen">' +
        (seg ? '<p class="sub center">' + esc(seg.label) + '</p>' : '') +
        '<div class="score">' +
          '<div class="big mono">' + right + '<span class="of"> / ' + asked + '</span></div>' +
          '<div class="cap">正确率 ' + rate + '%' +
            (uncertain ? ' · ' + uncertain + ' 题不确定（不计入）' : '') + '</div>' +
        '</div>' +
        '<div class="list">' + items + '</div>' +
        '<div class="spacer"></div>' +
        (seg ? '<div class="row"><button class="btn ghost" data-act="seg" data-seg="' + seg.id + '">再练这一段</button>' +
          '<button class="btn primary" data-act="home">完成</button></div>'
          : '<button class="btn primary wide" data-act="home">完成</button>') +
      '</div>';
  }

  function renderRecords() {
    var rows = BANK.segments.map(function (s) {
      var rec = S.store.seg[s.id];
      var acc = LOGIC.accuracyOf(S.store, s.id);
      var pct = acc === null ? '—' : Math.round(acc * 100) + '%';
      return '<div class="item"><span class="grow">' + esc(s.label) +
        '<div class="tiny">' + (rec ? '作答 ' + rec.asked + ' 次 · 重听均 ' + (rec.avgReplays || 0).toFixed(1) + ' 次' : '还没做过') + '</div>' +
        '</span><span class="mono tiny">' + pct + '</span>' + boxDots(s.id) + '</div>';
    }).join('');

    return '' +
      '<div class="screen">' +
        '<div class="between"><h2 class="h2">答题记录</h2>' +
          '<button class="btn ghost sm" data-act="home">返回</button></div>' +
        (S.storageOK ? '' : '<div class="banner">本次记录不会保存。</div>') +
        '<div class="list">' + rows + '</div>' +
        '<div class="spacer"></div>' +
        '<button class="btn danger wide" data-act="clear">清空全部记录</button>' +
      '</div>';
  }

  function render() {
    var html = S.screen === 'quiz' ? renderQuiz()
      : S.screen === 'summary' ? renderSummary()
      : S.screen === 'segments' ? renderSegments()
      : S.screen === 'records' ? renderRecords()
      : renderHome();
    app.innerHTML = html;
  }

  /* 只更新播放相关的 DOM，避免整页重绘打断音频 */
  function syncPlayUI() {
    if (S.screen !== 'quiz') return;
    if (!currentQ()) return;
    var btn = app.querySelector('.playbtn');
    if (btn) btn.classList.toggle('playing', S.playing);
    var st = document.getElementById('status');
    if (st) {
      st.textContent = statusText() + (S.replays > 1 ? ' · 已听 ' + S.replays + ' 次' : '');
    }
  }

  /* ---------------------------------------------------------- 流程 */
  function startRound(mode, segId) {
    unlockAudio();
    var base = {
      performers: BANK.performers,
      segments: BANK.segments,
      store: S.store,
      mode: mode,
      count: 10
    };
    var qs = [];
    if (mode === 'sequence') {
      /* segId 作为起点：从这一段开始往后走，而不是每次都从第 1 段起 */
      qs = LOGIC.buildSequential({
        performers: BANK.performers,
        segments: BANK.segments,
        from: segId || null
      });
    } else if (mode === 'single') {
      var seg = segById(segId);
      if (!seg) { S.toast = '找不到这一段'; S.screen = 'home'; render(); return; }
      S.fixedSeg = seg.id;
      qs = LOGIC.buildSingle({ performers: BANK.performers, segment: seg });
    } else {
      S.fixedSeg = null;
      qs = LOGIC.buildRound(base);
    }
    if (!qs.length) {
      S.toast = '题库为空，无法开始';
      render();
      return;
    }
    S.mode = mode;
    S.questions = qs;
    S.qi = 0;
    S.answered = false;
    S.chosen = null;
    S.replays = 0;
    S.results = [];
    S.abTarget = null;
    S.playing = false;
    S.status = 'idle';
    S.toast = '';
    S.screen = 'quiz';
    render();
    playClip(qs[0].clipId, true);   // 手势内直接起播，iOS 才不拦
  }

  /* 单段模式：换一位演奏家，默认避开刚答过的那个 */
  function again() {
    var q = currentQ();
    var seg = segById(S.fixedSeg) || (q && segById(q.segmentId));
    if (!seg) { S.screen = 'home'; render(); return; }
    unlockAudio();
    var qs = LOGIC.buildSingle({
      performers: BANK.performers,
      segment: seg,
      avoid: q ? q.correct : null
    });
    S.questions = qs;
    S.qi = 0;
    S.answered = false;
    S.chosen = null;
    S.replays = 0;
    S.status = 'idle';
    S.abTarget = null;
    render();
    if (qs.length) playClip(qs[0].clipId, true);
  }

  function choose(pid) {
    if (S.answered) return;
    var q = currentQ();
    S.chosen = pid;
    S.answered = true;
    var result = pid === 'uncertain' ? 'uncertain' : pid === q.correct ? 'correct' : 'wrong';
    LOGIC.applyAnswer(S.store, q.segmentId, result, S.replays);
    saveStore();
    S.results.push({ result: result, correct: q.correct, segmentLabel: q.segmentLabel });
    stopAudio();
    /* 考试模式绝不能出声：对/错是两种音高，等于把答案提前告诉耳朵。
     * 练习/顺序/错题/单段才给提示音（可以在设置里关掉）。 */
    if (S.mode !== 'exam') {
      if (result === 'correct') beep('ok');
      else if (result === 'wrong') beep('bad');
    }
    render();
  }

  function next() {
    stopAudio();
    S.abTarget = null;
    if (S.qi + 1 >= S.questions.length) {
      S.screen = 'summary';
      S.playing = false;
      render();
      return;
    }
    S.qi += 1;
    S.answered = false;
    S.chosen = null;
    S.replays = 0;
    S.status = 'idle';
    render();
    playClip(currentQ().clipId, true);
  }

  function compare() {
    var q = currentQ();
    if (!S.answered || S.chosen === q.correct) return;
    S.abTarget = S.abTarget === 'correct' ? 'chosen' : 'correct';
    var clipId = S.abTarget === 'correct' ? q.clipId : q.optionClips[S.chosen];
    if (!clipId) return;
    playClip(clipId, false);
    render();
  }

  /* ---------------------------------------------------------- 事件 */
  app.addEventListener('click', function (ev) {
    var el = ev.target.closest('[data-act]');
    if (!el) return;
    var act = el.getAttribute('data-act');
    var q = currentQ();

    if (act === 'start') return startRound(el.getAttribute('data-mode'), el.getAttribute('data-seg'));
    if (act === 'seg') {
      return startRound(S.segMode === 'seqfrom' ? 'sequence' : 'single',
                        el.getAttribute('data-seg'));
    }
    if (act === 'segmode') { S.segMode = el.getAttribute('data-val'); return render(); }
    if (act === 'seqfrom') return startRound('sequence', el.getAttribute('data-seg'));
    if (act === 'segments') { stopAudio(); S.screen = 'segments'; S.toast = ''; return render(); }
    if (act === 'duration') {
      S.duration = clampDur(el.getAttribute('data-sec'));
      S.store.duration = S.duration;
      saveStore();
      if (S.playing && player && player.currentTime >= S.duration - 0.02) endPlayback('end');
      return render();
    }
    if (act === 'again') return again();
    if (act === 'finish') {
      stopAudio();
      if (!S.results.length) { S.screen = 'home'; } else { S.screen = 'summary'; }
      return render();
    }
    if (act === 'records') { stopAudio(); S.screen = 'records'; return render(); }
    if (act === 'home') { stopAudio(); S.screen = 'home'; S.toast = ''; S.fixedSeg = null; return render(); }
    if (act === 'sound') { S.store.sound = !S.store.sound; saveStore(); return render(); }
    if (act === 'clear') {
      if (el.dataset.armed === '1') {
        S.store = LOGIC.emptyStore();
        S.store.duration = S.duration;
        saveStore();
        S.toast = '记录已清空';
        S.screen = 'home';
        return render();
      }
      el.dataset.armed = '1';
      el.textContent = '再点一次确认清空';
      return;
    }
    if (act === 'quit') { stopAudio(); S.screen = 'home'; S.fixedSeg = null; return render(); }
    if (act === 'play') { if (q) playClip(q.clipId, true); return; }
    if (act === 'choose') return choose(el.getAttribute('data-opt'));
    if (act === 'uncertain') return choose('uncertain');
    if (act === 'next') return next();
    if (act === 'ab') return compare();
  });

  document.addEventListener('gesturestart', function (e) { e.preventDefault(); });
  document.addEventListener('contextmenu', function (e) { e.preventDefault(); });
  document.addEventListener('selectstart', function (e) { e.preventDefault(); });

  /* ---------------------------------------------------------- 启动 */
  S.storageOK = probeStorage();
  S.store = loadStore();
  S.duration = clampDur(S.store.duration || MAXD);
  S.store.duration = S.duration;
  render();
  if (!S.storageOK) S.toast = '存储不可用';

  /* 空闲时预热音频表，避免第一题开始时才解析 20MB JSON（static 模式下是空表，无成本） */
  setTimeout(function () {
    try { loadAudioMap(); } catch (e) { /* 预热失败不影响答题 */ }
  }, 400);
})();
