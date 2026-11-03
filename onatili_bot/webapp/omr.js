/*
  Titulni telefonning o'zida o'qish (jonli skaner): burchaklarni topish, perspektivani tekislash,
  doirachalarni o'qish. Chittak (matemttest) loyihasining frontend/js/app.js faylidan olingan
  (12232–13190-qatorlar, 2d36265-commit) — backend/omr.py bilan bir xil titul formati.
*/

// Varaq o'lchamlari (punktlarda) — backend/omr.py va titul chizilishi bilan bir xil
const A4_SHEET_PAGE = {
  width: 595.28,
  height: 841.89,
  corner: 32,
  cornerSize: 14,
  edgeMarks: 10,
  edgeMarkSize: 8,
  nameBox: true,
  readScale: 2,
};
// Skanerlanayotgan titul o'lchami: ixcham titul (6-versiya) A4 ning 1/3, 1/2 qismi yoki to'liq A4 —
// /scan/layout dagi sheet_page bo'yicha; eski titullar — A4
let SHEET_PAGE = A4_SHEET_PAGE;

function sheetPageOf(layout) {
  const page = layout?.sheet_page;
  if (!page) return A4_SHEET_PAGE;
  return {
    ...A4_SHEET_PAGE,
    width: page.width,
    height: page.height,
    edgeMarks: page.edge_marks,
    nameBox: page.name_box,
    nameArea: page.name_area || NAME_BOX,
    readScale: page.height > 600 ? 1.5 : 2, // to'liq A4 (EvalBee uslubi) — kichikroq masshtab, tezroq
  };
}

// 4 nuqtadan 4 nuqtaga perspektiv moslik (varaqdagi punkt → kadr pikseli)
function perspectiveMap(from, to) {
  const h = perspectiveMatrix(from, to);
  if (!h) return null;
  return (x, y) => {
    const w = h[6] * x + h[7] * y + 1;
    return [(h[0] * x + h[1] * y + h[2]) / w, (h[3] * x + h[4] * y + h[5]) / w];
  };
}

function perspectiveMatrix(from, to) {
  const rows = [];
  from.forEach(([x, y], index) => {
    const [u, v] = to[index];
    rows.push([x, y, 1, 0, 0, 0, -u * x, -u * y, u]);
    rows.push([0, 0, 0, x, y, 1, -v * x, -v * y, v]);
  });
  for (let column = 0; column < 8; column += 1) {
    let pivot = column;
    for (let row = column + 1; row < 8; row += 1) if (Math.abs(rows[row][column]) > Math.abs(rows[pivot][column])) pivot = row;
    [rows[column], rows[pivot]] = [rows[pivot], rows[column]];
    if (Math.abs(rows[column][column]) < 1e-9) return null;
    for (let row = 0; row < 8; row += 1) {
      if (row === column) continue;
      const factor = rows[row][column] / rows[column][column];
      for (let k = column; k < 9; k += 1) rows[row][k] -= factor * rows[column][k];
    }
  }
  return rows.map((row, index) => row[8] / row[index]);
}

// Kichraytirilgan kadrda burchak kvadratlarini topish. Titulda chet belgilari bo'lsa (3-versiyadan),
// ular joyida turgan to'rtlik olinadi — varaq atrofidagi qora narsalar (stol, qo'l) adashtirmaydi.
function findSheetCorners(pixels, width, height, edgeMarks = false) {
  const gray = new Float32Array(width * height);
  for (let i = 0, p = 0; i < gray.length; i += 1, p += 4) {
    gray[i] = pixels[p] * 0.299 + pixels[p + 1] * 0.587 + pixels[p + 2] * 0.114;
  }
  // Yorug'lik tekislanadi: har nuqta atrofidagi eng yorug' joyga (qog'ozga) nisbatan. Soya tushgan
  // qog'oz ham oq bo'lib qoladi — soyada burchak va chet belgilari adashmaydi
  const light = slidingMax2D(gray, width, height, Math.max(4, Math.round(width / 36)));
  for (let i = 0; i < gray.length; i += 1) gray[i] = Math.min(255, (gray[i] * 255) / Math.max(light[i], 40));
  // Mahalliy o'rtacha (integral rasm) — soya va notekis yorug'likka chidamli
  const integral = new Float64Array((width + 1) * (height + 1));
  for (let y = 0; y < height; y += 1) {
    let rowSum = 0;
    for (let x = 0; x < width; x += 1) {
      rowSum += gray[y * width + x];
      integral[(y + 1) * (width + 1) + x + 1] = integral[y * (width + 1) + x + 1] + rowSum;
    }
  }
  const radius = Math.max(6, Math.round(width / 40));
  const dark = new Uint8Array(width * height);
  for (let y = 0; y < height; y += 1) {
    const y0 = Math.max(0, y - radius);
    const y1 = Math.min(height, y + radius + 1);
    for (let x = 0; x < width; x += 1) {
      const x0 = Math.max(0, x - radius);
      const x1 = Math.min(width, x + radius + 1);
      const sum =
        integral[y1 * (width + 1) + x1] - integral[y0 * (width + 1) + x1] - integral[y1 * (width + 1) + x0] + integral[y0 * (width + 1) + x0];
      const mean = sum / ((x1 - x0) * (y1 - y0));
      const value = gray[y * width + x];
      dark[y * width + x] = value < mean * 0.7 && value < 120 ? 1 : 0;
    }
  }

  const blobs = [];
  const stack = [];
  const minArea = Math.max(6, (width / 640) ** 2 * 12);
  const maxArea = (width * 0.045) ** 2;
  for (let start = 0; start < dark.length; start += 1) {
    if (!dark[start]) continue;
    dark[start] = 0;
    stack.push(start);
    let area = 0;
    let sumX = 0;
    let sumY = 0;
    let minX = width;
    let maxX = 0;
    let minY = height;
    let maxY = 0;
    while (stack.length) {
      const index = stack.pop();
      const x = index % width;
      const y = (index - x) / width;
      area += 1;
      sumX += x;
      sumY += y;
      if (x < minX) minX = x;
      if (x > maxX) maxX = x;
      if (y < minY) minY = y;
      if (y > maxY) maxY = y;
      if (x > 0 && dark[index - 1]) { dark[index - 1] = 0; stack.push(index - 1); }
      if (x < width - 1 && dark[index + 1]) { dark[index + 1] = 0; stack.push(index + 1); }
      if (y > 0 && dark[index - width]) { dark[index - width] = 0; stack.push(index - width); }
      if (y < height - 1 && dark[index + width]) { dark[index + width] = 0; stack.push(index + width); }
    }
    const boxWidth = maxX - minX + 1;
    const boxHeight = maxY - minY + 1;
    const fill = area / (boxWidth * boxHeight);
    const aspect = boxWidth / boxHeight;
    if (area >= minArea && area <= maxArea && fill > 0.55 && aspect > 0.6 && aspect < 1.6) {
      blobs.push({ x: sumX / area, y: sumY / area, size: Math.sqrt(area) });
    }
  }
  if (blobs.length < 4) return null;
  if (edgeMarks) return cornersFromEdgeMarks(gray, integral, width, height, blobs);

  // Eski titul (chet belgilarisiz): har burchakka eng chekkadagi dog'
  const pick = (score) => blobs.reduce((best, blob) => (score(blob) > score(best) ? blob : best));
  const corners = [
    pick((b) => -(b.x + b.y)),
    pick((b) => b.x - b.y),
    pick((b) => b.x + b.y),
    pick((b) => b.y - b.x),
  ];
  if (new Set(corners).size < 4) return null;
  const distance = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
  const top = distance(corners[0], corners[1]);
  const bottom = distance(corners[3], corners[2]);
  const left = distance(corners[0], corners[3]);
  const right = distance(corners[1], corners[2]);
  if (Math.min(top, bottom, left, right) < Math.min(width, height) * 0.3) return null;
  const ratio = (top + bottom) / (left + right);
  if (Math.abs(ratio - 0.684) / 0.684 > 0.3) return null;
  const sizes = corners.map((corner) => corner.size);
  if (Math.max(...sizes) > Math.min(...sizes) * 2.2) return null;
  return corners;
}

