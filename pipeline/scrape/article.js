/**
 * Article extraction and phrase measurement, injected into the page.
 *
 * Both entry points walk the DOM with the *same* filtered TreeWalker and build
 * the same flat string, so a character offset chosen in Python from `extract()`
 * output addresses exactly the same characters when handed back to `measure()`.
 * That shared walk is the whole trick: it is what lets phrase selection live in
 * testable Python while the measurement stays in the browser, where the real
 * layout is.
 */
(() => {
  const SKIP = "script, style, noscript, figcaption, aside, nav, footer, form, .ad, .advert";
  const BLOCK = "p, h1, h2, h3, h4, li, blockquote, dd, dt";

  function isVisible(el) {
    if (!el) return false;
    const cs = getComputedStyle(el);
    return (
      cs.display !== "none" &&
      cs.visibility !== "hidden" &&
      parseFloat(cs.opacity || "1") >= 0.05
    );
  }

  function pickContainer(selectors, minChars) {
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el && isVisible(el) && (el.innerText || "").trim().length >= minChars) {
        return { el, selector: sel };
      }
    }
    // Nothing matched: fall back to the block holding the most paragraph text.
    let best = null;
    let bestChars = 0;
    for (const el of document.querySelectorAll("article, main, section, div")) {
      const paragraphs = el.querySelectorAll(":scope > p");
      if (paragraphs.length < 3 || !isVisible(el)) continue;
      let chars = 0;
      paragraphs.forEach((p) => (chars += (p.innerText || "").trim().length));
      if (chars > bestChars) {
        bestChars = chars;
        best = el;
      }
    }
    return best && bestChars >= minChars ? { el: best, selector: null } : null;
  }

  function walk(container) {
    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        if (!node.nodeValue || !node.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
        const parent = node.parentElement;
        if (!parent || parent.closest(SKIP)) return NodeFilter.FILTER_REJECT;
        if (!isVisible(parent)) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    const entries = [];
    let text = "";
    while (walker.nextNode()) {
      const node = walker.currentNode;
      entries.push({ node, start: text.length, length: node.nodeValue.length });
      text += node.nodeValue;
    }
    return { entries, text };
  }

  function paragraphRanges(entries) {
    const ranges = [];
    let current = null;
    for (const entry of entries) {
      const block = entry.node.parentElement && entry.node.parentElement.closest(BLOCK);
      const key = block || entry.node.parentElement;
      if (!current || current.key !== key) {
        if (current) ranges.push(current);
        current = {
          key,
          tag: (block ? block.tagName : "DIV").toLowerCase(),
          start: entry.start,
          end: entry.start + entry.length,
        };
      } else {
        current.end = entry.start + entry.length;
      }
    }
    if (current) ranges.push(current);
    return ranges.map((r) => ({ tag: r.tag, start: r.start, end: r.end }));
  }

  function locate(entries, offset) {
    let lo = 0;
    let hi = entries.length - 1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      const entry = entries[mid];
      if (offset < entry.start) hi = mid - 1;
      else if (offset > entry.start + entry.length) lo = mid + 1;
      else return { node: entry.node, offset: offset - entry.start };
    }
    return null;
  }

  function metaContent(names) {
    for (const name of names) {
      const el =
        document.querySelector(`meta[property="${name}"]`) ||
        document.querySelector(`meta[name="${name}"]`);
      const value = el && el.getAttribute("content");
      if (value && value.trim()) return value.trim();
    }
    return "";
  }

  window.__vs = {
    extract(selectors, minChars) {
      const picked = pickContainer(selectors, minChars);
      if (!picked) return { ok: false, reason: "no-container" };
      const { entries, text } = walk(picked.el);
      window.__vsState = { container: picked.el, entries };
      const box = picked.el.getBoundingClientRect();
      return {
        ok: true,
        selector: picked.selector,
        text,
        readable: (picked.el.innerText || "").replace(/\n{3,}/g, "\n\n").trim(),
        paragraphs: paragraphRanges(entries),
        title:
          metaContent(["og:title", "twitter:title"]) ||
          (document.querySelector("h1") || {}).innerText ||
          document.title ||
          "",
        byline: metaContent(["article:author", "author", "og:site_name"]),
        published: metaContent(["article:published_time", "datePublished"]),
        description: metaContent(["og:description", "description"]),
        page: {
          width: document.documentElement.scrollWidth,
          height: document.documentElement.scrollHeight,
        },
        container: {
          x: box.left + window.scrollX,
          y: box.top + window.scrollY,
          w: box.width,
          h: box.height,
        },
      };
    },

    measure(spans) {
      const state = window.__vsState;
      if (!state) return { ok: false, reason: "no-state" };
      const out = [];
      for (const span of spans) {
        const a = locate(state.entries, span.start);
        const b = locate(state.entries, span.end);
        if (!a || !b) {
          out.push(null);
          continue;
        }
        const range = document.createRange();
        try {
          range.setStart(a.node, a.offset);
          range.setEnd(b.node, b.offset);
        } catch (err) {
          out.push(null);
          continue;
        }
        const rects = Array.from(range.getClientRects()).filter(
          (r) => r.width > 1 && r.height > 1
        );
        if (!rects.length) {
          out.push(null);
          continue;
        }
        // A phrase that wraps produces one rect per line. Marking the union
        // would draw a box around whitespace, so the marker takes the first
        // line and the wrap is recorded for the selector to weigh.
        const first = rects[0];
        const sameLine = rects.filter((r) => Math.abs(r.top - first.top) < 2);
        const left = Math.min(...sameLine.map((r) => r.left));
        const right = Math.max(...sameLine.map((r) => r.right));
        const union = {
          x: Math.min(...rects.map((r) => r.left)) + window.scrollX,
          y: Math.min(...rects.map((r) => r.top)) + window.scrollY,
          w: Math.max(...rects.map((r) => r.right)) - Math.min(...rects.map((r) => r.left)),
          h: Math.max(...rects.map((r) => r.bottom)) - Math.min(...rects.map((r) => r.top)),
        };
        out.push({
          rect: {
            x: left + window.scrollX,
            y: first.top + window.scrollY,
            w: right - left,
            h: first.height,
          },
          union,
          lines: rects.length,
        });
      }
      return { ok: true, rects: out };
    },
  };
})();