// Serverdagi usul bilan bir xil (backend/omr.py: _marked_edges, _corners_from_edges, _edge_marks_score)
function cornersFromEdgeMarks(gray, integral, width, height, blobs) {
  const page = SHEET_PAGE;
  const span = page.height - 2 * page.corner;
  const shape = (page.width - 2 * page.corner) / span; // titul eni / bo'yi (burchaklar orasi)
  const steps = page.edgeMarks + 1;
  if (blobs.length > 300) return null;
  const at = (x, y) =>
    gray[Math.min(height - 1, Math.max(0, Math.round(y))) * width + Math.min(width - 1, Math.max(0, Math.round(x)))];
  // Kvadrat oyna ichidagi o'rtacha yorqinlik — integral rasmdan bir zumda
  const mean = (x, y, r) => {
    const x0 = Math.min(width, Math.max(0, Math.round(x) - r));
    const x1 = Math.min(width, Math.max(0, Math.round(x) + r + 1));
    const y0 = Math.min(height, Math.max(0, Math.round(y) - r));
    const y1 = Math.min(height, Math.max(0, Math.round(y) + r + 1));
    const count = (x1 - x0) * (y1 - y0);
    if (!count) return 255;
    const row = width + 1;
    return (integral[y1 * row + x1] - integral[y0 * row + x1] - integral[y1 * row + x0] + integral[y0 * row + x0]) / count;
  };
  // Oyna ichidagi eng qora nuqta — kichik belgi biroz siljigan bo'lsa ham topilsin
  const darkest = (x, y, r) => {
    const cx = Math.round(x);
    const cy = Math.round(y);
    let low = 255;
    for (let dy = -r; dy <= r; dy += 1) for (let dx = -r; dx <= r; dx += 1) low = Math.min(low, at(cx + dx, cy + dy));
    return low;
  };
  const median = (values) => [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)];
  const distance = (p, q) => Math.hypot(p.x - q.x, p.y - q.y);

  // 1) Chet bo'lishi mumkin bo'lgan juftlar: orasida 10 ta belgi teng oraliqda qatorlashgan
  let edges = [];
  for (let i = 0; i < blobs.length; i += 1) {
    for (let j = i + 1; j < blobs.length; j += 1) {
      const [a, b] = blobs[i].y <= blobs[j].y ? [blobs[i], blobs[j]] : [blobs[j], blobs[i]];
      if (Math.abs(a.size - b.size) / Math.max(a.size, b.size) > 0.45) continue;
      const length = distance(a, b);
      const expected = (((a.size + b.size) / 2) * span) / page.cornerSize;
      if (length < expected * 0.45 || length > expected * 2.2) continue;
      const near = Math.sqrt(a.size / b.size); // perspektiva: yaqinroq uchi kattaroq ko'rinadi
      const point = (u) => {
        const t = (u * near) / (1 - u + u * near);
        return [a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t];
      };
      const r = Math.max(1, Math.round((a.size + b.size) * 0.14));
      const ends = Math.min(mean(a.x, a.y, r), mean(b.x, b.y, r));
      // Qog'oz yorqinligi — belgilar orasining o'rtalaridan
      const middles = [];
      for (let k = 0; k < steps; k += 1) middles.push(mean(...point((k + 0.5) / steps), r));
      const paper = median(middles);
      if (paper - ends < 30) continue;
      const threshold = (paper + ends) / 2;
      const total = page.edgeMarks + steps * 3;
      const allowed = total * 0.2;
      let misses = middles.filter((value) => value <= threshold).length;
      // Juft yaroqsizligi aniq bo'lishi bilan to'xtatiladi (kadr tez tekshirilsin)
      for (let k = 0; k < steps && misses <= allowed; k += 1) {
        for (const part of [0.35, 0.65]) if (mean(...point((k + part) / steps), r) <= threshold) misses += 1;
        if (k === 0) continue;
        let found = false;
        for (const shift of [0, -0.075, 0.075, -0.15, 0.15]) {
          // Belgi kengroq oynada qidiriladi: bukilgan qog'ozda u to'g'ri chiziqdan chetga suriladi
          if (darkest(...point((k + shift) / steps), Math.max(r, Math.round((a.size + b.size) * 0.3))) < threshold) {
            found = true;
            break;
          }
        }
        if (!found) misses += 1;
      }
      const score = 1 - misses / total;
      if (score >= 0.8) edges.push({ score, a, b, length });
    }
  }
  // Haqiqiy chet: uchlarida katta burchak kvadratlari (14 pt — doirachadan ~1,5 baravar katta), so'ng eng
  // uzuni. Bo'yalgan doirachalar ustuni ham "chet"ga o'xshaydi — ular ro'yxatdan haqiqiy chetni siqib chiqarmasin
  const endSize = (edge) => Math.round(((edge.a.size + edge.b.size) / 2) * 2);
  edges.sort(
    (p, q) => Math.round(q.score * 10) - Math.round(p.score * 10) || endSize(q) - endSize(p) || q.length - p.length
  );
  edges = edges.slice(0, 200); // soxta chetlar ko'p bo'lsa ham haqiqiysi ro'yxatda qolsin (qiya tutilganda)

  // 2) Ikki chetdan to'rtburchak: A4 shakli, qarama-qarshi tomonlari deyarli teng, burchaklari 90° ga yaqin
  const shapes = [];
  for (let i = 0; i < edges.length; i += 1) {
    for (let j = i + 1; j < edges.length; j += 1) {
      let [left, right] = [edges[i], edges[j]];
      if (new Set([left.a, left.b, right.a, right.b]).size < 4) continue;
      if (left.a.x + left.b.x > right.a.x + right.b.x) [left, right] = [right, left];
      const corners = [left.a, right.a, right.b, left.b];
      const top = distance(corners[0], corners[1]);
      const bottom = distance(corners[3], corners[2]);
      const leftSide = distance(corners[0], corners[3]);
      const rightSide = distance(corners[1], corners[2]);
      if (Math.abs((top + bottom) / (leftSide + rightSide) - shape) / shape > 0.3) continue;
      const sizes = corners.map((corner) => corner.size);
      if ((Math.max(...sizes) - Math.min(...sizes)) / Math.max(...sizes) > 0.45) continue;
      if (Math.max(top, bottom) > 1.5 * Math.min(top, bottom)) continue;
      if (Math.max(leftSide, rightSide) > 1.5 * Math.min(leftSide, rightSide)) continue;
      let area = 0;
      let turns = 0;
      let rectangular = true;
      for (let k = 0; k < 4; k += 1) {
        const p = corners[(k + 3) % 4];
        const q = corners[k];
        const s = corners[(k + 1) % 4];
        const ax = p.x - q.x;
        const ay = p.y - q.y;
        const bx = s.x - q.x;
        const by = s.y - q.y;
        if (Math.abs(ax * bx + ay * by) / (Math.hypot(ax, ay) * Math.hypot(bx, by)) > Math.cos((55 * Math.PI) / 180)) {
          rectangular = false;
        }
        turns += Math.sign(ax * by - ay * bx);
        area += q.x * s.y - s.x * q.y;
      }
      area = Math.abs(area) / 2;
      if (!rectangular || Math.abs(turns) !== 4) continue;
      if (area < 0.6 * ((top + bottom) / 2) * ((leftSide + rightSide) / 2)) continue;
      shapes.push({ area, corners });
    }
  }
  shapes.sort((p, q) => q.area - p.area);

  // 3) Aniq tekshiruv: har bir chet belgisi o'z joyida qora, oralari va tepa cheti oq qog'oz
  const sides = [page.corner, page.width - page.corner];
  const marks = [];
  const blanks = [];
  for (let k = 1; k <= page.edgeMarks; k += 1) for (const x of sides) marks.push([x, page.corner + (k * span) / steps]);
  for (let k = 0; k < steps; k += 1) {
    for (const part of [0.3, 0.5, 0.7]) for (const x of sides) blanks.push([x, page.corner + ((k + part) * span) / steps]);
  }
  for (let k = 0; k < 12; k += 1) blanks.push([page.corner + 30 + (k * (page.width - 2 * page.corner - 60)) / 11, page.corner]);
  const pageCorners = [
    [page.corner, page.corner],
    [page.width - page.corner, page.corner],
    [page.width - page.corner, page.height - page.corner],
    [page.corner, page.height - page.corner],
  ];
  let best = null;
  let bestScore = 0;
  for (const { corners } of shapes) {
    const map = perspectiveMap(pageCorners, corners.map((corner) => [corner.x, corner.y]));
    if (!map) continue;
    const sidePx = (distance(corners[0], corners[3]) + distance(corners[1], corners[2])) / 2;
    const r = Math.max(1, Math.round(((page.edgeMarkSize / 2) * 0.6 * sidePx) / span));
    const sample = ([x, y]) => mean(...map(x, y), r);
    // Belgi (qora kvadrat) — kutilgan joy atrofida eng qora joy: bukilgan qog'ozda u biroz suriladi
    const reach = r * 2;
    const markSample = ([x, y]) => {
      const [u, v] = map(x, y);
      let low = 255;
      for (const dx of [-reach, -reach / 2, 0, reach / 2, reach]) {
        for (const dy of [-reach, -reach / 2, 0, reach / 2, reach]) low = Math.min(low, mean(u + dx, v + dy, r));
      }
      return low;
    };
    const blankValues = blanks.map(sample);
    const paper = median(blankValues);
    const darkness = median(pageCorners.map(markSample));
    if (paper - darkness < 30) continue;
    const threshold = (paper + darkness) / 2;
    const hits = marks.filter((mark) => markSample(mark) < threshold).length + blankValues.filter((value) => value > threshold).length;
    const score = hits / (marks.length + blanks.length);
    if (score > bestScore) {
      bestScore = score;
      best = corners;
    }
    if (score >= 0.95) break;
  }
  return bestScore >= 0.9 ? best : null;
}

/* ---------- Varaqni telefonning o'zida o'qish (ilovadagi skaner) ---------- */

/*
  backend/omr.py dagi scan_answer_sheet bilan bir xil usul, faqat kamera kadrining o'zida:
  burchaklar topilgach varaq tekislanadi, qora-oqqa ajratiladi, chet belgilari bo'yicha
  egilish tuzatiladi, har savol qatori chop etilgan halqalarga moslanadi va doirachalar
  ichining qoraygani o'lchanadi. Surat serverga yuborilmaydi — natija bir zumda.
*/
// Tekislangan varaqda 1 punkt = 2 piksel; to'liq A4 dagi EvalBee uslubidagi titulda 1,5 (doirachalar
// kattaroq — aniqlik o'sha, o'qish ~25% tez). Titul tuzilishi kelganda SHEET_PAGE bilan birga o'rnatiladi
let READ_SCALE = 2;
const FILLED_RATIO = 0.5;
const UNSURE_RATIO = 0.25;
const NAME_BOX = [120, 80, 420, 100];

// Burchak kvadratining aniq markazi (kichik kadrda topilgani taxminiy) — atrofidagi qora dog' markazi
function refineCorner(gray, width, height, x, y, radius) {
  const r = Math.max(3, Math.round(radius));
  let low = 255;
  let high = 0;
  for (let dy = -r; dy <= r; dy += 1) {
    for (let dx = -r; dx <= r; dx += 1) {
      const px = Math.round(x) + dx;
      const py = Math.round(y) + dy;
      if (px < 0 || py < 0 || px >= width || py >= height) continue;
      const value = gray[py * width + px];
      if (value < low) low = value;
      if (value > high) high = value;
    }
  }
  if (high - low < 30) return { x, y };
  const threshold = (low + high) / 2;
  let sumX = 0;
  let sumY = 0;
  let count = 0;
  for (let dy = -r; dy <= r; dy += 1) {
    for (let dx = -r; dx <= r; dx += 1) {
      const px = Math.round(x) + dx;
      const py = Math.round(y) + dy;
      if (px < 0 || py < 0 || px >= width || py >= height) continue;
      if (gray[py * width + px] < threshold) {
        sumX += px;
        sumY += py;
        count += 1;
      }
    }
  }
  return count ? { x: sumX / count, y: sumY / count } : { x, y };
}

// Tekislangan varaq: qora-oq (1 — qora) va kulrang nusxa; bir kadr uchun bir marta tayyorlanadi
function prepareSheetFrame(gray, width, height, corners) {
  const page = SHEET_PAGE;
  const W = Math.round(page.width * READ_SCALE);
  const H = Math.round(page.height * READ_SCALE);
  const pageCorners = [
    [page.corner, page.corner],
    [page.width - page.corner, page.corner],
    [page.width - page.corner, page.height - page.corner],
    [page.corner, page.height - page.corner],
  ].map(([x, y]) => [x * READ_SCALE, y * READ_SCALE]);
  const h = perspectiveMatrix(pageCorners, corners.map((corner) => [corner.x, corner.y]));
  if (!h) return null;
  const [h0, h1, h2, h3, h4, h5, h6, h7] = h;

  // Perspektivani to'g'rilash (bilinear) — chetdan tashqarisi oq. Har piksel uchun massiv
  // yaratilmaydi (2 mln piksel — telefonda eng sekin joyi shu edi)
  const warped = new Uint8ClampedArray(W * H);
  for (let y = 0; y < H; y += 1) {
    for (let x = 0; x < W; x += 1) {
      const w = h6 * x + h7 * y + 1;
      const u = (h0 * x + h1 * y + h2) / w;
      const v = (h3 * x + h4 * y + h5) / w;
      const x0 = Math.floor(u);
      const y0 = Math.floor(v);
      if (x0 < 0 || y0 < 0 || x0 >= width - 1 || y0 >= height - 1) {
        warped[y * W + x] = 255;
        continue;
      }
      const fx = u - x0;
      const fy = v - y0;
      const i = y0 * width + x0;
      const top = gray[i] + (gray[i + 1] - gray[i]) * fx;
      const bottom = gray[i + width] + (gray[i + width + 1] - gray[i + width]) * fx;
      warped[y * W + x] = top + (bottom - top) * fy;
    }
  }

  // Yengil xiralash (3×3, [1 2 1]×[1 2 1]) — ikki o'tishda: avval gorizontal, keyin vertikal.
  // So'ng mahalliy o'rtacha bo'yicha qora-oq (serverdagi adaptiveThreshold kabi)
  const across = new Uint16Array(W * H);
  for (let y = 0; y < H; y += 1) {
    const start = y * W;
    for (let x = 0; x < W; x += 1) {
      const left = warped[start + (x > 0 ? x - 1 : 0)];
      const right = warped[start + (x < W - 1 ? x + 1 : W - 1)];
      across[start + x] = left + 2 * warped[start + x] + right;
    }
  }
  const blurred = new Float32Array(W * H);
  for (let y = 0; y < H; y += 1) {
    const up = (y > 0 ? y - 1 : 0) * W;
    const down = (y < H - 1 ? y + 1 : H - 1) * W;
    const start = y * W;
    for (let x = 0; x < W; x += 1) blurred[start + x] = (across[up + x] + 2 * across[start + x] + across[down + x]) / 16;
  }
  const row = W + 1;
  const integral = new Float64Array(row * (H + 1));
  for (let y = 0; y < H; y += 1) {
    let rowSum = 0;
    for (let x = 0; x < W; x += 1) {
      rowSum += blurred[y * W + x];
      integral[(y + 1) * row + x + 1] = integral[y * row + x + 1] + rowSum;
    }
  }
  const half = Math.round(40 * READ_SCALE) >> 1;
  // Qog'oz yorqinligi: atrofdagi (~9 pt) eng yorug' nuqta. Soya chegarasida o'rtacha yorqinlik aralashib
  // ketadi — soyadagi oq qog'oz "qora" bo'lib qolmasligi uchun nuqta qog'ozdan sezilarli qora bo'lishi ham
  // shart (aks holda soyada imtihon kodi kataklari noto'g'ri o'qilib, titul "boshqa imtihonniki" bo'lib qolardi)
  const paper = paperBrightness(blurred, W, H, Math.max(2, Math.round(9 * READ_SCALE)));
  const binary = new Uint8Array(W * H);
  // Qora piksellar soni uchun integral — kvadrat ichidagi ulush bir zumda (qora-oq bilan bir o'tishda)
  const binaryIntegral = new Int32Array(row * (H + 1));
  const left = new Int32Array(W);
  const right = new Int32Array(W);
  for (let x = 0; x < W; x += 1) {
    left[x] = Math.max(0, x - half);
    right[x] = Math.min(W, x + half + 1);
  }
  for (let y = 0; y < H; y += 1) {
    const top = Math.max(0, y - half) * row;
    const bottom = Math.min(H, y + half + 1) * row;
    const rows = (bottom - top) / row;
    const start = y * W;
    let rowSum = 0;
    for (let x = 0; x < W; x += 1) {
      const x0 = left[x];
      const x1 = right[x];
      const mean = (integral[bottom + x1] - integral[top + x1] - integral[bottom + x0] + integral[top + x0]) / ((x1 - x0) * rows);
      const value = blurred[start + x];
      const dark = value <= mean - 18 && value <= paper[start + x] * 0.8 ? 1 : 0;
      binary[start + x] = dark;
      rowSum += dark;
      binaryIntegral[(y + 1) * row + x + 1] = binaryIntegral[y * row + x + 1] + rowSum;
    }
  }
  return { W, H, warped, binary, binaryIntegral, masks: new Map() };
}

// Har nuqta atrofidagi to'rtburchakdagi eng katta qiymat: x bo'yicha [−left, +right], y bo'yicha [−up, +down]
// piksel. Ikki o'tishda, har biri O(n) (van Herk)
function slidingMaxBox(source, width, height, left, right, up, down) {
  const longest = Math.max(width + left + right, height + up + down);
  const line = new Float32Array(longest);
  const ahead = new Float32Array(longest);
  const behind = new Float32Array(longest);
  const pass = (input, output, count, length, stride, step, before, after) => {
    const size = before + after + 1;
    const total = length + before + after;
    for (let index = 0; index < count; index += 1) {
      const base = index * stride;
      line.fill(-1, 0, total);
      for (let i = 0; i < length; i += 1) line[before + i] = input[base + i * step];
      for (let i = 0; i < total; i += 1) ahead[i] = i % size === 0 ? line[i] : Math.max(ahead[i - 1], line[i]);
      for (let i = total - 1; i >= 0; i -= 1) {
        behind[i] = i === total - 1 || (i + 1) % size === 0 ? line[i] : Math.max(behind[i + 1], line[i]);
      }
      for (let i = 0; i < length; i += 1) output[base + i * step] = Math.max(behind[i], ahead[i + before + after]);
    }
  };
  const across = new Float32Array(width * height);
  const result = new Float32Array(width * height);
  pass(source, across, height, width, width, 1, left, right); // qatorlar bo'ylab
  pass(across, result, width, height, 1, width, up, down); // ustunlar bo'ylab
  return result;
}

// Har nuqta atrofidagi (±reach piksel kvadrat) eng katta qiymat
function slidingMax2D(source, width, height, reach) {
  return slidingMaxBox(source, width, height, reach, reach, reach, reach);
}

// Qog'oz yorqinligi soya chegarasida ham to'g'ri: chap / o'ng / yuqori / past yarim-oynalardagi eng yorug'
// joyning eng xirasi. Soya chegarasi yonida chegaradan o'tmaydigan tomon soyadagi qog'ozni beradi;
// bo'yalgan doiracha esa har tomondan oq qog'oz bilan o'ralgan — u qoraligicha qoladi
function paperBrightness(source, width, height, reach) {
  const sides = [
    slidingMaxBox(source, width, height, reach, 0, reach, reach),
    slidingMaxBox(source, width, height, 0, reach, reach, reach),
    slidingMaxBox(source, width, height, reach, reach, reach, 0),
    slidingMaxBox(source, width, height, reach, reach, 0, reach),
  ];
  const result = sides[0];
  for (let i = 0; i < result.length; i += 1) {
    result[i] = Math.min(result[i], sides[1][i], sides[2][i], sides[3][i]);
  }
  return result;
}

// Doiracha atrofidagi halqa (chop etilgan chegara) va ichki doira — nuqtalar ro'yxati
function bubbleMasks(sheet, radius) {
  const key = Math.round(radius * 100);
  if (!sheet.masks.has(key)) {
    const size = Math.round(radius * READ_SCALE * 1.4);
    const unit = Math.max(1, size / 1.4);
    const ring = [];
    const inner = [];
    for (let dy = -size; dy <= size; dy += 1) {
      for (let dx = -size; dx <= size; dx += 1) {
        const distance = Math.sqrt(dx * dx + dy * dy) / unit;
        if (distance >= 0.82 && distance <= 1.18) ring.push(dy * sheet.W + dx, dx, dy);
        if (distance <= 0.55) inner.push(dy * sheet.W + dx, dx, dy);
      }
    }
    sheet.masks.set(key, { ring, inner });
  }
  return sheet.masks.get(key);
}

// Niqob ostidagi qora ulush (punktlardagi nuqta atrofida); varaqdan tashqarisi oq hisoblanadi
function maskFill(sheet, mask, x, y) {
  const cx = Math.round(x * READ_SCALE);
  const cy = Math.round(y * READ_SCALE);
  const { W, H, binary } = sheet;
  if (cx < 0 || cy < 0 || cx >= W || cy >= H) return 0;
  const base = cy * W + cx;
  let dark = 0;
  for (let i = 0; i < mask.length; i += 3) {
    const px = cx + mask[i + 1];
    const py = cy + mask[i + 2];
    if (px >= 0 && py >= 0 && px < W && py < H) dark += binary[base + mask[i]];
  }
  return dark / (mask.length / 3);
}

function boxFill(sheet, x, y, r) {
  const { W, H, binaryIntegral } = sheet;
  const row = W + 1;
  const x0 = Math.max(0, Math.round(x * READ_SCALE) - r);
  const x1 = Math.min(W, Math.round(x * READ_SCALE) + r + 1);
  const y0 = Math.max(0, Math.round(y * READ_SCALE) - r);
  const y1 = Math.min(H, Math.round(y * READ_SCALE) + r + 1);
  if (x1 <= x0 || y1 <= y0) return 0;
  return (
    (binaryIntegral[y1 * row + x1] - binaryIntegral[y0 * row + x1] - binaryIntegral[y1 * row + x0] + binaryIntegral[y0 * row + x0]) /
    ((x1 - x0) * (y1 - y0))
  );
}

// Chetdagi tekislash belgilarining haqiqiy joyi (kutilgan joy atrofida qora kvadrat)
function edgeMarkOffsets(sheet, marks) {
  const offsets = { left: [], right: [] };
  for (const mark of marks) {
    const y = SHEET_PAGE.height - mark.y;
    const found = locateMark(sheet, mark.x, y, mark.r, 9);
    if (found) offsets[mark.ref].push([y, found[0], found[1]]);
  }
  offsets.left.sort((a, b) => a[0] - b[0]);
  offsets.right.sort((a, b) => a[0] - b[0]);
  return offsets;
}

// Kutilgan joy (x, y — punktlarda) atrofidagi (±window) qora kvadrat: haqiqiy joyi farqi [dx, dy] yoki null
function locateMark(sheet, x, y, half, window) {
  const { W, H, binary } = sheet;
  const x0 = Math.max(0, Math.round((x - window) * READ_SCALE));
  const y0 = Math.max(0, Math.round((y - window) * READ_SCALE));
  const x1 = Math.min(W, Math.round((x + window) * READ_SCALE));
  const y1 = Math.min(H, Math.round((y + window) * READ_SCALE));
  if (x1 <= x0 || y1 <= y0) return null;
  const pw = x1 - x0;
  const seen = new Uint8Array(pw * (y1 - y0));
  const expected = (2 * half * READ_SCALE) ** 2;
  let best = null;
  for (let start = 0; start < seen.length; start += 1) {
    const sx = start % pw;
    const sy = (start - sx) / pw;
    if (seen[start] || !binary[(y0 + sy) * W + x0 + sx]) continue;
    const stack = [start];
    seen[start] = 1;
    let area = 0;
    let sumX = 0;
    let sumY = 0;
    while (stack.length) {
      const index = stack.pop();
      const px = index % pw;
      const py = (index - px) / pw;
      area += 1;
      sumX += px;
      sumY += py;
      for (let dy = -1; dy <= 1; dy += 1) {
        for (let dx = -1; dx <= 1; dx += 1) {
          const nx = px + dx;
          const ny = py + dy;
          if (nx < 0 || ny < 0 || nx >= pw || ny >= y1 - y0) continue;
          const next = ny * pw + nx;
          if (seen[next] || !binary[(y0 + ny) * W + x0 + nx]) continue;
          seen[next] = 1;
          stack.push(next);
        }
      }
    }
    if (area < 0.4 * expected || area > 2.2 * expected) continue;
    const cx = sumX / area / READ_SCALE + x0 / READ_SCALE;
    const cy = sumY / area / READ_SCALE + y0 / READ_SCALE;
    const distance = (cx - x) ** 2 + (cy - y) ** 2;
    if (!best || distance < best[0]) best = [distance, cx - x, cy - y];
  }
  return best ? [best[1], best[2]] : null;
}

function interpolateOffset(points, y) {
  if (!points.length) return [0, 0];
  if (y <= points[0][0]) return [points[0][1], points[0][2]];
  const last = points[points.length - 1];
  if (y >= last[0]) return [last[1], last[2]];
  for (let i = 1; i < points.length; i += 1) {
    const [y1, dx1, dy1] = points[i - 1];
    const [y2, dx2, dy2] = points[i];
    if (y1 <= y && y <= y2) {
      const t = y2 > y1 ? (y - y1) / (y2 - y1) : 0;
      return [dx1 + (dx2 - dx1) * t, dy1 + (dy2 - dy1) * t];
    }
  }
  return [0, 0];
}

// Tayyorlangan varaqdan javoblarni o'qish (doirachalar joyi — serverdan olingan titul tuzilishi)
function readSheetAnswers(sheet, bubbles, page = 1) {
  const pageBubbles = bubbles.filter((bubble) => bubble.page === page);
  const offsets = edgeMarkOffsets(sheet, pageBubbles.filter((bubble) => bubble.kind === "mark"));
  const leftX = SHEET_PAGE.corner;
  const rightX = SHEET_PAGE.width - SHEET_PAGE.corner;
  const corrected = (x, y) => {
    const [ldx, ldy] = interpolateOffset(offsets.left, y);
    const [rdx, rdy] = interpolateOffset(offsets.right, y);
    const t = Math.min(1, Math.max(0, (x - leftX) / (rightX - leftX)));
    return [x + ldx + (rdx - ldx) * t, y + ldy + (rdy - ldy) * t];
  };

  // 7-versiya: har 5 savollik blok burchagidagi kvadratlar — doiracha joyi o'z blokining 4 kvadrati
  // orasida (bilinear) aniqlanadi; topilmagan kvadrat o'rniga chet belgilari bo'yicha joy
  const anchorShift = new Map();
  for (const anchor of pageBubbles) {
    if (anchor.kind !== "anchor") continue;
    const x = anchor.x;
    const y = SHEET_PAGE.height - anchor.y;
    const [cx, cy] = corrected(x, y);
    const [fx, fy] = locateMark(sheet, cx, cy, anchor.r, 5) || [0, 0];
    anchorShift.set(anchor.letter, [x, y, cx + fx - x, cy + fy - y]);
  }
  const placed = (bubble) => {
    const x = bubble.x;
    const y = SHEET_PAGE.height - bubble.y;
    const block = bubble.block;
    if (!block || block.some((corner) => !anchorShift.has(corner))) return corrected(x, y);
    const [topLeft, topRight, bottomRight, bottomLeft] = block.map((corner) => anchorShift.get(corner));
    const u = (x - topLeft[0]) / (topRight[0] - topLeft[0]);
    const v = (y - topLeft[1]) / (bottomLeft[1] - topLeft[1]);
    const weights = [(1 - u) * (1 - v), u * (1 - v), u * v, (1 - u) * v];
    const shifts = [topLeft, topRight, bottomRight, bottomLeft];
    let dx = 0;
    let dy = 0;
    weights.forEach((weight, index) => {
      dx += weight * shifts[index][2];
      dy += weight * shifts[index][3];
    });
    return [x + dx, y + dy];
  };

  // Har savol qatori chop etilgan halqalari ustiga moslanadi (ustun bo'yicha tepadan pastga)
  const rows = new Map();
  for (const bubble of pageBubbles) {
    if (bubble.kind === "mark" || bubble.kind === "code" || bubble.kind === "anchor") continue;
    if (!rows.has(bubble.ref)) rows.set(bubble.ref, []);
    rows.get(bubble.ref).push(bubble);
  }
  const ordered = [...rows.entries()].sort(
    ([, a], [, b]) => Math.round(a[0].x / 40) - Math.round(b[0].x / 40) || b[0].y - a[0].y
  );
  const rowShift = new Map();
  const aligned = [];
  for (const [ref, row] of ordered) {
    const centers = row.map(placed);
    const [x0, y0] = centers[0];
    let nearest = null;
    for (const item of aligned) {
      const distance = (item[0] - x0) ** 2 + (item[1] - y0) ** 2;
      if (!nearest || distance < nearest[0]) nearest = [distance, item];
    }
    const start = nearest ? nearest[1][2] : [0, 0];
    const far = !nearest || Math.abs(nearest[1][0] - x0) >= 40 || Math.abs(nearest[1][1] - y0) >= 60;
    const reach = far ? 8 : 5;
    const radius = row[0].r;
    const { ring } = bubbleMasks(sheet, radius);
    let best = null;
    for (let gy = -reach; gy <= reach; gy += 1) {
      for (let gx = -reach; gx <= reach; gx += 1) {
        const dx = start[0] + gx * radius * 0.16;
        const dy = start[1] + gy * radius * 0.16;
        let score = 0;
        for (const [cx, cy] of centers) score += maskFill(sheet, ring, cx + dx, cy + dy);
        if (!best || score > best[0]) best = [score, dx, dy];
      }
    }
    rowShift.set(ref, [best[1], best[2]]);
    aligned.push([x0, y0, [best[1], best[2]]]);
  }

  const codeBits = new Map();
  const questions = new Map();
  const positions = {}; // {savol: {harf: [x, y, r]}} — qatorga moslangan joyi (punktlarda)
  const highlights = []; // bo'yalgan ID va variant doirachalari — natija rasmida sariq halqa
  const idRows = new Map();
  const variantRatios = new Map();
  for (const bubble of pageBubbles) {
    if (bubble.kind === "mark" || bubble.kind === "anchor") continue;
    const [x, y] = placed(bubble);
    if (bubble.kind === "code") {
      // Kod katagi: atrofidagi bir necha joydan eng qorasi (serverdagi _fill_ratio square). Kataklar
      // orasi keng (≈40 pt) — qog'oz bukilib siljigan katak ham topilsin
      const inner = Math.max(2, Math.round(bubble.r * READ_SCALE * 0.55));
      const shift = bubble.r * 1.6;
      let ratio = 0;
      for (const dx of [-shift, -shift / 2, 0, shift / 2, shift]) {
        for (const dy of [-shift, -shift / 2, 0, shift / 2, shift]) ratio = Math.max(ratio, boxFill(sheet, x + dx, y + dy, inner));
      }
      codeBits.set(Number(bubble.letter), ratio);
      continue;
    }
    const [dx, dy] = rowShift.get(bubble.ref);
    const { inner } = bubbleMasks(sheet, bubble.r);
    let ratio = 0;
    for (const ox of [-0.1, 0, 0.1]) {
      for (const oy of [-0.1, 0, 0.1]) ratio = Math.max(ratio, maskFill(sheet, inner, x + dx + ox * bubble.r, y + dy + oy * bubble.r));
    }
    if (bubble.kind === "id" || bubble.kind === "variant") {
      if (ratio >= FILLED_RATIO) highlights.push([x + dx, y + dy, bubble.r]);
      if (bubble.kind === "variant") {
        variantRatios.set(bubble.letter, ratio);
        continue;
      }
      if (!idRows.has(bubble.ref)) idRows.set(bubble.ref, new Map());
      idRows.get(bubble.ref).set(bubble.letter, ratio);
      continue;
    }
    if (!questions.has(bubble.ref)) {
      questions.set(bubble.ref, { ref: bubble.ref, label: bubble.label, kind: bubble.kind, options: [], ratios: {} });
    }
    if (!positions[bubble.ref]) positions[bubble.ref] = {};
    positions[bubble.ref][bubble.letter] = [x + dx, y + dy, bubble.r];
    const entry = questions.get(bubble.ref);
    entry.options.push(bubble.letter);
    entry.ratios[bubble.letter] = ratio;
  }

  const answers = {};
  const result = [];
  for (const entry of questions.values()) {
    const filled = entry.options.filter((letter) => entry.ratios[letter] >= FILLED_RATIO);
    const unsure = entry.options.filter((letter) => entry.ratios[letter] >= UNSURE_RATIO && entry.ratios[letter] < FILLED_RATIO);
    let status;
    if (entry.kind === "multi") status = unsure.length ? "unsure" : filled.length ? "ok" : "empty";
    else if (filled.length > 1) status = "multiple";
    else if (filled.length) status = unsure.length ? "unsure" : "ok";
    else status = unsure.length ? "unsure" : "empty";
    const chosen = entry.kind === "multi" || filled.length === 1 ? filled.join(",") : "";
    if (chosen) answers[String(entry.ref)] = chosen;
    result.push({ ...entry, chosen, status });
  }

  let sheetNumber = null;
  let numberStatus = "none";
  if (idRows.size) {
    const digits = [...idRows.keys()].sort().map((ref) => {
      const filled = [...idRows.get(ref).entries()].filter(([, ratio]) => ratio >= FILLED_RATIO).map(([digit]) => digit);
      return filled.length === 1 ? filled[0] : null;
    });
    if (digits.every((digit) => digit === null)) numberStatus = "empty";
    else if (digits.some((digit) => digit === null) || Number(digits.join("")) === 0) numberStatus = "unclear";
    else {
      sheetNumber = Number(digits.join(""));
      numberStatus = "ok";
    }
  }

  const markedVariants = [...variantRatios.entries()].filter(([, ratio]) => ratio >= FILLED_RATIO).map(([name]) => name);
  const variant = markedVariants.length === 1 ? markedVariants[0] : null;

  let sheetCode = null;
  if (codeBits.size && [...codeBits.values()].every((ratio) => ratio >= FILLED_RATIO || ratio < UNSURE_RATIO)) {
    sheetCode = 0;
    for (const [bit, ratio] of codeBits) if (ratio >= FILLED_RATIO) sheetCode += 2 ** bit;
  }

  return {
    answers,
    questions: result,
    sheet_number: sheetNumber,
    sheet_number_status: numberStatus,
    variant,
    sheet_code: sheetCode,
    positions,
    highlights,
  };
}

// Bitta kamera kadri: kichraytirilgan kadrda burchaklar; topilsa va titul tuzilishi bo'lsa — to'liq
// o'lchamli kadrda varaq o'qiladi. Web Worker ichida ham, asosiy oqimda ham bir xil ishlaydi
// (source — video yoki ImageBitmap)
function analyzeFrame(source, sourceWidth, sourceHeight, options) {
  const pool = analyzeFrame.pool || (analyzeFrame.pool = {});
  // crop — kadrning faqat shu qismi (manba pikselida): [x, y, eni, bo'yi]
  const context = (name, width, height, crop = [0, 0, sourceWidth, sourceHeight]) => {
    if (!pool[name]) {
      const canvas = typeof OffscreenCanvas !== "undefined" ? new OffscreenCanvas(1, 1) : document.createElement("canvas");
      pool[name] = canvas.getContext("2d", { willReadFrequently: true });
    }
    const draw = pool[name];
    if (draw.canvas.width !== width) draw.canvas.width = width;
    if (draw.canvas.height !== height) draw.canvas.height = height;
    draw.drawImage(source, ...crop, 0, 0, width, height);
    return draw.getImageData(0, 0, width, height).data;
  };

  // Kadr kichraytiriladi (uzun tomoni 720 px) — chet belgilari ko'rinadigan darajada
  const factor = 720 / Math.max(sourceWidth, sourceHeight);
  const width = Math.round(sourceWidth * factor);
  const height = Math.round(sourceHeight * factor);
  const corners = findSheetCorners(context("probe", width, height), width, height, options.edgeMarks);
  const answer = { width, height, corners, read: null };
  if (!corners || !options.layout || options.skipRead) return answer;

  // To'liq o'lchamda faqat titul turgan qism olinadi (burchaklar atrofi) — kadrning qolgani
  // (stol, boshqa titullar) o'qilmaydi; ixcham titulda bu kadrning uchdan biri
  const scale = Math.min(1, 1600 / Math.max(sourceWidth, sourceHeight));
  const k = (sourceWidth * scale) / width; // kichik kadr pikseli → to'liq o'lcham pikseli
  const margin = Math.max(...corners.map((corner) => corner.size)) * k * 1.5 + 4;
  const xs = corners.map((corner) => corner.x * k);
  const ys = corners.map((corner) => corner.y * k);
  const left = Math.max(0, Math.floor(Math.min(...xs) - margin));
  const top = Math.max(0, Math.floor(Math.min(...ys) - margin));
  const fullWidth = Math.min(Math.round(sourceWidth * scale), Math.ceil(Math.max(...xs) + margin)) - left;
  const fullHeight = Math.min(Math.round(sourceHeight * scale), Math.ceil(Math.max(...ys) + margin)) - top;
  const pixels = context("full", fullWidth, fullHeight, [left / scale, top / scale, fullWidth / scale, fullHeight / scale]);
  const gray = new Float32Array(fullWidth * fullHeight);
  for (let i = 0, p = 0; i < gray.length; i += 1, p += 4) {
    gray[i] = pixels[p] * 0.299 + pixels[p + 1] * 0.587 + pixels[p + 2] * 0.114;
  }
  const full = corners.map((corner) =>
    refineCorner(gray, fullWidth, fullHeight, corner.x * k - left, corner.y * k - top, corner.size * k)
  );
  const sheet = prepareSheetFrame(gray, fullWidth, fullHeight, full);
  if (!sheet) return answer;
  let result = readSheetAnswers(sheet, options.layout.bubbles, options.page);
  if (options.page === 1 && options.layout.old_bubbles && result.sheet_number_status !== "ok") {
    // Oldin chop etilgan 3 xonali ID'li titul
    const old = readSheetAnswers(sheet, options.layout.old_bubbles, 1);
    if (old.sheet_number_status === "ok") result = old;
  }
  // Natija ekraniga faqat tekislangan kulrang varaq kerak
  answer.read = { result, sheet: { W: sheet.W, H: sheet.H, warped: sheet.warped } };
  return answer;
}

// Kadrlar alohida oqimda (Web Worker) o'qiladi — ekran va kamera qotmaydi. Worker shu fayldagi
// funksiyalarning o'zidan yig'iladi; ishlamasa (eski WebView) — null, kadr asosiy oqimda o'qiladi
function createScanWorker() {
  if (typeof Worker === "undefined" || typeof OffscreenCanvas === "undefined" || typeof createImageBitmap === "undefined") {
    return null;
  }
  const source = [
    `let SHEET_PAGE = ${JSON.stringify(A4_SHEET_PAGE)};`,
    `let READ_SCALE = 2;`,
    `const FILLED_RATIO = ${FILLED_RATIO};`,
    `const UNSURE_RATIO = ${UNSURE_RATIO};`,
    ...[
      perspectiveMatrix,
      perspectiveMap,
      findSheetCorners,
      cornersFromEdgeMarks,
      refineCorner,
      prepareSheetFrame,
      slidingMaxBox,
      slidingMax2D,
      paperBrightness,
      bubbleMasks,
      maskFill,
      boxFill,
      edgeMarkOffsets,
      locateMark,
      interpolateOffset,
      readSheetAnswers,
      analyzeFrame,
    ].map(String),
    `let layout = null;
    let page = 1;
    onmessage = ({ data }) => {
      if ("layout" in data) {
        layout = data.layout;
        page = data.page;
        SHEET_PAGE = data.sheetPage;
        READ_SCALE = SHEET_PAGE.readScale || 2;
        return;
      }
      const { bitmap, edgeMarks, skipRead } = data;
      let answer;
      try {
        answer = analyzeFrame(bitmap, bitmap.width, bitmap.height, { edgeMarks, skipRead, layout, page });
      } catch (error) {
        answer = { error: String(error) };
      }
      bitmap.close();
      postMessage(answer, answer.read ? [answer.read.sheet.warped.buffer] : []);
    };`,
  ].join("\n");
  try {
    const url = URL.createObjectURL(new Blob([source], { type: "text/javascript" }));
    const worker = new Worker(url);
    worker.url = url;
    return worker;
  } catch {
    return null;
  }
}

